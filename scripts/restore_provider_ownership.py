#!/usr/bin/env python3
"""Restore connector rule ownership after enrollment on a replacement host."""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

from recovery_bundle import validate_completed, provider_state
from private_backup import read_file


def restore_ownership(bundle, config_path, state, provider_uid, provider_gid):
    validate_completed(bundle)
    value = provider_state(json.loads(read_file(Path(bundle) / 'provider-ownership.json')))
    session = json.loads(read_file(state / 'session.json'))
    bound = session.get('auth_state', {}).get('user_id')
    if not bound or bound != value['user_id']:
        raise ValueError('Enroll and verify the same Fastmail account before restoring ownership')
    current = json.loads(read_file(state / 'rules.json')) if (state / 'rules.json').exists() else {'owned': {}, 'pending': None}
    desired = {'owned': value['owned'], 'pending': value['pending']}
    if (current['owned'] or current['pending']) and current != desired:
        raise ValueError('Existing rule ownership differs; administrator reconciliation required')
    config = json.loads(read_file(config_path))
    config['installation'] = value['installation']
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from data_changes import atomic_write
    # Services are stopped. The operation is retryable if interrupted between files.
    atomic_write(config_path, json.dumps(config, sort_keys=True))
    atomic_write(state / 'rules.json', json.dumps(desired, sort_keys=True))
    os.chown(state / 'rules.json', provider_uid, provider_gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--connector-stopped', action='store_true', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Administrator execution required')
    for unit in ('tahor-provider.service', 'tahor-provider-check.service'):
        state = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        if state not in ('inactive', 'failed'):
            parser.error('Stop the connector and its check service before restoring')
    desired = Path('/var/lib/tahor-provider/inbox/desired.json')
    if json.loads(read_file(desired)).get('enabled'):
        parser.error('Disable automatic provider synchronization before restoring')
    identity = pwd.getpwnam('tahor-provider')
    restore_ownership(args.bundle, Path('/etc/tahor-provider/config.json'),
                      Path('/var/lib/tahor-provider/private'), identity.pw_uid, identity.pw_gid)
    print('Provider ownership restored. Review existing provider rules before enabling synchronization.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Provider ownership restore failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
