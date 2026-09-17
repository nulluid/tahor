#!/usr/bin/env python3
"""Render opt-in notification units separately from the core services."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.system_services import render as core_render


def render(user, config, state, python, root=ROOT):
    service = core_render(user, config, state, python, root)['tahor-healthcheck.service']
    service = service.replace('Description=tahor-healthcheck', 'Description=Tahor self-addressed notifications')
    service = service.replace(' status --check', ' notify') if ' status --check' in service else service.replace(' status ', ' notify ')
    service += '\nTimeoutStartSec=300\n'
    return {'tahor-notifications.service': service,
            'tahor-notifications.timer': '''[Unit]
Description=Check Tahor notifications every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
'''}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default='tahor')
    parser.add_argument('--config', type=Path, default=Path('/etc/tahor/config.env'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/tahor'))
    parser.add_argument('--python', type=Path, default=ROOT/'venv/bin/python')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in render(args.user, args.config, args.state, args.python).items():
        (args.output/name).write_text(content)
    print('Generated two notification units. Sending remains off until explicitly enabled.')


if __name__ == '__main__':
    main()
