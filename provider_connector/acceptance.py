"""Explicit administrator acceptance test using only reserved .invalid domains.

Run under the connector's hardened systemd identity and credential loading.
No credential is copied. Rule snapshots and mutation intent stay in private state.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from data_changes import atomic_write
from provider_connector.auth import FastmailAuth, private_json
from provider_connector.rules import RuleManager


def fingerprints(rules):
    return {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            for key, value in rules.items()}


def lifecycle(auth, state, fresh_login=False):
    """Exercise real sync/readback/restart/cleanup; callers serialize service access."""
    installation = uuid.uuid4().hex
    directory = Path(state) / ('acceptance-' + installation)
    directory.mkdir(mode=0o700)
    targets = [installation + '-a.invalid', installation + '-b.invalid']
    manager = RuleManager(auth, directory, installation)
    report = {'created': False, 'restart_verified': False, 'managed_change_verified': False,
              'cleanup_verified': False, 'unrelated_rules_unchanged': False,
              'settings_renewed': False, 'fresh_login_verified': False}
    baseline = manager.get()
    baseline_hashes = fingerprints(baseline)
    atomic_write(directory / 'baseline.json', json.dumps(baseline_hashes, sort_keys=True))
    atomic_write(directory / 'intent.json', json.dumps({'installation': installation, 'domains': targets}))
    error = None
    try:
        if fresh_login:
            if (auth.auth_state.get('blocked')
                    or auth.auth_state.get('next_login', 0) > time.time()):
                raise ValueError('Fresh login guard not ready')
            auth.session = None
            auth.http.cookies.clear()
            auth.ensure_session()
            report['fresh_login_verified'] = True
        # Expire only our cached settings authorization, not the provider session
        # or any login/rejection/cooldown guard. The provider decides the challenge.
        auth.auth_state.pop('settings_until', None)
        auth.save()
        auth.ensure_settings_auth()
        report['settings_renewed'] = True
        manager.sync(targets[:1])
        report['created'] = True  # sync includes returned-ID journal and readback.
        restarted = FastmailAuth(auth.credentials_path, auth.state_dir)
        restarted.ensure_session()
        manager = RuleManager(restarted, directory, installation)
        if manager.sync(targets[:1]) != 1:
            raise ValueError('Restart reconciliation failed')
        report['restart_verified'] = True
        # Editing the managed domain list is a journaled remove/create operation;
        # the production connector does not expose arbitrary Rule/update calls.
        if manager.sync(targets[1:]) != 1:
            raise ValueError('Managed domain change failed')
        report['managed_change_verified'] = True
    except Exception as exc:
        error = type(exc).__name__  # Written privately; no exception message.
    finally:
        try:
            manager.sync([])
            final = manager.get()
            report['cleanup_verified'] = not manager.journal['owned'] and not manager.journal.get('pending')
            report['unrelated_rules_unchanged'] = fingerprints(final) == baseline_hashes
        except Exception as exc:
            error = error or type(exc).__name__
        report['success'] = (error is None and report['cleanup_verified']
                             and report['unrelated_rules_unchanged'])
        atomic_write(directory / 'result.json', json.dumps(dict(report, error_class=error), sort_keys=True))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--live-invalid-domain-test', action='store_true', required=True)
    parser.add_argument('--fresh-login', action='store_true')
    args = parser.parse_args()
    config = private_json(args.config)
    credentials = os.environ.get('CREDENTIALS_DIRECTORY')
    if not credentials:
        raise SystemExit('Run with isolated systemd credential loading')
    config['credentials'] = str(Path(credentials) / 'fastmail')
    with (Path(config['state']) / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = FastmailAuth(config['credentials'], config['state'])
        auth.ensure_session()
        result = lifecycle(auth, config['state'], args.fresh_login)
    print(json.dumps(result, sort_keys=True), flush=True)
    if not result['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Connector acceptance could not complete; inspect protected status.', flush=True)
        raise SystemExit(1)
