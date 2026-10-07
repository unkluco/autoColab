"""Read notebook JSON without normalizing unrelated cells or metadata."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile


DEFAULT_NOTEBOOK_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUP_MAX_COUNT = 100
DEFAULT_BACKUP_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_BACKUP_MAX_AGE_DAYS = 14
_BACKUP_NAME = re.compile(r'^(\d{8}T\d{12}Z)-[0-9a-f]{12}-.+\.ipynb$', re.IGNORECASE)
_HASH_CHUNK_BYTES = 1024 * 1024


class NotebookError(ValueError):
    pass


def source_text(cell: dict) -> str:
    source = cell.get('source', '')
    if isinstance(source, str):
        return source
    if isinstance(source, list) and all(isinstance(part, str) for part in source):
        return ''.join(source)
    raise NotebookError('Cell source must be a string or an array of strings')


@dataclass(frozen=True)
class Marker:
    cell_index: int
    line_index: int
    cell_type: str


@dataclass(frozen=True)
class Snapshot:
    path: Path
    raw: bytes
    digest: str
    notebook: dict

    def first_marker(self, marker='@bot', cell_types=('code', 'markdown')):
        for cell_index, cell in enumerate(self.notebook['cells']):
            if cell.get('cell_type') in cell_types:
                for line_index, line in enumerate(source_text(cell).splitlines(keepends=True)):
                    if marker in line:
                        return Marker(cell_index, line_index, cell['cell_type'])
        return None

    def context(self, include_outputs=False):
        pieces = []
        for index, cell in enumerate(self.notebook['cells'], 1):
            pieces.append(f'=== Cell {index} — {cell.get("cell_type", "unknown")} ===\n{source_text(cell)}')
            if include_outputs and cell.get('outputs'):
                for output in cell['outputs']:
                    if not isinstance(output, dict):
                        continue
                    text = output.get('text', '')
                    if isinstance(text, list):
                        text = ''.join(part for part in text if isinstance(part, str))
                    if isinstance(text, str) and text:
                        pieces.append(f'--- Output (text) ---\n{text}')
                    display = output.get('data', {}).get('text/plain', '') if isinstance(output.get('data'), dict) else ''
                    if isinstance(display, list):
                        display = ''.join(part for part in display if isinstance(part, str))
                    if isinstance(display, str) and display:
                        pieces.append(f'--- Output (text/plain) ---\n{display}')
                    if output.get('output_type') == 'error':
                        pieces.append(f'--- Error ---\n{output.get("ename", "")}: {output.get("evalue", "")}')
                        traceback = output.get('traceback', [])
                        if isinstance(traceback, list):
                            pieces.append('\n'.join(line for line in traceback if isinstance(line, str)))
        return '\n\n'.join(pieces)

    def replace(self, marker: Marker, result: str):
        if not isinstance(result, str) or not result.strip():
            raise NotebookError('Codex returned an empty answer; notebook left unchanged')
        updated = deepcopy(self.notebook)
        cell = updated['cells'][marker.cell_index]
        original_source = cell.get('source', '')
        lines = source_text(cell).splitlines(keepends=True)
        original_line = lines[marker.line_index]
        # Preserve the model response. Only add the separator needed before the next line.
        replacement = result
        if not replacement.endswith(('\n', '\r')) and (marker.line_index + 1 < len(lines) or original_line.endswith(('\n', '\r'))):
            replacement += '\r\n' if original_line.endswith('\r\n') else '\n'
        new_source = ''.join(lines[:marker.line_index]) + replacement + ''.join(lines[marker.line_index + 1:])
        cell['source'] = new_source.splitlines(keepends=True) if isinstance(original_source, list) else new_source
        return updated


def load_snapshot(path: Path | str, max_bytes: int = DEFAULT_NOTEBOOK_MAX_BYTES) -> Snapshot:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise NotebookError('Notebook size limit must be a positive integer')
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise NotebookError(f'Notebook exceeds the {max_bytes:,}-byte size limit')
    # The file can grow after stat(), so the actual read also has a hard bound.
    with path.open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise NotebookError(f'Notebook exceeds the {max_bytes:,}-byte size limit')
    try:
        notebook = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotebookError(f'Invalid notebook JSON: {exc}') from exc
    if not isinstance(notebook, dict) or notebook.get('nbformat') != 4 or not isinstance(notebook.get('cells'), list):
        raise NotebookError('Expected an ipynb version 4 notebook with a cells array')
    for cell in notebook['cells']:
        if not isinstance(cell, dict) or cell.get('cell_type') not in ('code', 'markdown', 'raw'):
            raise NotebookError('Invalid notebook cell')
        source_text(cell)
    return Snapshot(path, raw, hashlib.sha256(raw).hexdigest(), notebook)


def _matches_snapshot(snapshot: Snapshot) -> bool:
    """Hash in small chunks and reject growth without loading another full file."""
    expected_size = len(snapshot.raw)
    try:
        if snapshot.path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        total = 0
        with snapshot.path.open('rb') as stream:
            while True:
                chunk = stream.read(min(_HASH_CHUNK_BYTES, expected_size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    return False
                digest.update(chunk)
    except FileNotFoundError:
        return False
    return total == expected_size and digest.hexdigest() == snapshot.digest


def _managed_backups(backup_dir: Path):
    """Recognize only the filenames produced by AutoColab, without following links."""
    entries = []
    for path in backup_dir.iterdir():
        match = _BACKUP_NAME.fullmatch(path.name)
        if match is None:
            continue
        try:
            created = datetime.strptime(match[1], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
            info = path.stat(follow_symlinks=False)
        except ValueError:
            continue
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            entries.append((created, path.name, info.st_size, path))
    return sorted(entries)


def _prune_backups(backup_dir: Path, max_count: int, max_bytes: int,
                   cutoff: datetime, protected: Path | None = None):
    entries = _managed_backups(backup_dir)
    retained = []
    for entry in entries:
        if entry[0] < cutoff and entry[3] != protected:
            entry[3].unlink(missing_ok=True)
        else:
            retained.append(entry)
    total_bytes = sum(entry[2] for entry in retained)
    for entry in list(retained):
        if len(retained) <= max_count and total_bytes <= max_bytes:
            break
        if entry[3] == protected:
            continue
        entry[3].unlink(missing_ok=True)
        retained.remove(entry)
        total_bytes -= entry[2]
    if len(retained) > max_count or total_bytes > max_bytes:
        raise NotebookError('Required notebook backup does not fit the configured retention limits')


def save_if_unchanged(snapshot: Snapshot, updated: dict, backup_dir=None, *,
                      backup_max_count: int = DEFAULT_BACKUP_MAX_COUNT,
                      backup_max_bytes: int = DEFAULT_BACKUP_MAX_BYTES,
                      backup_max_age_days: float = DEFAULT_BACKUP_MAX_AGE_DAYS) -> bool:
    if backup_dir is not None:
        for value, name in ((backup_max_count, 'count'), (backup_max_bytes, 'size')):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise NotebookError(f'Backup {name} limit must be a positive integer')
        if (isinstance(backup_max_age_days, bool) or not isinstance(backup_max_age_days, (int, float))
                or not math.isfinite(backup_max_age_days) or backup_max_age_days <= 0):
            raise NotebookError('Backup age limit must be a positive number of days')
        if len(snapshot.raw) > backup_max_bytes:
            raise NotebookError('Original notebook exceeds the backup size limit; notebook left unchanged')
        if snapshot.path.suffix.lower() != '.ipynb':
            raise NotebookError('Notebook backup filenames must end in .ipynb')
    payload = (json.dumps(updated, ensure_ascii=False, indent=1, allow_nan=False) + '\n').encode('utf-8')
    # Write a complete sibling file first, then check the current bytes immediately before replacing.
    handle, temporary_name = tempfile.mkstemp(prefix=f'.{snapshot.path.name}.', suffix='.tmp', dir=snapshot.path.parent)
    temporary_path = Path(temporary_name)
    backup_path = None
    backup_created = False
    saved = False
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Do not create a backup or prune existing ones for an already stale result.
        if not _matches_snapshot(snapshot):
            return False
        if backup_dir is not None:
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=backup_max_age_days)
            # Reserve space before the write, so retention also prevents cumulative disk growth.
            _prune_backups(backup_dir, backup_max_count - 1,
                           backup_max_bytes - len(snapshot.raw), cutoff)
            identity = hashlib.sha256(str(snapshot.path.resolve()).encode('utf-8')).hexdigest()[:12]
            stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
            backup_path = backup_dir / f'{stamp}-{identity}-{snapshot.path.name}'
            with backup_path.open('xb') as stream:
                backup_created = True
                stream.write(snapshot.raw)
                stream.flush()
                os.fsync(stream.fileno())
            _prune_backups(backup_dir, backup_max_count, backup_max_bytes, cutoff,
                           protected=backup_path)
        # A Drive edit during backup preparation must still discard this answer.
        if not _matches_snapshot(snapshot):
            return False
        os.replace(temporary_path, snapshot.path)
        saved = True
        return True
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            if backup_created and not saved:
                backup_path.unlink(missing_ok=True)
