"""Real process tests with isolated mutex names; no Drive or model access."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from single_instance import DEFAULT_NAME, machine_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Windows virtual-environment python.exe can be a redirector with another PID.
# Use its base interpreter so hard-kill tests target the actual mutex owner.
CHILD_PYTHON = getattr(sys, '_base_executable', sys.executable)
CHILD = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from single_instance import DuplicateInstance, MachineInstance
try:
    with MachineInstance(Path(sys.argv[3]), Path(sys.argv[4]), name=sys.argv[5],
                         owner_path=Path(sys.argv[6])) as instance:
        print(json.dumps(instance.owner), flush=True)
        if sys.argv[2] == 'hold':
            sys.stdin.readline()
except DuplicateInstance as exc:
    print(str(exc), file=sys.stderr, flush=True)
    sys.exit(2)
'''


class MachineInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='autocolab-instance-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner_path = self.root / 'shared' / 'host.json'
        self.name = 'Global\\AutoColab_test_' + uuid.uuid4().hex
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def command(self, mode, runtime):
        return [CHILD_PYTHON, '-c', CHILD, str(PROJECT_ROOT), mode, str(runtime),
                str(self.root / 'watch'), self.name, str(self.owner_path)]

    def start_host(self, runtime):
        child = subprocess.Popen(self.command('hold', runtime), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding='utf-8')
        self.children.append(child)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        try:
            line = ready.get(timeout=15)
        except queue.Empty:
            child.kill()
            output, error = child.communicate(timeout=10)
            self.fail(f'Host readiness timed out: {output} {error}')
        if not line:
            output, error = child.communicate(timeout=10)
            self.fail(f'Host failed before ready: {output} {error}')
        return child, json.loads(line)

    def status(self):
        return machine_status(name=self.name, owner_path=self.owner_path)

    def stop_host(self, child):
        child.communicate(input='stop\n', timeout=15)
        self.assertEqual(child.returncode, 0)

    def test_global_name_is_independent_of_project_and_config(self):
        self.assertEqual(DEFAULT_NAME, 'Global\\AutoColabHost_v1')

    def test_different_runtime_cannot_start_second_host(self):
        runtime = self.root / 'runtime-a'
        child, owner = self.start_host(runtime)
        result = subprocess.run(self.command('try', self.root / 'copied-project' / 'runtime-b'),
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('already running', result.stderr)
        self.assertFalse((self.root / 'copied-project' / 'runtime-b').exists())
        running, active = self.status()
        self.assertTrue(running)
        self.assertEqual(active['instance_id'], owner['instance_id'])
        self.assertEqual(active['pid'], child.pid)
        self.assertEqual(active['runtime_dir'], str(runtime.resolve()))

    def test_graceful_release_and_reacquire(self):
        child, owner = self.start_host(self.root / 'runtime-a')
        self.stop_host(child)
        self.assertFalse(self.owner_path.exists())
        self.assertEqual(self.status(), (False, None))
        replacement, next_owner = self.start_host(self.root / 'runtime-b')
        self.assertNotEqual(owner['instance_id'], next_owner['instance_id'])
        self.assertEqual(self.status()[1]['pid'], replacement.pid)

    def test_abrupt_exit_releases_mutex_and_stale_owner_is_ignored(self):
        child, _ = self.start_host(self.root / 'runtime-a')
        child.kill()
        child.communicate(timeout=10)
        self.assertTrue(self.owner_path.exists())
        self.assertEqual(self.status(), (False, None))
        next_child, next_owner = self.start_host(self.root / 'runtime-b')
        self.assertEqual(self.status()[1]['instance_id'], next_owner['instance_id'])
        self.assertEqual(next_owner['pid'], next_child.pid)

    def test_idle_stale_descriptor_does_not_report_running(self):
        self.owner_path.parent.mkdir()
        self.owner_path.write_text('{"pid": 999999, "runtime_dir": "stale"}', encoding='utf-8')
        self.assertEqual(self.status(), (False, None))

    def test_busy_mutex_with_missing_or_stale_process_identity(self):
        child, owner = self.start_host(self.root / 'runtime')
        self.owner_path.unlink()
        self.assertEqual(self.status(), (True, None))
        stale = dict(owner, process_start_id='windows:incorrect-creation-time')
        self.owner_path.write_text(json.dumps(stale), encoding='utf-8')
        self.assertEqual(self.status(), (True, None))
        self.assertIsNone(child.poll())

    def test_old_stop_request_is_cleared_before_owner_is_published(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'stop.request').write_text('old request', encoding='utf-8')
        self.start_host(runtime)
        self.assertFalse((runtime / 'stop.request').exists())
        self.assertTrue(self.status()[0])

    def test_release_does_not_delete_descriptor_from_another_instance(self):
        child, owner = self.start_host(self.root / 'runtime')
        other = dict(owner, instance_id=str(uuid.uuid4()))
        self.owner_path.write_text(json.dumps(other), encoding='utf-8')
        self.stop_host(child)
        self.assertEqual(json.loads(self.owner_path.read_text())['instance_id'], other['instance_id'])
        self.assertEqual(self.status(), (False, None))


if __name__ == '__main__':
    unittest.main()
