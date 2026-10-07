"""Controller independence and malformed telemetry; no real host touched."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import controller
import main


class ControllerTests(unittest.TestCase):
    def test_stop_ignores_broken_config_and_never_loads_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / 'bad.toml'
            bad.write_text('not valid = [')
            owner = {'runtime_dir': str(root), 'pid': 1, 'instance_id': 'owned'}
            with patch('controller.machine_status', return_value=(True, owner)), patch('main.load_settings', side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(['--stop', '--config', str(bad)]), 0)
            self.assertTrue((root / 'stop.request').is_file())

    def test_status_wrong_json_types_fall_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = {'runtime_dir': str(root), 'pid': 123, 'instance_id': 'owned'}
            for data in ([], None, 7, 'text'):
                with self.subTest(data=data):
                    (root / 'status.json').write_text(json.dumps(data))
                    output = io.StringIO()
                    with patch('controller.machine_status', return_value=(True, owner)), contextlib.redirect_stdout(output):
                        self.assertEqual(controller.main(['--status']), 0)
                    self.assertEqual(json.loads(output.getvalue())['state'], 'starting')

    def test_cli_control_does_not_import_bootstrap_or_worker(self):
        # Copy only controller prerequisites; environment setup is deliberately impossible.
        import shutil
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('main.py', 'controller.py'):
                shutil.copyfile(project / name, root / name)
            (root / 'single_instance.py').write_text('def machine_status(): return False, None\n')
            result = subprocess.run([sys.executable, str(root / 'main.py'), '--status', '--config', 'missing.toml'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['running'])

    def test_worker_refuses_config_version_different_from_supervisor(self):
        from config import load_settings
        settings = load_settings(Path(__file__).resolve().parents[1] / 'config.toml')
        with patch('main.load_settings', return_value=settings), patch('main.MachineInstance') as lock, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(['--config-digest', 'changed']), 3)
        lock.assert_not_called()
