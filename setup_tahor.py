#!/usr/bin/env python3
"""Create a private configuration and optional systemd user services."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def env_value(value):
    if '\n' in value or '\r' in value or '\0' in value:
        raise ValueError('Configuration values must be one line')
    return json.dumps(value, ensure_ascii=False)


def write_new(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x') as stream:
            os.chmod(path, mode)
            stream.write(text)
    except FileExistsError:
        return False
    return True


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%'))


def working_directory(value):
    text = str(value)
    if any(char in text for char in '\r\n\0') or text != text.strip():
        raise ValueError('Service directory cannot contain line breaks or surrounding whitespace')
    return text.replace('%', '%%')


def write_units(directory, config, python):
    directory.mkdir(parents=True, exist_ok=True)
    commands = {
        'tahor-backlog-worker': ('worker', ''),
        'tahor-web': ('web', ''),
        'tahor-draft-replies': ('drafts', '--watch'),
        'tahor-filing': ('filing', ''),
        'tahor-retention': ('retention', ''),
        'tahor-healthcheck': ('status', '--check'),
    }
    for name, (command, suffix) in commands.items():
        oneshot = command in ('filing', 'retention', 'status')
        text = f'''[Unit]
Description={name}
After=network-online.target
Wants=network-online.target

[Service]
Type={"oneshot" if oneshot else "simple"}
WorkingDirectory={working_directory(ROOT)}
ExecStart={unit_quote(python)} {unit_quote(ROOT / "run.py")} --env {unit_quote(config)} {command} {suffix}
UMask=0077
NoNewPrivileges=true
'''
        if not oneshot:
            text += 'Restart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n'
        (directory / (name + '.service')).write_text(text)
    for name, schedule in (('tahor-filing', '*-*-* 09:00:00'), ('tahor-retention', '*-*-* 09:15:00'), ('tahor-healthcheck', '*:0/15')):
        (directory / (name + '.timer')).write_text(f'''[Unit]
Description=Schedule {name}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
''')


def configure(args):
    config_dir = args.config_dir.expanduser().resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    data_dir = (args.data_dir or config_dir / 'data').expanduser().resolve()
    state_dir = config_dir / 'state'
    for directory in (data_dir, state_dir):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    config_path = config_dir / 'config.env'
    if not config_path.exists():
        email = args.email or os.environ.get('FASTMAIL_EMAIL', '')
        password = os.environ.get('FASTMAIL_APP_PASSWORD', '')
        api_key = os.environ.get('OPENROUTER_API_KEY', '')
        if not args.non_interactive:
            email = email or input('Mailbox email: ').strip()
            password = password or getpass.getpass('IMAP app password (hidden): ')
            api_key = api_key or getpass.getpass('OpenRouter API key (hidden; free models supported): ')
        values = {
            'FASTMAIL_EMAIL': email, 'FASTMAIL_APP_PASSWORD': password,
            'FASTMAIL_HOST': args.imap_host, 'FASTMAIL_SMTP_HOST': args.smtp_host,
            'OPENROUTER_API_KEY': api_key, 'DATA_DIR': str(data_dir),
            'PROMPT_PATH': str(data_dir / 'prompt.txt'),
            'TAHOR_DB_PATH': str(state_dir / 'decisions.db'),
            'TAHOR_STATE_DIR': str(state_dir),
            'TAHOR_SETTINGS_PATH': str(state_dir / 'settings.json'),
            'BASE_URL': args.base_url, 'ALLOWED_EMAIL': email,
            'GOOGLE_CLIENT_ID': os.environ.get('GOOGLE_CLIENT_ID', ''),
            'GOOGLE_CLIENT_SECRET': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
            'TAHOR_DATA_PUSH': '0', 'TAHOR_NOTIFY_DRAFTS': '0',
        }
        write_new(config_path, '# Private Tahor configuration. Never commit this file.\n' + ''.join(f'{key}={env_value(value)}\n' for key, value in values.items()))
    for source, destination in (('prompt.example.txt', 'prompt.txt'), ('vendor_buckets.example.json', 'vendor_buckets.json')):
        write_new(data_dir / destination, (ROOT / source).read_text())
    write_new(data_dir / 'sieve.txt', '# Tahor sender blocks will be proposed here.\n')
    write_new(state_dir / 'settings.json', json.dumps({'classify_mode': args.mode, 'rule_model': 'nemotron-free', 'reply_model': 'nemotron-free', 'reply_triggers': []}, indent=2) + '\n')
    if args.systemd_dir:
        write_units(args.systemd_dir.expanduser(), config_path, Path(sys.executable).absolute())
    print(f'Configuration: {config_path}')
    print(f'Private mailbox data: {data_dir}')
    print(f'Runtime state: {state_dir}')
    print('Existing configuration and data files were preserved.')
    return config_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path.home() / '.config/tahor')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--systemd-dir', type=Path)
    parser.add_argument('--email')
    parser.add_argument('--imap-host', default='imap.fastmail.com')
    parser.add_argument('--smtp-host', default='smtp.fastmail.com')
    parser.add_argument('--base-url', default='http://localhost:8420')
    parser.add_argument('--mode', choices=('free', 'paid', 'auto'), default='free')
    parser.add_argument('--non-interactive', action='store_true')
    configure(parser.parse_args())


if __name__ == '__main__':
    main()
