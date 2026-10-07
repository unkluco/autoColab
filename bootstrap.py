"""Prepare the local .venv and its dependencies before launching the host."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import venv

ROOT = Path(__file__).resolve().parent
ENV_DIR = ROOT / '.venv'
ENV_PYTHON = ENV_DIR / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
WORK_DIR = ROOT / 'work'


class SetupLock:
    def __enter__(self):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.stream = (WORK_DIR / 'setup.lock').open('a+b')
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b'0')
            self.stream.flush()
        deadline = time.monotonic() + 60
        while True:
            self.stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.stream.close()
                    raise RuntimeError('Environment setup is busy. Try again shortly.')
                time.sleep(0.25)

    def __exit__(self, *args):
        if os.name == 'nt':
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def prepare_environment():
    if sys.version_info < (3, 11):
        raise RuntimeError('Install Python 3.11 or newer to create the AutoColab environment.')
    with SetupLock():
        log_path = WORK_DIR / 'setup.log'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with log_path.open('w', encoding='utf-8') as output:
            def checked(args):
                result = subprocess.run(args, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                        creationflags=flags, timeout=180)
                output.flush()
                if result.returncode:
                    raise RuntimeError(f'Environment setup failed; see {log_path}')

            healthy = False
            if ENV_PYTHON.is_file():
                try:
                    result = subprocess.run([str(ENV_PYTHON), '-c',
                        'import sys,tomllib,json,pathlib,subprocess; assert sys.version_info >= (3,11)'],
                        stdout=output, stderr=subprocess.STDOUT, creationflags=flags, timeout=20)
                    healthy = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired) as exc:
                    output.write(f'Existing environment needs repair: {exc}\n')
                    output.flush()
            if not healthy:
                # Reuse/repair only this project's .venv; never install into global Python.
                venv.EnvBuilder(with_pip=True).create(ENV_DIR)
            result = subprocess.run([str(ENV_PYTHON), '-c', 'import pip'], stdout=output,
                                    stderr=subprocess.STDOUT, creationflags=flags, timeout=20)
            if result.returncode:
                checked([str(ENV_PYTHON), '-m', 'ensurepip', '--upgrade'])
            # An idempotent install verifies requirements on every startup and installs missing packages.
            checked([str(ENV_PYTHON), '-m', 'pip', 'install', '--disable-pip-version-check',
                     '--no-input', '-r', str(ROOT / 'requirements.txt')])
            checked([str(ENV_PYTHON), '-m', 'pip', 'check'])
            checked([str(ENV_PYTHON), '-c', 'import config,notebooks,solver,worker,single_instance,controller,storage_safety,process_job'])
        return ENV_PYTHON


def main():
    if '--status' in sys.argv[1:] or '--stop' in sys.argv[1:]:
        from controller import main as control_main
        return control_main(sys.argv[1:])
    try:
        interpreter = prepare_environment()
        if sys.argv[1:] == ['--prepare-only']:
            print(f'Environment ready: {interpreter}')
            return 0
        environment = os.environ.copy()
        environment['AUTOCOLAB_BOOTSTRAPPED'] = str(ROOT)
        environment['PYTHONUTF8'] = '1'
        child = subprocess.Popen([str(interpreter), str(ROOT / 'main.py'), *sys.argv[1:]],
                                 env=environment)
        # The console delivers Ctrl+C to the child too; allow its graceful stop to finish.
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                continue
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'Environment error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
