#!/usr/bin/env python3
"""Install the optional isolated connector on a hardened Linux system deployment."""
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_changes import atomic_write
from provider_services import render

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = Path('/var/lib/tahor-provider')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def directory(path, owner, group, mode):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SystemExit('Refusing symlink at installation path.')
    os.chown(path, owner, group)
    path.chmod(mode)


def main():
    if os.geteuid() != 0 or ROOT != Path('/opt/tahor'):
        raise SystemExit('Install from root-owned /opt/tahor using sudo.')
    if ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
        raise SystemExit('Application code must be root-owned and not group/world writable.')
    app = pwd.getpwnam('tahor')
    for name in ('tahor-provider', 'tahor-bridge'):
        try:
            grp.getgrnam(name)
        except KeyError:
            run('groupadd', '--system', name)
    try:
        provider = pwd.getpwnam('tahor-provider')
    except KeyError:
        run('useradd', '--system', '--gid', 'tahor-provider', '--home-dir', str(BRIDGE/'private'), '--shell', '/sbin/nologin', 'tahor-provider')
        provider = pwd.getpwnam('tahor-provider')
    if provider.pw_uid == 0 or provider.pw_shell not in ('/sbin/nologin', '/usr/sbin/nologin'):
        raise SystemExit('Connector account must be non-root and non-login.')
    bridge_gid = grp.getgrnam('tahor-bridge').gr_gid
    run('usermod', '-a', '-G', 'tahor-bridge', 'tahor')
    directory(BRIDGE, 0, bridge_gid, 0o710)
    directory(BRIDGE/'inbox', app.pw_uid, bridge_gid, 0o2750)
    directory(BRIDGE/'outbox', provider.pw_uid, bridge_gid, 0o2750)
    directory(BRIDGE/'private', provider.pw_uid, provider.pw_gid, 0o700)
    config_dir = Path('/etc/tahor-provider')
    directory(config_dir, 0, 0, 0o700)
    config_path = config_dir/'config.json'
    if not config_path.exists():
        atomic_write(config_path, json.dumps({'bridge': str(BRIDGE), 'state': str(BRIDGE/'private'),
                    'request_uid': app.pw_uid, 'installation': uuid.uuid4().hex}))
    credential_dir = Path('/etc/credstore.encrypted')
    directory(credential_dir, 0, 0, 0o700)
    encrypted = credential_dir/'tahor-fastmail'
    if not encrypted.exists():
        subprocess.run(['systemd-creds', 'encrypt', '--name=fastmail', '-', str(encrypted)], input=b'{}', check=True, capture_output=True)
        encrypted.chmod(0o600)
    for name, text in render(ROOT).items():
        path = Path('/etc/systemd/system')/name
        atomic_write(path, text)
        path.chmod(0o644)
    for name in ('tahor-web', 'tahor-decision-app', 'tahor-backlog-worker', 'tahor-draft-replies', 'tahor-decisions', 'tahor-subscription-actions', 'tahor-decision-actions'):
        # Web actions and approved background decisions may refresh managed rules.
        if not Path('/etc/systemd/system', name+'.service').exists():
            continue
        dropin = Path('/etc/systemd/system', name+'.service.d')
        dropin.mkdir(exist_ok=True)
        atomic_write(dropin/'30-provider-bridge.conf', '[Service]\nSupplementaryGroups=tahor-bridge\nEnvironment=TAHOR_PROVIDER_BRIDGE=/var/lib/tahor-provider\nReadWritePaths=/var/lib/tahor-provider/inbox\nInaccessiblePaths=/var/lib/tahor-provider/private /etc/tahor-provider /etc/credstore.encrypted\n')
    if not (BRIDGE/'inbox/desired.json').exists():
        from provider_connector.rules import digest
        atomic_write(BRIDGE/'inbox/desired.json', json.dumps({'version': 1, 'enabled': False, 'domains': [], 'digest': digest([])}))
        os.chown(BRIDGE/'inbox/desired.json', app.pw_uid, bridge_gid)
        (BRIDGE/'inbox/desired.json').chmod(0o640)
    if Path('/usr/sbin/restorecon').exists():
        run('restorecon', '-R', '/etc/systemd/system', str(BRIDGE), str(config_dir), str(credential_dir))
    run('systemd-analyze', 'verify', '/etc/systemd/system/tahor-provider.service', '/etc/systemd/system/tahor-provider-check.service')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'tahor-provider.service')
    print('Connector installed; new installations start disabled. Existing sync settings are preserved.\n'
          'Restart the web service to load bridge permissions.\n'
          'Enroll using: sudo /opt/tahor/venv/bin/python /opt/tahor/scripts/enroll_fastmail.py')


if __name__ == '__main__':
    main()
