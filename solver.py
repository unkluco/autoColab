"""Run Codex with UTF-8 stdin and capture only its final response."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib

from config import Settings, ConfigError
from notebooks import Marker, Snapshot
from process_job import WindowsJob, gated_command
from storage_safety import recover_calls, directory_bytes, path_link


class SolverError(RuntimeError):
    pass


class SolverInputError(SolverError):
    """This notebook cannot be sent; other notebooks can still proceed."""


class SolverResponseError(SolverError):
    """The CLI succeeded but this answer cannot be inserted into the notebook."""


class SolverCleanupError(SolverError):
    """Fatal: restart the host's process job to remove unfinished descendants."""


class SolverStopped(SolverError):
    pass


class _BoundedLog:
    """Drain both CLI output streams without retaining their contents in memory."""
    def __init__(self, path: Path, limit: int):
        self.stream = path.open('wb')
        self.limit = limit
        self.problem = None
        self.thread = None

    def start(self, pipe):
        def drain():
            written = 0
            try:
                while chunk := pipe.read1(65536):
                    remaining = max(0, self.limit - written)
                    if remaining:
                        kept = chunk[:remaining]
                        self.stream.write(kept)
                        self.stream.flush()
                        written += len(kept)
                    if len(chunk) > remaining:
                        self.problem = f'Codex log exceeded {self.limit} bytes'
                        # Continue draining until the caller terminates the process.
            except (OSError, ValueError) as exc:
                self.problem = f'Cannot capture Codex log: {exc}'
            finally:
                for stream in (pipe, self.stream):
                    try:
                        stream.close()
                    except (OSError, ValueError) as exc:
                        self.problem = f'Cannot close Codex log stream: {exc}'

        self.thread = threading.Thread(target=drain, name='codex-log', daemon=True)
        try:
            self.thread.start()
        except RuntimeError as exc:
            self.thread = None
            pipe.close()
            self.stream.close()
            raise SolverError(f'Cannot start Codex log reader: {exc}') from exc


def _send_input(process, payload):
    # A separate writer keeps stop/timeout checks responsive even if stdin is never read.
    def write():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # The exit code or missing answer provides the useful failure below.
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    thread = threading.Thread(target=write, name='codex-input', daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        process.stdin.close()
        raise SolverError(f'Cannot start Codex input writer: {exc}') from exc
    return thread


def _stop_process_tree(process, threads):
    """Best effort tree termination; no cancellation operation may wait forever."""
    errors = []
    job = getattr(process, '_autocolab_job', None)
    if isinstance(job, WindowsJob):
        try:
            job.stop()
        except OSError as exc:
            errors.append(f'process job termination failed: {exc}')
    elif os.name == 'nt':
        try:
            result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
            if result.returncode:
                errors.append(f'taskkill returned {result.returncode}')
        except subprocess.TimeoutExpired:
            errors.append('taskkill timed out')
        except OSError as exc:
            errors.append(f'taskkill failed: {exc}')
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(f'process group termination failed: {exc}')
    if process.poll() is None:
        try:
            process.kill()
        except OSError as exc:
            errors.append(f'process termination failed: {exc}')
    deadline = time.monotonic() + 5
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f'process did not stop: {exc}')
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))
        if thread.is_alive():
            errors.append(f'{thread.name} did not stop')
    return '; '.join(errors)


def resolve_codex_command(command: str) -> list[str]:
    found = shutil.which(command)
    if not found and Path(command).is_file():
        found = str(Path(command).resolve())
    if not found:
        raise SolverError(f'Codex CLI not found: {command}')
    executable = Path(found)
    if os.name != 'nt' or executable.suffix.lower() not in ('.cmd', '.bat', '.ps1'):
        return [str(executable)]
    # The npm wrapper is a shell script. Invoke its native binary or JS entry directly.
    npm_package = executable.parent / 'node_modules' / '@openai' / 'codex'
    arch = 'arm64' if os.environ.get('PROCESSOR_ARCHITECTURE', '').lower() == 'arm64' else 'x64'
    vendor = npm_package / 'node_modules' / '@openai' / f'codex-win32-{arch}' / 'vendor'
    matches = sorted(vendor.glob('*/codex/codex.exe'))
    if matches:
        return [str(matches[0])]
    entry = npm_package / 'bin' / 'codex.js'
    node = shutil.which('node')
    if entry.is_file() and node:
        return [node, str(entry)]
    raise SolverError('Cannot resolve Codex npm wrapper. Set codex.command to the codex.exe path.')


class CodexSolver:
    def __init__(self, settings: Settings, stop_event=None):
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.command = resolve_codex_command(settings.command)
        self.validate_prompts()

    def instructions(self, cell_type):
        prompt = self.settings.code_prompt_file if cell_type == 'code' else self.settings.markdown_prompt_file
        try:
            text = self.settings.main_prompt_file.read_text(encoding='utf-8-sig') + '\n\n' + prompt.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f'Cannot read Codex instructions: {exc}') from exc
        return text.replace('@bot', self.settings.marker)

    def validate_arguments(self, arguments):
        if os.name == 'nt':
            command = subprocess.list2cmdline(gated_command(arguments))
            if len(command.encode('utf-16-le')) // 2 >= 30000:
                raise ConfigError('Codex command/instructions are too long for Windows; shorten prompt/configuration')

    def validate_prompts(self):
        for kind in self.settings.cell_types:
            scratch = self.settings.runtime_dir / 'calls' / ('codex-' + 'x' * 8)
            self.validate_arguments(self.build_arguments(scratch / 'answer.txt', scratch, self.instructions(kind)))

    def stopping(self):
        return self.stop_event.is_set() or (self.settings.runtime_dir / 'stop.request').exists()

    def build_arguments(self, response_file: Path, scratch: Path, instructions: str):
        settings = self.settings
        args = self.command + [
            'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '--ephemeral',
            '--color', 'never', '--output-last-message', str(response_file),
            '-C', str(scratch), '-c', 'approval_policy="never"',
            '-c', 'developer_instructions=' + json.dumps(instructions, ensure_ascii=False),
            '-c', 'web_search=' + json.dumps(settings.web_search),
        ]
        if settings.model:
            args.extend(['--model', settings.model])
        for feature in settings.disabled_features:
            args.extend(['--disable', feature])
        if settings.disable_mcp_servers:
            codex_home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
            try:
                with (codex_home / 'config.toml').open('rb') as stream:
                    user_config = tomllib.load(stream)
            except FileNotFoundError:
                user_config = {}
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise SolverError(f'Cannot read Codex config: {exc}') from exc
            for name in user_config.get('mcp_servers', {}):
                # CLI override paths are split on dots; quotes become literal key characters.
                if '.' in name or '=' in name:
                    raise SolverError(f'Cannot override MCP server name {name!r}; rename it or disable this option.')
                args.extend(['-c', 'mcp_servers.' + name + '.enabled=false'])
        return args + ['-']

    def __call__(self, snapshot: Snapshot, marker: Marker) -> str:
        try:
            return self._solve(snapshot, marker)
        except SolverError:
            raise
        except OSError as exc:
            raise SolverError(f'Codex runtime failure: {exc}') from exc

    def _solve(self, snapshot: Snapshot, marker: Marker) -> str:
        settings = self.settings
        if self.stopping():
            raise SolverStopped('Worker stopped')
        # Read on every call so prompt edits take effect without restarting the worker.
        instructions = self.instructions(marker.cell_type)
        context = (
            f'Target: Cell {marker.cell_index + 1}, line {marker.line_index + 1}, type {marker.cell_type}.\n'
            f'Replace the entire first line containing {settings.marker!r}. Return only the replacement.\n\n'
            '<notebook_context>\n' + snapshot.context(settings.include_outputs) + '\n</notebook_context>\n'
        )
        if len(context) > settings.context_max_chars:
            raise SolverInputError(
                f'Notebook context has {len(context)} characters; limit is {settings.context_max_chars}. '
                'Notebook left unchanged; increase codex.context_max_chars to send the entire context.'
            )
        try:
            payload = context.encode('utf-8')
        except UnicodeError as exc:
            raise SolverInputError(f'Notebook context contains invalid Unicode: {exc}') from exc
        calls_dir = settings.runtime_dir / 'calls'
        try:
            calls_dir.mkdir(parents=True, exist_ok=True)
            if path_link(calls_dir):
                raise ConfigError('runtime/calls must not be a link or junction')
            recover_calls(calls_dir)
            if directory_bytes(calls_dir, settings.call_max_bytes) > settings.call_max_bytes:
                raise ConfigError('Codex scratch storage exceeds runtime.call_max_bytes; inspect abandoned files')
        except OSError as exc:
            raise SolverError(f'Cannot prepare Codex runtime: {exc}') from exc
        with tempfile.TemporaryDirectory(prefix='codex-', dir=calls_dir, ignore_cleanup_errors=True) as temporary:
            scratch = Path(temporary)
            (scratch / '.autocolab-owned').write_text(scratch.name, encoding='ascii')
            response_file = scratch / 'answer.txt'
            args = self.build_arguments(response_file, scratch, instructions)
            self.validate_arguments(args)
            job = None
            log_path = settings.runtime_dir / 'codex-last.log'
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            try:
                output = _BoundedLog(log_path, settings.codex_log_max_bytes)
            except OSError as exc:
                raise SolverError(f'Cannot open Codex log: {exc}') from exc
            try:
                job = WindowsJob() if os.name == 'nt' else None
                process = subprocess.Popen(gated_command(args) if job else args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=scratch, creationflags=flags,
                                           start_new_session=os.name != 'nt')
            except OSError as exc:
                output.stream.close()
                if job:
                    job.close()
                raise SolverError(f'Cannot start Codex: {exc}') from exc
            threads = []
            deadline = time.monotonic() + settings.timeout_seconds
            try:
                if job:
                    process._autocolab_job = job
                    job.assign(process)
                output.start(process.stdout)
                threads.append(output.thread)
                writer = _send_input(process, b'\0' + payload if job else payload)
                threads.append(writer)
                while True:
                    if self.stopping():
                        raise SolverStopped('Worker stopped while Codex was running')
                    if output.problem:
                        raise SolverError(f'{output.problem}; see {log_path}')
                    if directory_bytes(calls_dir, settings.call_max_bytes) > settings.call_max_bytes:
                        raise SolverError('Codex scratch storage exceeded runtime.call_max_bytes')
                    if time.monotonic() >= deadline:
                        raise SolverError(f'Codex timed out after {settings.timeout_seconds:g}s; see {log_path}')
                    try:
                        process.wait(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                # Children may have inherited a pipe even after the CLI exits. Do not wait forever.
                if job:
                    job.stop()
                elif os.name != 'nt':
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                cleanup_deadline = time.monotonic() + 5
                for thread in threads:
                    thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
                    if thread.is_alive():
                        raise SolverError(f'Codex exited but {thread.name} is still open; see {log_path}')
                if output.problem:
                    raise SolverError(f'{output.problem}; see {log_path}')
                if directory_bytes(calls_dir, settings.call_max_bytes) > settings.call_max_bytes:
                    raise SolverError('Codex scratch storage exceeded runtime.call_max_bytes')
            except BaseException as exc:
                cleanup_error = _stop_process_tree(process, threads)
                if cleanup_error:
                    raise SolverCleanupError(f'{exc}; cleanup did not complete: {cleanup_error}') from exc
                raise
            finally:
                if job:
                    job.close()
                if output.thread is None:
                    for stream in (output.stream, process.stdin, process.stdout):
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            if process.returncode:
                raise SolverError(f'Codex failed (exit {process.returncode}); see {log_path}')
            if not response_file.is_file():
                raise SolverResponseError(f'Codex did not write a final answer; see {log_path}')
            try:
                with response_file.open('rb') as stream:
                    raw = stream.read(4 * settings.context_max_chars + 4)
                if len(raw) > 4 * settings.context_max_chars + 3:
                    raise SolverResponseError('Codex final answer exceeded the configured context size limit')
                answer = raw.decode('utf-8-sig')
            except UnicodeError as exc:
                raise SolverResponseError(f'Cannot read Codex final answer: {exc}') from exc
            except OSError as exc:
                raise SolverError(f'Cannot read Codex final answer: {exc}') from exc
            if len(answer) > settings.context_max_chars:
                raise SolverResponseError('Codex final answer exceeded the configured context size limit')
            if not answer.strip():
                raise SolverResponseError(f'Codex returned an empty answer; see {log_path}')
            return answer
