import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from storage_safety import OwnedTemp, recover_temps, recover_calls


class ArtifactTests(unittest.TestCase):
    def test_recorded_orphan_is_removed_but_unrecorded_file_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / 'journal'
            owned = OwnedTemp(root, journal)
            with owned.open() as stream:
                stream.write(b'partial notebook')
            other = root / '.autocolab-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp'
            other.write_bytes(b'personal file')
            recover_temps(journal, (root,))
            self.assertFalse(owned.path.exists())
            self.assertEqual(other.read_bytes(), b'personal file')
            self.assertEqual(list(journal.iterdir()), [])

    def test_recovery_does_not_delete_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / 'allowed'
            allowed.mkdir()
            owned = OwnedTemp(root, allowed / 'journal')
            with owned.open() as stream:
                stream.write(b'outside')
            recover_temps(allowed / 'journal', (allowed,))
            self.assertTrue(owned.path.exists())
            owned.cleanup()

    def test_calls_cleanup_requires_ownership_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = root / 'codex-abcdefgh'
            owned.mkdir()
            (owned / '.autocolab-owned').write_text(owned.name)
            (owned / 'answer.txt').write_text('answer')
            other = root / 'codex-personal'
            other.mkdir()
            recover_calls(root)
            self.assertFalse(owned.exists())
            self.assertTrue(other.exists())

    def test_abrupt_process_exit_leaves_record_that_next_host_recovers(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = ('import os,sys; from pathlib import Path; from storage_safety import OwnedTemp; '
                    'r=Path(sys.argv[1]); t=OwnedTemp(r,r/"journal"); s=t.open(); '
                    's.write(b"partial notebook"); s.flush(); os.fsync(s.fileno()); os._exit(7)')
            child = subprocess.run([sys.executable, '-c', code, str(root)], cwd=project, timeout=10)
            self.assertEqual(child.returncode, 7)
            self.assertEqual(len(list(root.glob('.autocolab-*.tmp'))), 1)
            recover_temps(root / 'journal', (root,))
            self.assertEqual(list(root.glob('.autocolab-*.tmp')), [])

    def test_committed_temp_missing_is_safe_to_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = OwnedTemp(root, root / 'journal')
            with owned.open() as stream:
                stream.write(b'complete')
            owned.path.rename(root / 'notebook.ipynb')
            recover_temps(root / 'journal', (root,))
            self.assertEqual((root / 'notebook.ipynb').read_bytes(), b'complete')
