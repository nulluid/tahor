#!/usr/bin/env python3
"""Generate system services for an existing unprivileged Tahor account."""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setup_tahor import unit_quote, working_directory


def render(user, config, state, python, root=ROOT, web_name='tahor-web'):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', user) or user == 'root':
        raise ValueError('Choose a dedicated non-root service account')
    for path in (config, state, python, root):
        if not Path(path).is_absolute() or any(c in str(path) for c in '\r\n\0'):
            raise ValueError('Service paths must be absolute and single-line')
    if web_name not in ('tahor-web', 'tahor-decision-app'):
        raise ValueError('Unrecognized web service name')
    units = {}
    commands = {
        'tahor-backlog-worker': ('worker', ''),
        web_name: ('web', ''),
        'tahor-draft-replies': ('drafts', '--watch'),
        'tahor-filing': ('filing', ''),
        'tahor-filing-maintenance': ('filing-maintenance', ''),
        'tahor-expenses': ('expense-maintenance', ''),
        'tahor-retention': ('retention', ''),
        'tahor-healthcheck': ('status', '--check'),
        'tahor-decisions': ('decisions', ''),
        'tahor-decision-suggestions': ('decision-suggestions', ''),
        'tahor-decision-actions': ('decision-actions', ''),
        'tahor-subscriptions': ('subscriptions', ''),
        'tahor-subscription-actions': ('subscription-actions', ''),
    }
    for name, (command, suffix) in commands.items():
        oneshot = command in ('filing', 'filing-maintenance', 'expense-maintenance', 'retention', 'status', 'decisions', 'subscriptions', 'subscription-actions', 'decision-suggestions', 'decision-actions')
        units[name+'.service'] = f'''[Unit]
Description={name}
After=network-online.target
Wants=network-online.target

[Service]
Type={"oneshot" if oneshot else "simple"}
User={user}
Group={user}
WorkingDirectory={working_directory(root)}
ExecStart={unit_quote(python)} {unit_quote(Path(root)/"run.py")} --env {unit_quote(config)} {command} {suffix}
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={unit_quote(state)}
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
LimitCORE=0
'''
        if command in ('subscriptions', 'subscription-actions', 'decision-suggestions', 'decision-actions', 'filing-maintenance', 'expense-maintenance'):
            units[name+'.service'] += 'TimeoutStartSec=300\n'
        if not oneshot:
            units[name+'.service'] += '\nRestart=always\nRestartSec=30\n\n[Install]\nWantedBy=multi-user.target\n'
    for name, schedule in (('tahor-filing','*-*-* 09:00:00 UTC'),('tahor-retention','*-*-* 09:15:00 UTC'),('tahor-healthcheck','*:0/15'),('tahor-decisions','*:0/5')):
        units[name+'.timer'] = f'''[Unit]
Description=Schedule {name}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
'''
    for name in ('tahor-expenses', 'tahor-filing-maintenance', 'tahor-subscriptions', 'tahor-subscription-actions', 'tahor-decision-suggestions', 'tahor-decision-actions'):
        units[name+'.timer'] = f'''[Unit]
Description=Process queued {name} work promptly

[Timer]
OnBootSec={'5min' if name in ('tahor-filing-maintenance', 'tahor-expenses') else '15s'}
OnUnitInactiveSec={'5min' if name in ('tahor-filing-maintenance', 'tahor-expenses') else '15s'}
AccuracySec=1s

[Install]
WantedBy=timers.target
'''
    return units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default='tahor')
    parser.add_argument('--config', type=Path, default=Path('/etc/tahor/config.env'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/tahor'))
    parser.add_argument('--python', type=Path, default=ROOT/'venv/bin/python')
    parser.add_argument('--web-name', default='tahor-web', choices=('tahor-web','tahor-decision-app'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    units = render(args.user,args.config,args.state,args.python,web_name=args.web_name)
    args.output.mkdir(parents=True,exist_ok=True)
    for name, text in units.items():
        (args.output/name).write_text(text)
    print(f'Generated {len(units)} service/timer files. Review and validate before installing.')


if __name__ == '__main__':
    main()
