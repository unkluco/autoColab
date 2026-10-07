"""Stop/status work without loading notebook config or preparing dependencies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from single_instance import machine_status


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Control the active AutoColab worker')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--status', action='store_true')
    group.add_argument('--stop', action='store_true')
    parser.add_argument('--config')  # Accepted for script compatibility; owner determines runtime.
    parser.add_argument('--watch-folder')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    try:
        running, owner = machine_status()
        if args.stop:
            if running and owner:
                (Path(owner['runtime_dir']) / 'stop.request').write_text('stop\n', encoding='utf-8')
                print('Stop requested. Worker will cancel its active call and exit.')
            elif running:
                print('Host owner details are inaccessible; stop it from its original console.', file=sys.stderr)
                return 1
            else:
                print('Worker is not running.')
            return 0
        status = {}
        if running and owner:
            try:
                path = Path(owner['runtime_dir']) / 'status.json'
                if path.stat().st_size <= 131072:
                    value = json.loads(path.read_text(encoding='utf-8'))
                    if isinstance(value, dict):
                        status = value
            except (OSError, ValueError):
                pass
            if status.get('instance_id') != owner['instance_id'] or status.get('pid') != owner['pid']:
                status = {'state': 'starting'}
            status.update(owner)
        status['running'] = running
        if not running:
            status['state'] = 'stopped'
        elif not owner:
            status['state'] = 'running_owner_unavailable'
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError) as exc:
        print(f'Controller error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
