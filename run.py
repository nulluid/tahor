#!/usr/bin/env python3
"""Load one private environment file and run a Tahor component."""
import argparse
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parent


def load_environment(path):
    if not path.is_file():
        raise ValueError(f'Configuration not found: {path}. Run install.sh first.')
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, separator, value = line.partition('=')
        if not separator or not key.replace('_', '').isalnum() or key[0].isdigit():
            raise ValueError(f'Invalid configuration key on line {number}')
        parsed = shlex.split(value)
        if len(parsed) > 1:
            raise ValueError(f'Quote the configuration value on line {number}')
        os.environ[key] = parsed[0] if parsed else ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=Path.home() / '.config/tahor/config.env')
    parser.add_argument('command', choices=('worker', 'web', 'drafts', 'filing', 'retention', 'doctor', 'status', 'sieve', 'decisions', 'notify'))
    args, extra = parser.parse_known_args()
    try:
        load_environment(args.env.expanduser())
    except ValueError as exc:
        parser.error(str(exc))
    import tahor_db
    tahor_db.init_db()
    scripts = {'notify': 'notifications.py', 'worker': 'backlog_worker.py', 'drafts': 'draft_replies.py', 'filing': 'filing_sweep.py', 'retention': 'retention_sweep.py', 'doctor': 'doctor.py', 'status': 'status_report.py', 'sieve': 'decision-app/generate_sieve.py', 'decisions': 'decision-app/apply_decisions.py'}
    if args.command == 'web':
        os.execv(sys.executable, [sys.executable, '-m', 'gunicorn', '--chdir', str(ROOT / 'decision-app'), '--bind', os.environ.get('TAHOR_BIND', '127.0.0.1:8420'), '--workers', '2', '--timeout', '180', 'app:app', *extra])
    os.execv(sys.executable, [sys.executable, str(ROOT / scripts[args.command]), *extra])


if __name__ == '__main__':
    main()
