"""Exercise the Windows console supervisor with isolated fake workers only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


CONFIG_HELPER = r'''
import json
from pathlib import Path
import sys
config = json.loads(Path(sys.argv[-1]).read_text())
if config['mode'] == 'invalid_config':
    print('Invalid fixture configuration')
    raise SystemExit(3)
if config['mode'] == 'query_hang':
    import os
    import time
    root = Path(__file__).parent
    (root / 'query.pid').write_text(str(os.getpid()))
    (root / 'query-supervisor.pid').write_text(str(os.getppid()))
    while True:
        time.sleep(.1)
print(json.dumps(config['supervisor']))
'''

CHILD_FIXTURE = r'''
import os
from pathlib import Path
import subprocess
import sys
import time
root = Path(__file__).parent
attempt, role = sys.argv[1:]
(root / f'attempt-{attempt}-{role}.pid').write_text(str(os.getpid()))
print('FIXTURE ' + role + ' log', flush=True)
if role == 'child':
    subprocess.Popen([sys.executable, '-u', __file__, attempt, 'grandchild'])
while True:
    time.sleep(.1)
'''

BOOTSTRAP_FIXTURE = r'''
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
root = Path(__file__).parent
config = json.loads(Path(sys.argv[-1]).read_text())
records = root / 'attempts.jsonl'
attempt = len(records.read_text().splitlines()) + 1 if records.exists() else 1
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
previous_live = []
for pid_file in root.glob('attempt-*.pid'):
    handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
    if handle:
        if kernel.WaitForSingleObject(handle, 0) == 0x102:
            previous_live.append(pid_file.name)
        kernel.CloseHandle(handle)
with records.open('a') as stream:
    stream.write(json.dumps({'attempt': attempt, 'pid': os.getpid(),
        'supervisor_pid': os.getppid(), 'previous_live': previous_live,
        'time': time.monotonic()}) + '\n')
(root / f'attempt-{attempt}-bootstrap.pid').write_text(str(os.getpid()))
mode = config['mode']
if mode == 'exit2':
    raise SystemExit(2)
if mode == 'exit3':
    raise SystemExit(3)
subprocess.Popen([sys.executable, '-u', str(root / 'child.py'), str(attempt), 'child'])
until = time.monotonic() + 5
while not (root / f'attempt-{attempt}-grandchild.pid').exists():
    if time.monotonic() > until:
        raise RuntimeError('fixture descendants did not start')
    time.sleep(.02)
runtime = Path(config['supervisor']['runtime_dir'])
runtime.mkdir(exist_ok=True)
started = datetime.now(timezone.utc).isoformat()
instance = str(uuid.uuid4())
def status():
    data = {'pid': os.getpid(), 'instance_id': instance, 'started_at': started,
            'updated_at': datetime.now(timezone.utc).isoformat(), 'state': 'fixture'}
    temporary = runtime / 'new-status.json'
    temporary.write_text(json.dumps(data))
    try:
        os.replace(temporary, runtime / 'status.json')
    except PermissionError:
        # Windows can briefly refuse atomic replacement while another reader
        # is closing. Production status writes are also best effort.
        pass
if mode in ('crash_then_success', 'backoff') and attempt == 1:
    time.sleep(.2)
    raise SystemExit(7)
if mode == 'backoff_steps' and attempt <= 4:
    time.sleep(.2)
    raise SystemExit(7)
if mode == 'backoff_reset' and attempt <= 3:
    duration = 1.3 if attempt == 3 else .2
    until = time.monotonic() + duration
    while time.monotonic() < until:
        status()
        time.sleep(.05)
    raise SystemExit(7)
if mode in ('hang', 'missing', 'malformed', 'foreign') and attempt == 1:
    if mode == 'hang':
        status()
    if mode == 'malformed':
        (runtime / 'status.json').write_text('partial json')
    while True:
        time.sleep(.1)
duration = 2.2 if mode == 'heartbeat' else .8
if mode in ('running', 'supervisor_death'):
    duration = 60
until = time.monotonic() + duration
while time.monotonic() < until:
    status()
    time.sleep(.1)
raise SystemExit(0)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows JobObject supervision')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='autocolab-launcher-')
        self.root = Path(self.temporary.name)
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'run_host.ps1', self.root / 'run_host.ps1')
        launcher = self.root / 'run_host.ps1'
        launcher.write_text(launcher.read_text(encoding='utf-8').replace(
            r'Global\AutoColabConsoleSupervisor_v1',
            'Global\\AutoColabTestSupervisor_' + uuid.uuid4().hex), encoding='utf-8')
        (self.root / 'config.py').write_text(CONFIG_HELPER, encoding='utf-8')
        (self.root / 'bootstrap.py').write_text(BOOTSTRAP_FIXTURE, encoding='utf-8')
        (self.root / 'child.py').write_text(CHILD_FIXTURE, encoding='utf-8')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handles = []
        self.process = None
        self.log = None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 5000) == 0x102:
                # Only held handles opened for this test's recorded fixture PIDs.
                self.kernel.TerminateProcess(handle, 1)
            self.kernel.CloseHandle(handle)
        if self.log:
            self.log.close()
        # A Windows process handle can signal exit just before all inherited
        # file handles finish closing. Retry cleanup briefly, but never ignore
        # a surviving process or permanently locked fixture directory.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def launch(self, mode, through_cmd=False, **settings):
        supervisor = {'runtime_dir': str(self.root / 'runtime'),
                      'restart_initial_seconds': .1, 'restart_max_seconds': .4,
                      'restart_reset_seconds': 5, 'watchdog_seconds': 1.5}
        supervisor.update(settings)
        (self.root / 'config.toml').write_text(json.dumps({'mode': mode, 'supervisor': supervisor}))
        command = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-File', str(self.root / 'run_host.ps1'), '-PythonPath', sys.executable]
        if through_cmd:
            command = ['cmd.exe', '/d', '/c', subprocess.list2cmdline(command)]
        self.log = (self.root / 'console.log').open('w', encoding='utf-8')
        self.process = subprocess.Popen(command, cwd=self.root, stdout=self.log,
                                        stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        return supervisor

    def wait_until(self, predicate, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            if self.process and self.process.poll() is not None:
                self.fail('Supervisor exited before fixture was ready:\n' + self.output())
            time.sleep(.03)
        self.fail('Fixture timeout:\n' + self.output())

    def output(self):
        return (self.root / 'console.log').read_text(encoding='utf-8', errors='replace')

    def attempts(self):
        path = self.root / 'attempts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def hold_process(self, pid):
        handle = self.kernel.OpenProcess(0x100000 | 1, False, pid)
        self.assertTrue(handle, f'Cannot hold fixture process {pid}:\n{self.output()}')
        self.handles.append(handle)
        return handle

    def hold_first_attempt(self):
        self.wait_until(lambda: (self.root / 'attempt-1-grandchild.pid').exists())
        for role in ('bootstrap', 'child', 'grandchild'):
            self.hold_process(int((self.root / f'attempt-1-{role}.pid').read_text()))

    def assert_stopped(self, handles=None):
        for handle in self.handles if handles is None else handles:
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0,
                             'Fixture process survived shutdown:\n' + self.output())

    def test_crash_restarts_after_descendants_stop(self):
        self.launch('crash_then_success')
        self.hold_first_attempt()
        self.assertEqual(self.process.wait(timeout=15), 0, self.output())
        attempts = self.attempts()
        self.assertEqual(len(attempts), 2, self.output())
        self.assertEqual(attempts[1]['previous_live'], [], self.output())
        self.assert_stopped()
        self.assertIn('FIXTURE grandchild log', self.output())
        self.assertIn('Host exited with code 7.', self.output())

    def test_duplicate_does_not_restart(self):
        self.launch('exit2')
        self.assertEqual(self.process.wait(timeout=15), 2, self.output())
        self.assertEqual(len(self.attempts()), 1)

    def test_restart_backoff_increases_to_configured_cap(self):
        self.launch('backoff_steps')
        self.assertEqual(self.process.wait(timeout=20), 0, self.output())
        self.assertEqual(len(self.attempts()), 5)
        self.assertEqual([record['previous_live'] for record in self.attempts()], [[], [], [], [], []])
        retries = [line for line in self.output().splitlines() if 'Retry in ' in line]
        self.assertEqual([line.rsplit('Retry in ', 1)[1] for line in retries],
                         ['0.1 seconds.', '0.2 seconds.', '0.4 seconds.', '0.4 seconds.'])

    def test_stable_attempt_resets_restart_backoff(self):
        self.launch('backoff_reset', restart_reset_seconds=1)
        self.assertEqual(self.process.wait(timeout=20), 0, self.output())
        self.assertEqual(len(self.attempts()), 4)
        retries = [line for line in self.output().splitlines() if 'Retry in ' in line]
        self.assertEqual([line.rsplit('Retry in ', 1)[1] for line in retries],
                         ['0.1 seconds.', '0.2 seconds.', '0.1 seconds.'])

    def test_permanent_worker_config_error_does_not_restart(self):
        self.launch('exit3')
        self.assertEqual(self.process.wait(timeout=15), 3, self.output())
        self.assertEqual(len(self.attempts()), 1)

    def test_invalid_launcher_config_never_starts_host(self):
        self.launch('invalid_config')
        self.assertEqual(self.process.wait(timeout=15), 3, self.output())
        self.assertEqual(self.attempts(), [])

    def test_watchdog_restarts_hung_host_and_children(self):
        self.launch('hang', watchdog_seconds=.8)
        self.hold_first_attempt()
        self.assertEqual(self.process.wait(timeout=15), 0, self.output())
        self.assertEqual(len(self.attempts()), 2, self.output())
        self.assertEqual(self.attempts()[1]['previous_live'], [])
        self.assert_stopped()
        self.assertIn('No valid host progress', self.output())

    def test_watchdog_recovers_missing_and_malformed_status(self):
        for mode in ('missing', 'malformed'):
            with self.subTest(mode=mode):
                # Each subcase uses its own fixture directory and supervisor.
                if mode == 'malformed':
                    self.tearDown()
                    self.setUp()
                self.launch(mode, watchdog_seconds=.8)
                self.hold_first_attempt()
                self.assertEqual(self.process.wait(timeout=15), 0, self.output())
                self.assertEqual(len(self.attempts()), 2, self.output())
                self.assert_stopped()

    def test_foreign_status_updates_cannot_feed_watchdog(self):
        settings = self.launch('foreign', watchdog_seconds=.8)
        runtime = Path(settings['runtime_dir'])
        runtime.mkdir(exist_ok=True)
        stop = threading.Event()
        def write_foreign_status():
            while not stop.wait(.08):
                stamp = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                data = {'pid': os.getpid(), 'instance_id': 'other-instance',
                        'started_at': stamp, 'updated_at': stamp}
                temporary = runtime / 'foreign.tmp'
                try:
                    temporary.write_text(json.dumps(data))
                    os.replace(temporary, runtime / 'status.json')
                except OSError:
                    pass
        writer = threading.Thread(target=write_foreign_status)
        writer.start()
        try:
            self.hold_first_attempt()
            self.wait_until(lambda: len(self.attempts()) >= 2)
        finally:
            stop.set()
            writer.join(timeout=3)
        self.assertEqual(self.process.wait(timeout=15), 0, self.output())
        self.assertEqual(len(self.attempts()), 2)
        self.assert_stopped()

    def test_regular_progress_keeps_one_host(self):
        self.launch('heartbeat', watchdog_seconds=.8)
        self.assertEqual(self.process.wait(timeout=15), 0, self.output())
        self.assertEqual(len(self.attempts()), 1, self.output())
        self.assertNotIn('No valid host progress', self.output())

    def test_console_parent_death_during_backoff_stops_supervisor(self):
        self.launch('backoff', through_cmd=True, restart_initial_seconds=10,
                    restart_max_seconds=10)
        self.wait_until(lambda: self.attempts())
        supervisor = self.hold_process(self.attempts()[0]['supervisor_pid'])
        self.wait_until(lambda: 'Retry in 10 seconds' in self.output())
        self.process.terminate()  # Only CMD dies; supervisor observes its held parent handle.
        self.process.wait(timeout=10)
        self.assert_stopped([supervisor])
        self.assertEqual(len(self.attempts()), 1)

    def test_second_supervisor_is_rejected_during_backoff(self):
        self.launch('backoff', restart_initial_seconds=3, restart_max_seconds=3)
        self.wait_until(lambda: 'Retry in 3 seconds' in self.output())
        alternate = self.root / 'alternate-config.toml'
        settings = json.loads((self.root / 'config.toml').read_text())
        settings['supervisor']['runtime_dir'] = str(self.root / 'other-runtime')
        alternate.write_text(json.dumps(settings))
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy',
                                 'Bypass', '-File', str(self.root / 'run_host.ps1'),
                                 '-PythonPath', sys.executable, '-ConfigFile', str(alternate)],
                                cwd=self.root, capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('supervisor is already running', result.stdout)
        self.assertFalse((self.root / 'other-runtime').exists())
        self.assertEqual(self.process.wait(timeout=15), 0, self.output())
        self.assertEqual(len(self.attempts()), 2)

    def test_console_parent_death_during_settings_query_stops_helper(self):
        self.launch('query_hang', through_cmd=True)
        self.wait_until(lambda: (self.root / 'query-supervisor.pid').exists())
        self.hold_process(int((self.root / 'query.pid').read_text()))
        self.hold_process(int((self.root / 'query-supervisor.pid').read_text()))
        self.process.terminate()
        self.process.wait(timeout=10)
        self.assert_stopped()
        self.assertEqual(self.attempts(), [])

    def test_console_parent_death_stops_running_descendants(self):
        self.launch('running', through_cmd=True)
        self.hold_first_attempt()
        supervisor = self.hold_process(self.attempts()[0]['supervisor_pid'])
        self.process.terminate()
        self.process.wait(timeout=10)
        self.assert_stopped()

    def test_supervisor_death_stops_running_descendants(self):
        self.launch('supervisor_death')
        self.hold_first_attempt()
        self.process.terminate()
        self.process.wait(timeout=10)
        self.assert_stopped()


if __name__ == '__main__':
    unittest.main()
