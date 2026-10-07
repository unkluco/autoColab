"""One worker, one Codex invocation, one marker at a time."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time

from config import Settings
from config import ConfigError
from storage_safety import path_link, within_root, recover_temps
from notebooks import load_snapshot, save_if_unchanged
from solver import SolverCleanupError, SolverError, SolverInputError, SolverResponseError, SolverStopped

logger = logging.getLogger('autocolab')


def write_json(path: Path, data: dict, *, retry_delays_ms=(50, 100, 200)):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        # Reuse the complete temporary file; only retry the commit when a reader
        # briefly denies replacement. Disk/serialization errors propagate immediately.
        for attempt in range(len(retry_delays_ms) + 1):
            try:
                os.replace(name, path)
                break
            except OSError as exc:
                retryable = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in (5, 32, 33)
                if not retryable or attempt == len(retry_delays_ms):
                    raise
                time.sleep(retry_delays_ms[attempt] / 1000)
    finally:
        Path(name).unlink(missing_ok=True)


class InstanceLock:
    """OS-held lock, released automatically even if the worker crashes."""
    def __init__(self, runtime_dir: Path):
        self.path = Path(runtime_dir) / 'worker.lock'
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError('Another autocolab worker is already running with this runtime directory')
        return self

    def release(self):
        if self.stream is not None:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def worker_running(runtime_dir: Path):
    lock = InstanceLock(runtime_dir)
    try:
        lock.acquire()
    except RuntimeError:
        return True
    else:
        lock.release()
        return False


class Worker:
    def __init__(self, settings: Settings, solver, stop_event=None):
        self.settings = settings
        self.solver = solver
        self.stop_event = stop_event or threading.Event()
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.failures = {}
        self.conflicts = {}
        self.blocked_path = self.settings.runtime_dir / 'blocked-notebooks.json'
        self.blocked = {}
        if self.blocked_path.exists():
            try:
                if self.blocked_path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('block registry too large')
                self.blocked = json.loads(self.blocked_path.read_text(encoding='utf-8'))
                if not isinstance(self.blocked, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str) or
                    re.fullmatch(r'[0-9a-f]{64}', value) is None for key, value in self.blocked.items()):
                    raise ValueError('invalid block registry')
            except (OSError, ValueError) as exc:
                raise ConfigError(f'Cannot read marker-loop protection registry: {exc}') from exc
        self.next_artifact_cleanup = 0
        self.global_backoff_seconds = 0
        self.global_backoff_until = 0
        self.last_status_warning = None
        self.status = {
            'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
            'watch_folder': str(settings.watch_folder), 'state': 'starting',
            'saved': 0, 'conflicts': 0, 'failures': 0,
        }

    def update_status(self, **fields):
        self.status.update(fields)
        self.status['updated_at'] = datetime.now(timezone.utc).isoformat()
        try:
            write_json(self.settings.runtime_dir / 'status.json', self.status,
                       retry_delays_ms=self.settings.status_retry_delays_ms)
            self.last_status_warning = None
            return True
        except OSError as exc:
            now = time.monotonic()
            if self.last_status_warning is None or now - self.last_status_warning >= 60:
                logger.warning('Cannot update status; work continues: %s', exc)
                self.last_status_warning = now
            return False

    def stopping(self):
        if self.stop_event.is_set():
            return True
        try:
            return (self.settings.runtime_dir / 'stop.request').exists()
        except OSError:
            return False

    def list_notebooks(self):
        watch_folder = self.settings.watch_folder
        if not watch_folder.is_dir():
            raise FileNotFoundError(f'Watch folder is unavailable: {watch_folder}')
        runtime_dir = self.settings.runtime_dir.resolve()
        paths = []

        def excluded(path):
            resolved = path.resolve()
            return (resolved == runtime_dir or runtime_dir in resolved.parents or
                    not within_root(path, watch_folder))

        def walk_error(exc):
            logger.warning('Cannot scan a subfolder: %s', exc)

        for root, dirs, files in os.walk(watch_folder, followlinks=False, onerror=walk_error):
            root = Path(root)
            dirs[:] = sorted(name for name in dirs if name != '.ipynb_checkpoints' and
                             not path_link(root / name) and not excluded(root / name))
            for name in files:
                path = root / name
                if path.suffix.lower() == '.ipynb' and not path_link(path) and not excluded(path):
                    paths.append(path)
            if not self.settings.recursive:
                dirs.clear()
        return sorted(paths, key=lambda path: (str(path.relative_to(watch_folder)).casefold(), str(path)))

    def scan_once(self):
        if self.stopping():
            return 'stopped'
        remaining = self.global_backoff_until - time.monotonic()
        if remaining > 0:
            self.update_status(state='retrying_codex', retry_in_seconds=round(remaining, 3))
            return 'backoff'
        self.update_status(state='scanning', current_file=None)
        had_failure = False
        try:
            paths = self.list_notebooks()
            if time.monotonic() >= self.next_artifact_cleanup:
                recover_temps(self.settings.runtime_dir / 'artifacts',
                              (self.settings.watch_folder, self.settings.runtime_dir))
                self.next_artifact_cleanup = time.monotonic() + 60
        except OSError as exc:
            logger.error('%s', exc)
            self.update_status(state='waiting_for_folder', error=str(exc))
            return 'failed'
        # Forget errors for deleted files; retries apply only to the same content version.
        present = set(paths)
        self.failures = {path: value for path, value in self.failures.items() if path in present}
        self.conflicts = {path: value for path, value in self.conflicts.items() if path in present}
        for path in paths:
            if self.stopping():
                return 'stopped'
            identity = None
            try:
                conflict = self.conflicts.get(path)
                if conflict and time.monotonic() < conflict[1]:
                    continue
                cooldown = self.failures.get(path)
                if cooldown and cooldown[0] is None and time.monotonic() < cooldown[1]:
                    continue
                # A stat identity also lets malformed/uploading files cool down without recurring log noise.
                stat = path.stat()
                identity = (stat.st_mtime_ns, stat.st_size)
                cooldown = self.failures.get(path)
                if cooldown and cooldown[0] == identity and time.monotonic() < cooldown[1]:
                    continue
                if not within_root(path, self.settings.watch_folder):
                    continue
                snapshot = load_snapshot(path, max_bytes=self.settings.notebook_max_bytes)
                blocked = self.blocked.get(str(path))
                if blocked == snapshot.digest:
                    continue
                if blocked is not None:
                    del self.blocked[str(path)]
                    write_json(self.blocked_path, self.blocked)
                marker = snapshot.first_marker(self.settings.marker, self.settings.cell_types)
                if marker is None:
                    self.failures.pop(path, None)
                    self.conflicts.pop(path, None)
                    continue
                logger.info('Solving %s | cell %d (%s), line %d', path, marker.cell_index + 1,
                            marker.cell_type, marker.line_index + 1)
                self.update_status(state='solving', current_file=str(path), cell=marker.cell_index + 1,
                                   cell_type=marker.cell_type, line=marker.line_index + 1, error=None)
                answer = self.solver(snapshot, marker)
                self.global_backoff_seconds = 0
                self.global_backoff_until = 0
                if self.stopping():
                    return 'stopped'
                if self.settings.marker in answer:
                    # Persist before continuing, so restarts cannot reset this guard.
                    self.blocked[str(path)] = snapshot.digest
                    write_json(self.blocked_path, self.blocked)
                    (self.settings.runtime_dir / 'rejected-answer.txt').write_text(answer, encoding='utf-8')
                    logger.error('Answer still contains marker; pause this notebook until its content changes: %s', path)
                    self.update_status(state='blocked_marker', current_file=str(path),
                                       error='Answer contains active marker; edit notebook to retry')
                    had_failure = True
                    continue
                updated = snapshot.replace(marker, answer)
                backup_dir = self.settings.runtime_dir / 'backups' if self.settings.backup_enabled else None
                if not save_if_unchanged(snapshot, updated, backup_dir,
                                         backup_max_count=self.settings.backup_max_count,
                                         backup_max_bytes=self.settings.backup_max_bytes,
                                         backup_max_age_days=self.settings.backup_max_age_days,
                                         journal_dir=self.settings.runtime_dir / 'artifacts',
                                         scope_root=self.settings.watch_folder):
                    logger.info('File changed while Codex was answering; discard answer and rescan: %s', path)
                    self.failures.pop(path, None)
                    count = self.conflicts.get(path, (0, 0))[0] + 1
                    deadline = 0
                    if count >= self.settings.conflict_limit:
                        deadline = time.monotonic() + self.settings.conflict_seconds
                        count = 0
                        logger.warning('Repeated edits; defer %s for %.1fs and allow other files',
                                       path, self.settings.conflict_seconds)
                    self.conflicts[path] = (count, deadline)
                    self.update_status(state='conflict', conflicts=self.status['conflicts'] + 1)
                    return 'conflict'
                self.failures.pop(path, None)
                self.conflicts.pop(path, None)
                logger.info('Saved %s | %d answer characters', path, len(answer))
                self.update_status(state='saved', saved=self.status['saved'] + 1)
                return 'saved'
            except SolverStopped:
                return 'stopped'
            except SolverCleanupError:
                # The supervisor must drain the entire process group before another attempt.
                raise
            except ConfigError:
                raise
            except SolverError as exc:
                if isinstance(exc, (SolverInputError, SolverResponseError)):
                    had_failure = True
                    self.failures[path] = (identity, time.monotonic() + self.settings.retry_seconds)
                    logger.error('Cannot process %s: %s', path, exc)
                    self.update_status(state='failed', current_file=str(path), error=str(exc),
                                       failures=self.status['failures'] + 1)
                    continue
                self.global_backoff_seconds = min(
                    self.settings.global_max_seconds,
                    self.global_backoff_seconds * 2 if self.global_backoff_seconds
                    else self.settings.global_initial_seconds)
                delay = min(self.settings.global_max_seconds,
                            random.uniform(self.global_backoff_seconds * 0.8,
                                           self.global_backoff_seconds * 1.2))
                self.global_backoff_until = time.monotonic() + delay
                # At recovery, another candidate can prove the service is healthy.
                self.failures[path] = (identity, self.global_backoff_until + self.settings.retry_seconds)
                logger.error('Codex failed; pause all calls for %.1fs: %s', delay, exc)
                self.update_status(state='retrying_codex', current_file=str(path), error=str(exc),
                                   failures=self.status['failures'] + 1, retry_in_seconds=round(delay, 3))
                return 'backoff'
            except Exception as exc:
                had_failure = True
                self.failures[path] = (identity, time.monotonic() + self.settings.retry_seconds)
                logger.error('Cannot process %s: %s', path, exc, exc_info=logger.isEnabledFor(logging.DEBUG))
                self.update_status(state='failed', current_file=str(path), error=str(exc),
                                   failures=self.status['failures'] + 1)
                # Keep the marker untouched; allow other files to proceed.
        if not had_failure:
            self.update_status(state='idle', current_file=None, error=None)
        return 'failed' if had_failure else 'idle'

    def run(self):
        logger.info('Worker started | %s | pause %.2f–%.2fs', self.settings.watch_folder,
                    self.settings.min_seconds, self.settings.max_seconds)
        try:
            while not self.stopping():
                result = self.scan_once()
                if result == 'stopped':
                    break
                if result == 'conflict':
                    continue
                delay = random.uniform(self.settings.min_seconds, self.settings.max_seconds)
                if result == 'backoff':
                    delay = max(delay, self.global_backoff_until - time.monotonic())
                logger.debug('Next scan in %.3fs', delay)
                if result not in ('backoff', 'failed'):
                    self.update_status(state='waiting', next_scan_seconds=round(delay, 3))
                else:
                    self.update_status(next_scan_seconds=round(delay, 3))
                deadline = time.monotonic() + delay
                next_heartbeat = time.monotonic() + 5
                while not self.stopping() and time.monotonic() < deadline:
                    self.stop_event.wait(min(0.5, max(0, deadline - time.monotonic())))
                    if time.monotonic() >= next_heartbeat:
                        self.update_status()
                        next_heartbeat = time.monotonic() + 5
        finally:
            self.update_status(state='stopped', current_file=None)
            logger.info('Worker stopped')
