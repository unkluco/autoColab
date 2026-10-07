"""Recover only recorded AutoColab temporary files, within configured roots."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

logger = logging.getLogger('autocolab')
_TEMP = re.compile(r'^\.autocolab-([0-9a-f]{32})\.tmp$')
_RECORD = re.compile(r'^[0-9a-f]{32}\.json$')


def path_link(path: Path) -> bool:
    """Reject symlinks/junctions, without rejecting cloud placeholder tags."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) in (
        0xA0000003, 0xA000000C)  # mount point (junction), symbolic link


def within_root(path: Path, root: Path) -> bool:
    root = root.resolve()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if path_link(current):
                    return False
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False


class OwnedTemp:
    def __init__(self, parent: Path, journal_dir: Path | None = None):
        identity = uuid.uuid4().hex
        self.path = Path(parent).absolute() / f'.autocolab-{identity}.tmp'
        self.record = Path(journal_dir) / f'{identity}.json' if journal_dir is not None else None
        self.created = False

    def open(self):
        if self.record is not None:
            self.record.parent.mkdir(parents=True, exist_ok=True)
            if path_link(self.record.parent):
                raise OSError('Temporary-file journal must not be a link or junction')
            with self.record.open('x', encoding='utf-8') as stream:
                json.dump({'version': 1, 'path': str(self.path)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            stream = self.path.open('xb')
            self.created = True
            return stream
        except BaseException:
            if self.record is not None:
                self.record.unlink(missing_ok=True)
            raise

    def cleanup(self):
        # Keep the journal if deletion fails, so the next host can recover.
        if self.created:
            self.path.unlink(missing_ok=True)
        if self.record is not None:
            self.record.unlink(missing_ok=True)


def recover_temps(journal_dir: Path, roots: tuple[Path, ...]):
    if not journal_dir.is_dir() or path_link(journal_dir):
        return
    for record in journal_dir.iterdir():
        if not _RECORD.fullmatch(record.name) or path_link(record):
            continue
        try:
            if record.stat().st_size > 16384:
                raise ValueError('oversized record')
            data = json.loads(record.read_text(encoding='utf-8'))
            target = Path(data['path'])
            match = _TEMP.fullmatch(target.name)
            if data.get('version') != 1 or not target.is_absolute() or not match or match[1] != record.stem:
                raise ValueError('invalid temporary-file record')
            allowed = next((root for root in roots if root.is_dir() and within_root(target, root)), None)
            if allowed is None:
                continue  # Keep records when Drive is unavailable or configuration changed.
            if target.exists():
                if path_link(target) or not target.is_file():
                    raise ValueError('temporary path is not a regular file')
                target.unlink()
            record.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning('Cannot recover recorded temporary file: %s', record)


def recover_calls(calls_dir: Path):
    if not calls_dir.is_dir() or path_link(calls_dir):
        return
    for folder in calls_dir.iterdir():
        if not folder.name.startswith('codex-') or path_link(folder) or not folder.is_dir():
            continue
        marker = folder / '.autocolab-owned'
        try:
            if path_link(marker) or marker.read_text(encoding='ascii') != folder.name:
                continue
            if within_root(folder, calls_dir):
                shutil.rmtree(folder)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning('Cannot remove abandoned Codex call directory: %s', folder)


def directory_bytes(root: Path, limit: int) -> int:
    total = 0
    if not root.is_dir():
        return total
    for parent, dirs, files in os.walk(root, followlinks=False):
        parent = Path(parent)
        dirs[:] = [name for name in dirs if not path_link(parent / name)]
        for name in files:
            path = parent / name
            try:
                if not path_link(path):
                    total += path.stat().st_size
                    if total > limit:
                        return total
            except FileNotFoundError:
                pass
    return total
