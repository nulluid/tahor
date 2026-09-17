#!/usr/bin/env python3
"""Administrator export for a trusted off-host SSH pull; never exports credentials."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
import time

from recovery_bundle import pack, provider_state
from private_backup import read_file, write_file, NAMES

ROOT = Path('/opt/tahor')
EXPORTS = Path('/var/lib/tahor-recovery-export')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ack', help='Acknowledge a snapshot verified on the receiving host')
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(__file__).resolve().parents[1] != ROOT:
        parser.error('Run the installed exporter through sudo')
    recipient = pwd.getpwnam(os.environ.get('SUDO_USER', 'root'))
    if recipient.pw_uid == 0:
        parser.error('Run through sudo from the trusted SSH backup account')
    os.umask(0o077)
    if EXPORTS.is_symlink():
        raise ValueError('Refusing linked export directory')
    EXPORTS.mkdir(mode=0o710, exist_ok=True)
    if EXPORTS.stat().st_uid != 0:
        raise ValueError('Export directory must be administrator-owned')
    os.chown(EXPORTS, 0, recipient.pw_gid)
    EXPORTS.chmod(0o710)
    lock_fd = os.open(EXPORTS / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.ack:
            if not re.fullmatch(r'recovery-[0-9TZ]+\.tar\.gz', args.ack):
                raise ValueError('Invalid recovery acknowledgement')
            archive = EXPORTS / args.ack
            if not archive.is_file() or archive.is_symlink() or archive.stat().st_uid != recipient.pw_uid:
                raise ValueError('Unknown recovery export')
            sys.path.insert(0, str(ROOT))
            from data_changes import atomic_write
            status = Path('/var/lib/tahor/runtime/state/offhost_backup.json')
            atomic_write(status, json.dumps({'verified_at': time.time(), 'snapshot': args.ack}))
            app = pwd.getpwnam('tahor')
            os.chown(status, app.pw_uid, app.pw_gid)
            print(json.dumps({'acknowledged': True}))
            return
        result = subprocess.run(['runuser', '-u', 'tahor', '--', str(ROOT / 'venv/bin/python'),
            str(ROOT / 'scripts/private_backup.py'), '--env', '/etc/tahor/config.env',
            '--destination', '/var/lib/tahor/backups'], check=True, capture_output=True, text=True, timeout=180)
        match = re.search(r'Verified private backup: (/var/lib/tahor/backups/backup-[0-9TZ]+)', result.stdout)
        if not match:
            raise ValueError('Snapshot creation did not verify')
        snapshot = Path(match[1])
        # Root may read connector state, but only ownership and account identity
        # cross this boundary. Passwords, seeds, cookies and tokens never do.
        provider = None
        config = Path('/etc/tahor-provider/config.json')
        state = Path('/var/lib/tahor-provider/private')
        if config.exists():
            # The journal is atomically replaced by the connector. Its snapshot
            # includes any uncertain intent; never block behind the daemon's
            # lifetime lock. Account/installation identity is stable while it runs.
            from_config = json.loads(read_file(config))
            rules = json.loads(read_file(state / 'rules.json')) if (state / 'rules.json').exists() else {'owned': {}, 'pending': None}
            session = json.loads(read_file(state / 'session.json')) if (state / 'session.json').exists() else {}
            provider = provider_state({'installation': from_config['installation'],
                'user_id': session.get('auth_state', {}).get('user_id'),
                'owned': rules['owned'], 'pending': rules['pending']})
        archive = EXPORTS / ('recovery-' + snapshot.name.removeprefix('backup-') + '.tar.gz')
        # Validate a root-owned copy rather than weakening the snapshot tool's
        # ownership checks for the service-owned original.
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=EXPORTS) as temporary:
            staging = Path(temporary) / snapshot.name
            staging.mkdir(mode=0o700)
            for item in snapshot.iterdir():
                if item.name not in NAMES | {'manifest.json'}:
                    raise ValueError('Unexpected snapshot file')
                write_file(staging / item.name, read_file(item))
            pack(staging, archive, provider)
        os.chown(archive, recipient.pw_uid, recipient.pw_gid)
        archives = sorted(p for p in EXPORTS.iterdir() if re.fullmatch(r'recovery-[0-9TZ]+\.tar\.gz', p.name) and p.is_file() and not p.is_symlink())
        for old in archives[:-3]:
            old.unlink()
        print(json.dumps({'archive': str(archive)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Recovery export failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
