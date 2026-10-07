"""Recovery tests with local temporary files and fake solvers only."""
from dataclasses import replace
import json
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from config import ConfigError, load_settings
from solver import SolverCleanupError, SolverError, SolverInputError, SolverResponseError
from worker import Worker, write_json

PROJECT = Path(__file__).resolve().parents[1]


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='autocolab-recovery-test-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = replace(load_settings(PROJECT / 'config.toml'),
                                watch_folder=root / 'notebooks', runtime_dir=root / 'runtime',
                                retry_seconds=30, global_initial_seconds=30, global_max_seconds=600,
                                conflict_limit=3, conflict_seconds=30,
                                marker='@bot', cell_types=('code', 'markdown'))
        self.settings.watch_folder.mkdir()

    def notebook(self, name='sample.ipynb'):
        path = self.settings.watch_folder / name
        path.write_text(json.dumps({'nbformat': 4, 'nbformat_minor': 5, 'metadata': {},
            'cells': [{'cell_type': 'code', 'source': '@bot\n', 'metadata': {},
                       'outputs': [], 'execution_count': None}]}), encoding='utf-8')
        return path

    def test_status_failure_does_not_prevent_saving_notebook(self):
        path = self.notebook()
        worker = Worker(self.settings, lambda *_: 'answer = 42')
        with patch('worker.write_json', side_effect=PermissionError('temporary lock')):
            with self.assertLogs('autocolab', level='WARNING') as logs:
                self.assertEqual(worker.scan_once(), 'saved')
        self.assertEqual(worker.status['saved'], 1)
        self.assertNotIn('@bot', path.read_text(encoding='utf-8'))
        self.assertEqual(len(logs.output), 1)

    def test_run_survives_first_status_failure_and_stops_normally(self):
        self.notebook()
        event = threading.Event()
        def solve(*_):
            event.set()
            return 'answer = 42'
        worker = Worker(self.settings, solve, event)
        attempts = []
        def write(path, data, **kwargs):
            attempts.append(data['state'])
            if len(attempts) == 1:
                raise PermissionError('temporary lock')
            write_json(path, data, **kwargs)
        with patch('worker.write_json', side_effect=write):
            worker.run()
        self.assertEqual(attempts[-1], 'stopped')

    def test_missing_watch_folder_recovers_when_it_returns(self):
        self.settings.watch_folder.rmdir()
        worker = Worker(self.settings, lambda *_: 'answer = 42')
        self.assertEqual(worker.scan_once(), 'failed')
        self.assertEqual(worker.status['state'], 'waiting_for_folder')
        self.settings.watch_folder.mkdir()
        self.notebook()
        self.assertEqual(worker.scan_once(), 'saved')

    def test_continuous_main_starts_even_with_missing_drive(self):
        import main
        self.settings.watch_folder.rmdir()
        observed = []
        class Instance:
            owner = {'instance_id': 'isolated-test'}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
        def run(worker):
            observed.append(worker.scan_once())
        with patch('main.load_settings', return_value=self.settings), \
             patch('main.MachineInstance', return_value=Instance()), \
             patch('main.InstanceLock', return_value=Instance()), \
             patch('main.setup_logging'), patch('main.signal.signal'), \
             patch('main.CodexSolver', return_value=lambda *_: 'answer = 42'), \
             patch.object(Worker, 'run', run):
            self.assertEqual(main.main([]), 0)
        self.assertEqual(observed, ['failed'])

    def test_service_error_pauses_all_files_increases_delay_and_resets_on_success(self):
        for name in ('a.ipynb', 'b.ipynb', 'c.ipynb'):
            self.notebook(name)
        calls = []
        responses = iter([SolverError('network down'), SolverError('still down'), 'answer = 42'])
        def solve(snapshot, marker):
            calls.append(snapshot.path.name)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response
        worker = Worker(self.settings, solve)
        now = [0.0]
        with patch('worker.time.monotonic', side_effect=lambda: now[0]), \
             patch('worker.random.uniform', side_effect=lambda low, high: (low + high) / 2):
            self.assertEqual(worker.scan_once(), 'backoff')
            self.assertEqual(len(calls), 1)
            self.assertEqual(worker.global_backoff_until, 30)
            self.assertEqual(worker.scan_once(), 'backoff')
            self.assertEqual(len(calls), 1)
            now[0] = 31
            self.assertEqual(worker.scan_once(), 'backoff')
            self.assertEqual(worker.global_backoff_until, 91)
            now[0] = 92
            self.assertEqual(worker.scan_once(), 'saved')
        self.assertEqual(worker.global_backoff_seconds, 0)
        self.assertEqual(worker.global_backoff_until, 0)
        self.assertEqual(calls, ['a.ipynb', 'b.ipynb', 'a.ipynb'])

    def test_invalid_response_does_not_block_other_notebooks(self):
        first = self.notebook('a.ipynb')
        second = self.notebook('b.ipynb')
        def solve(snapshot, marker):
            if snapshot.path == first:
                raise SolverResponseError('empty answer')
            return 'answer = 42'
        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), 'saved')
        self.assertIn('@bot', first.read_text())
        self.assertNotIn('@bot', second.read_text())

    def test_incomplete_process_cleanup_exits_for_supervisor_recovery(self):
        self.notebook()
        def solve(*_):
            raise SolverCleanupError('descendant still owns a pipe')
        worker = Worker(self.settings, solve)
        with self.assertRaises(SolverCleanupError):
            worker.run()
        self.assertEqual(worker.status['state'], 'stopped')

    def test_failed_once_returns_failure_exit_code(self):
        import main
        self.notebook()
        class Instance:
            owner = {'instance_id': 'isolated-test'}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
        def solve(*_):
            raise SolverError('service unavailable')
        with patch('main.load_settings', return_value=self.settings), \
             patch('main.MachineInstance', return_value=Instance()), \
             patch('main.InstanceLock', return_value=Instance()), \
             patch('main.setup_logging'), patch('main.signal.signal'), \
             patch('main.CodexSolver', return_value=solve):
            self.assertEqual(main.main(['--once']), 1)

    def test_oversized_context_is_isolated_to_its_notebook(self):
        first = self.notebook('a.ipynb')
        second = self.notebook('b.ipynb')
        def solve(snapshot, marker):
            if snapshot.path == first:
                raise SolverInputError('context exceeds configured limit')
            return 'answer = 42'
        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), 'saved')
        self.assertIn('@bot', first.read_text())
        self.assertNotIn('@bot', second.read_text())
        self.assertEqual(worker.global_backoff_until, 0)

    def test_repeated_conflicts_defer_first_file_and_allow_next_file(self):
        first = self.notebook('a.ipynb')
        second = self.notebook('b.ipynb')
        calls = []
        def solve(snapshot, marker):
            calls.append(snapshot.path.name)
            if snapshot.path == first:
                snapshot.path.write_bytes(snapshot.raw + b'\n')
            return 'answer = 42'
        worker = Worker(self.settings, solve)
        for _ in range(self.settings.conflict_limit):
            self.assertEqual(worker.scan_once(), 'conflict')
        self.assertEqual(worker.scan_once(), 'saved')
        self.assertEqual(calls, ['a.ipynb'] * self.settings.conflict_limit + ['b.ipynb'])
        self.assertNotIn('@bot', second.read_text())
        self.assertIn('@bot', first.read_text())

    def test_wait_emits_progress_and_can_be_stopped(self):
        now = [0.0]
        class Event:
            def is_set(self):
                return now[0] >= 6
            def wait(self, duration):
                now[0] += duration
        worker = Worker(self.settings, None, Event())
        with patch.object(worker, 'scan_once', return_value='idle'), \
             patch('worker.time.monotonic', side_effect=lambda: now[0]), \
             patch('worker.random.uniform', return_value=7), \
             patch('worker.write_json') as write:
            worker.run()
        self.assertGreaterEqual(write.call_count, 3)
        self.assertEqual(worker.status['state'], 'stopped')


class ConfigDurabilityTests(unittest.TestCase):
    def invalid_config(self, change):
        with tempfile.TemporaryDirectory(prefix='autocolab-config-test-') as directory:
            path = Path(directory) / 'config.toml'
            text = (PROJECT / 'config.toml').read_text(encoding='utf-8-sig')
            text = text.replace('"prompts/', '"' + PROJECT.as_posix() + '/prompts/')
            path.write_text(change(text), encoding='utf-8')
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_watchdog_must_allow_cli_timeout_and_cleanup(self):
        self.invalid_config(lambda text: text.replace('watchdog_seconds = 900', 'watchdog_seconds = 300'))

    def test_retry_maximum_cannot_be_smaller_than_initial(self):
        self.invalid_config(lambda text: re.sub(r'(?m)^global_max_seconds\s*=.*$', 'global_max_seconds = 1', text))

    def test_backup_cap_must_be_positive(self):
        self.invalid_config(lambda text: text.replace('backup_max_count = 100', 'backup_max_count = 0'))

    def test_backup_age_must_be_positive(self):
        self.invalid_config(lambda text: text.replace('backup_max_age_days = 14', 'backup_max_age_days = 0'))

    def test_status_delays_reject_negative_or_unbounded_waits(self):
        for value in ('[-1]', '[true]', '[1001]', '[1000, 1000, 1000, 1000, 1000, 1000]'):
            with self.subTest(value=value):
                self.invalid_config(lambda text: re.sub(r'(?m)^status_retry_delays_ms\s*=.*$',
                    'status_retry_delays_ms = ' + value, text))


if __name__ == '__main__':
    unittest.main()
