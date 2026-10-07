"""Bound notebook reads and backup growth using isolated filesystem fixtures."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import notebooks
from notebooks import NotebookError, load_snapshot, save_if_unchanged


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='autocolab-storage-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'sample.ipynb'
        self.backups = self.root / 'backups'
        document = {
            'cells': [{'cell_type': 'code', 'source': '@bot\n', 'metadata': {},
                       'execution_count': None, 'outputs': []}],
            'metadata': {'keep': True}, 'nbformat': 4, 'nbformat_minor': 5,
        }
        self.path.write_text(json.dumps(document), encoding='utf-8')
        self.snapshot = load_snapshot(self.path)
        self.updated = self.snapshot.replace(self.snapshot.first_marker(), 'answer = 42')

    def managed_backup(self, age_days=0, size=10, label='old.ipynb', number=0):
        self.backups.mkdir(exist_ok=True)
        stamp = (datetime.now(timezone.utc) - timedelta(days=age_days,
                                                      seconds=number + 1)).strftime('%Y%m%dT%H%M%S%fZ')
        path = self.backups / f'{stamp}-012345abcdef-{label}'
        path.write_bytes(b'x' * size)
        return path

    def test_read_accepts_exact_limit(self):
        self.assertEqual(load_snapshot(self.path, max_bytes=len(self.snapshot.raw)).raw,
                         self.snapshot.raw)

    def test_read_rejects_oversized_file_before_opening(self):
        with patch.object(Path, 'open', side_effect=AssertionError('must reject at stat')):
            with self.assertRaisesRegex(NotebookError, 'size limit'):
                load_snapshot(self.path, max_bytes=len(self.snapshot.raw) - 1)

    def test_actual_read_limit_handles_growth_after_stat(self):
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=0)):
            with self.assertRaisesRegex(NotebookError, 'size limit'):
                load_snapshot(self.path, max_bytes=len(self.snapshot.raw) - 1)

    def test_read_rejects_invalid_limits(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(NotebookError):
                load_snapshot(self.path, max_bytes=limit)

    def test_existing_conflict_does_not_create_or_prune_backups(self):
        old = self.managed_backup(age_days=30)
        self.path.write_bytes(self.snapshot.raw + b' ')
        latest = self.path.read_bytes()
        self.assertFalse(save_if_unchanged(self.snapshot, self.updated, self.backups,
                                           backup_max_count=1))
        self.assertEqual(list(self.backups.iterdir()), [old])
        self.assertEqual(self.path.read_bytes(), latest)

    def test_successful_backup_is_exact_original_bytes(self):
        self.assertTrue(save_if_unchanged(self.snapshot, self.updated, self.backups))
        copies = list(self.backups.iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.snapshot.raw)
        self.assertEqual(json.loads(self.path.read_bytes()), self.updated)

    def test_count_prunes_oldest_and_ignores_unrelated_files_and_directories(self):
        oldest = self.managed_backup(age_days=3)
        middle = self.managed_backup(age_days=2)
        newest = self.managed_backup(age_days=1)
        unrelated = self.backups / 'my-notebook.ipynb'
        unrelated.write_bytes(b'personal backup')
        invalid_stamp = self.backups / '20269999T123456123456Z-012345abcdef-other.ipynb'
        invalid_stamp.write_bytes(b'invalid recognized date')
        directory = self.backups / '20261007T123456123456Z-012345abcdef-directory.ipynb'
        directory.mkdir()
        self.assertTrue(save_if_unchanged(self.snapshot, self.updated, self.backups,
                                          backup_max_count=2))
        self.assertFalse(oldest.exists())
        self.assertFalse(middle.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(unrelated.read_bytes(), b'personal backup')
        self.assertEqual(invalid_stamp.read_bytes(), b'invalid recognized date')
        self.assertTrue(directory.is_dir())
        self.assertEqual(len(notebooks._managed_backups(self.backups)), 2)

    def test_total_byte_limit_reserves_room_for_required_backup(self):
        oldest = self.managed_backup(age_days=2, size=100)
        newest = self.managed_backup(age_days=1, size=50)
        cap = len(self.snapshot.raw) + 50
        self.assertTrue(save_if_unchanged(self.snapshot, self.updated, self.backups,
                                          backup_max_bytes=cap))
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(sum(entry[2] for entry in notebooks._managed_backups(self.backups)), cap)

    def test_age_limit_removes_expired_backups(self):
        expired = self.managed_backup(age_days=16)
        recent = self.managed_backup(age_days=2)
        self.assertTrue(save_if_unchanged(self.snapshot, self.updated, self.backups,
                                          backup_max_age_days=14))
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())

    def test_backup_too_large_fails_without_changing_notebook_or_existing_backup(self):
        old = self.managed_backup(age_days=30)
        with self.assertRaisesRegex(NotebookError, 'backup size limit'):
            save_if_unchanged(self.snapshot, self.updated, self.backups,
                              backup_max_bytes=len(self.snapshot.raw) - 1)
        self.assertEqual(self.path.read_bytes(), self.snapshot.raw)
        self.assertEqual(list(self.backups.iterdir()), [old])
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_edit_during_backup_is_discarded_and_unused_backup_removed(self):
        original_prune = notebooks._prune_backups
        changed = self.snapshot.raw + b'\n'

        def edit_after_backup(*args, **kwargs):
            original_prune(*args, **kwargs)
            if kwargs.get('protected') is not None:
                self.path.write_bytes(changed)

        with patch.object(notebooks, '_prune_backups', side_effect=edit_after_backup):
            self.assertFalse(save_if_unchanged(self.snapshot, self.updated, self.backups))
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_failed_backup_write_leaves_original_and_cleans_partial_backup(self):
        original_fsync = notebooks.os.fsync
        calls = 0

        def fail_second_fsync(handle):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('backup disk unavailable')
            return original_fsync(handle)

        with patch.object(notebooks.os, 'fsync', side_effect=fail_second_fsync):
            with self.assertRaisesRegex(OSError, 'backup disk unavailable'):
                save_if_unchanged(self.snapshot, self.updated, self.backups)
        self.assertEqual(self.path.read_bytes(), self.snapshot.raw)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_failed_replace_does_not_leave_unused_backup(self):
        with patch.object(notebooks.os, 'replace', side_effect=PermissionError('file busy')):
            with self.assertRaises(PermissionError):
                save_if_unchanged(self.snapshot, self.updated, self.backups)
        self.assertEqual(self.path.read_bytes(), self.snapshot.raw)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_uppercase_notebook_backups_are_included_in_retention(self):
        self.path = self.path.rename(self.root / 'sample.IPYNB')
        self.snapshot = load_snapshot(self.path)
        self.updated = self.snapshot.replace(self.snapshot.first_marker(), 'answer = 42')
        old = self.managed_backup(age_days=1, label='old.IPYNB')
        self.assertTrue(save_if_unchanged(self.snapshot, self.updated, self.backups,
                                          backup_max_count=1))
        self.assertFalse(old.exists())
        self.assertEqual(len(notebooks._managed_backups(self.backups)), 1)

    def test_existing_backup_name_collision_is_not_deleted(self):
        fixed_now = datetime.now(timezone.utc)
        stamp = fixed_now.strftime('%Y%m%dT%H%M%S%fZ')
        identity = hashlib.sha256(str(self.path.resolve()).encode('utf-8')).hexdigest()[:12]
        self.backups.mkdir()
        existing = self.backups / f'{stamp}-{identity}-{self.path.name}'
        existing.write_bytes(b'previous backup')
        with patch.object(notebooks, 'datetime') as date:
            date.now.return_value = fixed_now
            date.strptime.side_effect = datetime.strptime
            with self.assertRaises(FileExistsError):
                save_if_unchanged(self.snapshot, self.updated, self.backups)
        self.assertEqual(existing.read_bytes(), b'previous backup')
        self.assertEqual(self.path.read_bytes(), self.snapshot.raw)


if __name__ == '__main__':
    unittest.main()
