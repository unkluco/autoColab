"""Read the worker configuration. Paths are relative to the config file."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    config_file: Path
    watch_folder: Path
    recursive: bool
    min_seconds: float
    max_seconds: float
    marker: str
    cell_types: tuple[str, ...]
    include_outputs: bool
    command: str
    model: str
    main_prompt_file: Path
    code_prompt_file: Path
    markdown_prompt_file: Path
    timeout_seconds: float
    disable_mcp_servers: bool
    disabled_features: tuple[str, ...]
    web_search: str
    retry_seconds: float
    runtime_dir: Path
    backup_enabled: bool
    log_level: str
    log_max_bytes: int
    log_backup_count: int
    notebook_max_bytes: int = 20 * 1024 * 1024
    context_max_chars: int = 2_000_000
    codex_log_max_bytes: int = 10 * 1024 * 1024
    backup_max_count: int = 100
    backup_max_bytes: int = 512 * 1024 * 1024
    backup_max_age_days: float = 14
    global_initial_seconds: float = 30
    global_max_seconds: float = 600
    conflict_limit: int = 3
    conflict_seconds: float = 30
    restart_initial_seconds: float = 5
    restart_max_seconds: float = 120
    restart_reset_seconds: float = 300
    watchdog_seconds: float = 900
    status_retry_delays_ms: tuple[int, ...] = (50, 100, 200)


def load_settings(config_file: Path | str, watch_folder_override=None) -> Settings:
    config_file = Path(config_file).resolve()
    try:
        with config_file.open('rb') as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f'Cannot read config: {exc}') from exc
    base = config_file.parent

    def section(name):
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f'{name} must be a TOML table')
        return value

    def text(table, key, default='', allow_empty=False):
        value = table.get(key, default)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ConfigError(f'{key} must be a nonempty string')
        return value

    def flag(table, key, default):
        value = table.get(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f'{key} must be true/false')
        return value

    def number(table, key, default, minimum=0, integer=False):
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f'{key} must be a number')
        if not math.isfinite(value) or value < minimum or (integer and not isinstance(value, int)):
            raise ConfigError(f'Invalid value for {key}')
        return value

    def path(table, key, default):
        value = Path(text(table, key, default)).expanduser()
        return (base / value).resolve() if not value.is_absolute() else value.resolve()

    scan, notebook, codex, retry, runtime, supervisor = (section(k) for k in ('scan', 'notebook', 'codex', 'retry', 'runtime', 'supervisor'))
    minimum = number(scan, 'min_seconds', 4)
    maximum = number(scan, 'max_seconds', 7)
    if minimum > maximum:
        raise ConfigError('scan.min_seconds must not exceed scan.max_seconds')
    marker = text(notebook, 'marker', '@bot')
    if '\n' in marker or '\r' in marker:
        raise ConfigError('marker must not contain a newline')
    for key in ('replace_entire_marker_line', 'overwrite', 'check_hash_before_write'):
        if not flag(notebook, key, True):
            raise ConfigError(f'{key}=false is unsupported by this worker')
    cell_types = notebook.get('cell_types', ['code', 'markdown'])
    if not isinstance(cell_types, list) or not cell_types or any(k not in ('code', 'markdown') for k in cell_types):
        raise ConfigError('cell_types must contain code and/or markdown')
    disabled = codex.get('disabled_features', [])
    if not isinstance(disabled, list) or any(not isinstance(k, str) or not k for k in disabled):
        raise ConfigError('disabled_features must be an array of nonempty strings')
    web_search = text(codex, 'web_search', 'disabled')
    if web_search not in ('disabled', 'cached', 'live'):
        raise ConfigError('Invalid web_search setting')
    watch_folder = Path(watch_folder_override).resolve() if watch_folder_override is not None else path(data, 'watch_folder', '')
    prompts = [path(codex, key, f'prompts/{name}.txt') for key, name in (
        ('main_prompt_file', 'main'), ('code_prompt_file', 'code'), ('markdown_prompt_file', 'markdown'))]
    for prompt in prompts:
        if not prompt.is_file():
            raise ConfigError(f'Prompt file not found: {prompt}')
    log_level = text(runtime, 'log_level', 'INFO').upper()
    if log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
        raise ConfigError('Invalid log_level')
    timeout = number(codex, 'timeout_seconds', 300, minimum=1)
    global_initial = number(retry, 'global_initial_seconds', 30, minimum=1)
    global_maximum = number(retry, 'global_max_seconds', 600, minimum=1)
    restart_initial = number(supervisor, 'restart_initial_seconds', 5, minimum=1)
    restart_maximum = number(supervisor, 'restart_max_seconds', 120, minimum=1)
    watchdog = number(supervisor, 'watchdog_seconds', 900, minimum=1)
    if global_initial > global_maximum or restart_initial > restart_maximum:
        raise ConfigError('Retry/restart initial_seconds must not exceed max_seconds')
    if watchdog < timeout + 30:
        raise ConfigError('supervisor.watchdog_seconds must be at least codex.timeout_seconds + 30')
    backup_age = number(runtime, 'backup_max_age_days', 14, minimum=0)
    if backup_age <= 0:
        raise ConfigError('runtime.backup_max_age_days must be positive')
    status_delays = runtime.get('status_retry_delays_ms', [50, 100, 200])
    if (not isinstance(status_delays, list) or len(status_delays) > 10
            or any(isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 1000
                   for delay in status_delays)
            or sum(status_delays) > 5000):
        raise ConfigError('runtime.status_retry_delays_ms must be up to 10 integer delays, '
                          'each 0–1000 ms and totaling at most 5000 ms')
    return Settings(
        config_file, watch_folder, flag(data, 'recursive', True), minimum, maximum,
        marker, tuple(cell_types), flag(notebook, 'include_outputs_in_context', False),
        text(codex, 'command', 'codex'), text(codex, 'model', '', allow_empty=True),
        *prompts, timeout,
        flag(codex, 'disable_mcp_servers', True), tuple(disabled), web_search,
        number(retry, 'seconds', 60), path(runtime, 'directory', 'runtime'),
        flag(runtime, 'backup_enabled', True), log_level,
        number(runtime, 'log_max_bytes', 5242880, minimum=1, integer=True),
        number(runtime, 'log_backup_count', 3, minimum=1, integer=True),
        notebook_max_bytes=number(notebook, 'max_bytes', 20 * 1024 * 1024, minimum=1, integer=True),
        context_max_chars=number(codex, 'context_max_chars', 2_000_000, minimum=1, integer=True),
        codex_log_max_bytes=number(codex, 'log_max_bytes', 10 * 1024 * 1024, minimum=1, integer=True),
        backup_max_count=number(runtime, 'backup_max_count', 100, minimum=1, integer=True),
        backup_max_bytes=number(runtime, 'backup_max_bytes', 512 * 1024 * 1024, minimum=1, integer=True),
        backup_max_age_days=backup_age,
        global_initial_seconds=global_initial, global_max_seconds=global_maximum,
        conflict_limit=number(retry, 'conflict_limit', 3, minimum=1, integer=True),
        conflict_seconds=number(retry, 'conflict_seconds', 30, minimum=1),
        restart_initial_seconds=restart_initial, restart_max_seconds=restart_maximum,
        restart_reset_seconds=number(supervisor, 'restart_reset_seconds', 300, minimum=1),
        watchdog_seconds=watchdog,
        status_retry_delays_ms=tuple(status_delays),
    )


if __name__ == '__main__':
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--launcher-settings', required=True, type=Path)
    arguments = parser.parse_args()
    try:
        settings = load_settings(arguments.launcher_settings)
        print(json.dumps({name: getattr(settings, name) for name in (
            'restart_initial_seconds', 'restart_max_seconds',
            'restart_reset_seconds', 'watchdog_seconds')}
            | {'runtime_dir': str(settings.runtime_dir)}, ensure_ascii=True))
    except ConfigError as exc:
        print(f'Invalid AutoColab configuration: {exc}', file=sys.stderr)
        raise SystemExit(3)
