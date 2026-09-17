#!/usr/bin/env python3
"""Generate separate, non-login Fastmail service units; admin enrollment is required."""
from pathlib import Path


def render(root=Path('/opt/tahor')):
    if not root.is_absolute() or any(c in str(root) for c in '\n\r\0"%'):
        raise ValueError('Invalid code path')
    common = f'''[Unit]
Description=Tahor isolated Fastmail connector
After=network-online.target
Wants=network-online.target

[Service]
User=tahor-provider
Group=tahor-provider
SupplementaryGroups=tahor-bridge
WorkingDirectory={root}
LoadCredential=config:/etc/tahor-provider/config.json
LoadCredentialEncrypted=fastmail:/etc/credstore.encrypted/tahor-fastmail
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/tahor-provider/private /var/lib/tahor-provider/outbox
InaccessiblePaths=/var/lib/tahor /etc/tahor
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectKernelLogs=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
LimitCORE=0
MemoryMax=128M
MemorySwapMax=0
TasksMax=32
'''
    command = f'{root}/venv/bin/python -m provider_connector.service --config=%d/config'
    return {
        'tahor-provider.service': common + f'ExecStart={command}\nRestart=on-failure\nRestartSec=300\n\n[Install]\nWantedBy=multi-user.target\n',
        'tahor-provider-check.service': common + f'Type=oneshot\nExecStart={command} --check-auth\nTimeoutStartSec=420\n',
    }
