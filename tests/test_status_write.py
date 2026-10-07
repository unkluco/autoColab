"""Status replacement retries use temporary local files, never Drive or Codex."""
import ctypes
from ctypes import wintypes
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import call, patch

from config import load_settings
from worker import Worker, write_json

PROJECT = Path(__file__).resolve().parents[1]


class StatusWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='autocolab-status-retry-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'status.json'
        self.original = b'{"state":"old"}'
        self.path.write_bytes(self.original)

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob('status.json.*.tmp')), [])

    def test_transient_denials_retry_same_complete_file_then_succeed(self):
        commit = os.replace
        attempts = []
        def replace_after_lock(source, destination):
            attempts.append(source)
            self.assertEqual(json.loads(Path(source).read_text(encoding='utf-8')), {'state': 'đang quét'})
            if len(attempts) < 3:
                raise PermissionError('temporary reader lock')
            commit(source, destination)
        with patch('worker.os.replace', side_effect=replace_after_lock), patch('worker.time.sleep') as sleep:
            write_json(self.path, {'state': 'đang quét'})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(sleep.call_args_list, [call(.05), call(.1)])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'state': 'đang quét'})
        self.assert_no_temporary_files()

    def test_persistent_denial_has_four_attempts_preserves_previous_status(self):
        with patch('worker.os.replace', side_effect=PermissionError('still locked')) as commit, \
             patch('worker.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                write_json(self.path, {'state': 'new'})
        self.assertEqual(commit.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(.05), call(.1), call(.2)])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary_files()

    def test_windows_sharing_violation_is_retryable_even_as_oserror(self):
        error = OSError(errno.EIO, 'Windows sharing violation')
        error.winerror = 32
        commit = os.replace
        attempts = []
        def replace_after_lock(source, destination):
            attempts.append(source)
            if len(attempts) == 1:
                raise error
            commit(source, destination)
        with patch('worker.os.replace', side_effect=replace_after_lock), patch('worker.time.sleep') as sleep:
            write_json(self.path, {'state': 'new'}, retry_delays_ms=(7,))
        sleep.assert_called_once_with(.007)
        self.assertEqual(len(attempts), 2)
        self.assert_no_temporary_files()

    def test_disk_full_is_not_retried(self):
        with patch('worker.os.replace', side_effect=OSError(errno.ENOSPC, 'disk full')) as commit, \
             patch('worker.time.sleep') as sleep:
            with self.assertRaises(OSError):
                write_json(self.path, {'state': 'new'})
        self.assertEqual(commit.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary_files()

    def test_serialization_failure_does_not_retry_or_replace_status(self):
        with patch('worker.os.replace') as commit, patch('worker.time.sleep') as sleep:
            with self.assertRaises(TypeError):
                write_json(self.path, {'invalid': object()})
        commit.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporary_files()

    def test_empty_delays_disable_retries(self):
        with patch('worker.os.replace', side_effect=PermissionError('locked')) as commit, \
             patch('worker.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                write_json(self.path, {'state': 'new'}, retry_delays_ms=())
        self.assertEqual(commit.call_count, 1)
        sleep.assert_not_called()
        self.assert_no_temporary_files()

    def test_worker_exhaustion_warns_once_and_can_recover_on_next_update(self):
        settings = replace(load_settings(PROJECT / 'config.toml'), runtime_dir=self.root,
                           status_retry_delays_ms=(1, 2, 3))
        worker = Worker(settings, None)
        with patch('worker.os.replace', side_effect=PermissionError('locked')) as commit, \
             patch('worker.time.sleep') as sleep, self.assertLogs('autocolab', level='WARNING') as logs:
            self.assertFalse(worker.update_status(state='scanning'))
        self.assertEqual(commit.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(.001), call(.002), call(.003)])
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(worker.update_status(state='idle'))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['state'], 'idle')
        self.assert_no_temporary_files()

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing behavior')
    def test_actual_windows_reader_lock_released_during_retry(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # Permit reads/writes, but deliberately deny deleting/replacing this file.
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        released = threading.Event()
        def release():
            kernel.CloseHandle(handle)
            released.set()
        timer = threading.Timer(.08, release)
        timer.start()
        try:
            write_json(self.path, {'state': 'recovered'})
            self.assertTrue(released.is_set())
            self.assertEqual(json.loads(self.path.read_text())['state'], 'recovered')
            self.assert_no_temporary_files()
        finally:
            timer.cancel()
            timer.join(timeout=1)
            if not released.is_set():
                kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
