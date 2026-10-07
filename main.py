"""CLI entry point for the local notebook worker (Python 3.11+)."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent


def launch_through_environment():
    if os.environ.get('AUTOCOLAB_BOOTSTRAPPED') == str(ROOT) and Path(sys.prefix).resolve() == (ROOT / '.venv').resolve():
        return None
    child = subprocess.Popen([sys.executable, str(ROOT / 'bootstrap.py'), *sys.argv[1:]])
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue


# Prepare dependencies before importing worker modules, including future third-party libraries.
if __name__ == '__main__':
    if '--status' in sys.argv[1:] or '--stop' in sys.argv[1:]:
        from controller import main as control_main
        raise SystemExit(control_main(sys.argv[1:]))
    environment_result = launch_through_environment()
    if environment_result is not None:
        raise SystemExit(environment_result)

from config import ConfigError, load_settings
from notebooks import load_snapshot
from solver import CodexSolver, SolverError
from worker import InstanceLock, Worker
from single_instance import DuplicateInstance, MachineInstance


def setup_logging(settings, quiet=False):
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('autocolab')
    logger.setLevel(settings.log_level)
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
    log = RotatingFileHandler(settings.runtime_dir / 'worker.log', maxBytes=settings.log_max_bytes,
                              backupCount=settings.log_backup_count, encoding='utf-8')
    log.setFormatter(formatter)
    logger.addHandler(log)
    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if '--status' in arguments or '--stop' in arguments:
        from controller import main as control_main
        return control_main(arguments)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Fill the first @bot line in a synced notebook, sequentially.')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.toml')
    parser.add_argument('--watch-folder', type=Path, help='Override watch folder (useful for sample notebooks)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='Process at most one marker, then exit')
    mode.add_argument('--dry-run', action='store_true', help='List candidates; do not call Codex or edit notebooks')
    mode.add_argument('--check', action='store_true', help='Check config, folder, CLI, and login without solving')
    mode.add_argument('--status', action='store_true', help='Show worker status')
    mode.add_argument('--stop', action='store_true', help='Request a graceful stop (including active Codex call)')
    mode.add_argument('--runtime-dir', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--quiet', action='store_true', help='Write operational logs to file only')
    parser.add_argument('--config-digest', default='', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config, args.watch_folder)
        if args.config_digest and settings.config_digest != args.config_digest:
            raise ConfigError('Configuration changed during launch; close and reopen the server')
        if args.runtime_dir:
            print(settings.runtime_dir)
            return 0
        if (args.check or args.dry_run or args.once) and not settings.watch_folder.is_dir():
            print(f'Watch folder is unavailable: {settings.watch_folder}', file=sys.stderr)
            return 1
        if settings.watch_folder == settings.runtime_dir or settings.runtime_dir in settings.watch_folder.parents:
            raise ConfigError('runtime.directory must not equal or contain watch_folder')
        if args.check:
            solver = CodexSolver(settings)
            print('Config OK; three prompt files found.')
            print(f'Watch folder: {settings.watch_folder}')
            print(f'Scan pause: {settings.min_seconds:g}–{settings.max_seconds:g} seconds')
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            for tail in (['--version'], ['login', 'status']):
                result = subprocess.run(solver.command + tail, capture_output=True, encoding='utf-8', errors='replace',
                                        timeout=30, creationflags=flags)
                print((result.stdout + result.stderr).strip())
                if result.returncode:
                    return 1
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        if args.dry_run:
            # Inspection is read-only and can run alongside a live host.
            worker = Worker(settings, None, stop)
            count = 0
            for path in worker.list_notebooks():
                try:
                    marker = load_snapshot(path, max_bytes=settings.notebook_max_bytes).first_marker(settings.marker, settings.cell_types)
                    if marker:
                        count += 1
                        print(f'{path} | cell {marker.cell_index + 1} ({marker.cell_type}), line {marker.line_index + 1}')
                except (OSError, ValueError) as exc:
                    print(f'Cannot inspect {path}: {exc}', file=sys.stderr)
            print(f'{count} notebook(s) with a marker. No model call; no notebook modified.')
            return 0
        with MachineInstance(settings.runtime_dir, settings.watch_folder) as instance, InstanceLock(settings.runtime_dir):
            setup_logging(settings, args.quiet)
            solver = CodexSolver(settings, stop)
            worker = Worker(settings, solver, stop)
            worker.status['instance_id'] = instance.owner['instance_id']
            worker.update_status(state='starting')
            if args.once:
                result = worker.scan_once()
                worker.update_status(state='stopped', last_result=result, current_file=None)
                print(f'Result: {result}')
                return 1 if result in ('failed', 'backoff') else 0
            worker.run()
            return 0
    except DuplicateInstance as exc:
        print(f'Already running: {exc}', file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f'Configuration error: {exc}', file=sys.stderr)
        return 3
    except (SolverError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
