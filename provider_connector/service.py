"""Poll a narrow local request file; expose no network listener or credential API."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import time

from data_changes import atomic_write
from provider_connector.auth import FastmailAuth, ConnectorError, CredentialError, private_json
from provider_connector.rules import RuleManager, domains, digest


def read_desired(path, uid):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o027 or info.st_size > 65536:
            raise ValueError()
        content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError()
        value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != {'version', 'enabled', 'domains', 'digest'}
            or value['version'] != 1 or type(value['enabled']) is not bool):
        raise ValueError()
    values = domains(value['domains'])
    if value['digest'] != digest(values):
        raise ValueError()
    return value


def once(config):
    bridge = Path(config['bridge'])
    state = Path(config['state'])
    result = {'state': 'disabled', 'updated_at': time.time()}
    try:
        desired = read_desired(bridge / 'inbox/desired.json', config['request_uid'])
        result['digest'] = desired['digest']
        if desired['enabled']:
            auth = FastmailAuth(config['credentials'], state)
            auth.ensure_session()
            def guard():
                if read_desired(bridge / 'inbox/desired.json', config['request_uid']) != desired:
                    raise ValueError()
            manager = RuleManager(auth, state, config['installation'], guard=guard)
            result['count'] = manager.sync(desired['domains'])
            result['state'] = 'installed'
    except FileNotFoundError:
        result['state'] = 'credentials_required'
    except ConnectorError as error:
        result['state'] = error.code
    except (ValueError, TypeError, KeyError, OSError):
        result['state'] = 'invalid_request'
    except Exception:
        result['state'] = 'provider_unavailable'
    # No provider exception text, response bodies, domains, credentials or session IDs.
    output = bridge / 'outbox/status.json'
    atomic_write(output, json.dumps(result))
    output.chmod(0o640)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check-auth', action='store_true')
    args = parser.parse_args()
    config = private_json(args.config)
    if os.environ.get('CREDENTIALS_DIRECTORY'):
        config['credentials'] = str(Path(os.environ['CREDENTIALS_DIRECTORY']) / 'fastmail')
    lock_path = Path(config['state']) / 'service.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            if args.check_auth:
                try:
                    auth = FastmailAuth(config['credentials'], config['state'])
                    auth.session = None
                    auth.http.cookies.clear()
                    auth.auth_state = {'user_id': auth.auth_state.get('user_id')}
                    auth.ensure_session()
                    auth.ensure_settings_auth()
                except Exception:
                    print('Fastmail sign-in failed. Check protected enrollment and connector status.')
                    raise SystemExit(1)
                print('Fresh Fastmail sign-in and settings reauthentication verified.')
                return
            result = once(config)
            print('Fastmail connector: ' + result['state'], flush=True)
            if args.once:
                return
            time.sleep(300)


if __name__ == '__main__':
    main()
