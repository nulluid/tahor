"""Credential-free desired state and sanitized status for the optional connector."""
import fcntl
import json
import os
from pathlib import Path
import time

from data_changes import atomic_write

STATES = {
    'not_configured': 'Connector not configured',
    'disabled': 'Automatic provider rules are off',
    'pending': 'Waiting for the connector',
    'installed': 'Provider rules verified',
    'credentials_required': 'Administrator enrollment required',
    'authentication_required': 'Fastmail sign-in requires administrator attention',
    'protocol_changed': 'Fastmail interface changed; synchronization stopped',
    'rate_limited': 'Fastmail rate limit; retry scheduled',
    'provider_unavailable': 'Fastmail unavailable; retry scheduled',
    'rule_conflict': 'Provider rule conflict; administrator review required',
    'invalid_request': 'Invalid connector request; synchronization stopped',
    'stale': 'Connector status is out of date',
}


def paths():
    root = os.environ.get('TAHOR_PROVIDER_BRIDGE')
    return Path(root) if root else None


def publish(enabled=None):
    root = paths()
    if root is None:
        raise ValueError('The administrator must install the connector first.')
    with (root / 'inbox/.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish(enabled)


def _publish(enabled=None):
    root = paths()
    if root is None:
        raise ValueError('The administrator must install the connector first.')
    request = root / 'inbox' / 'desired.json'
    if enabled is None:
        try:
            enabled = json.loads(request.read_text()).get('enabled') is True
        except (OSError, ValueError):
            enabled = False
    import tahor_db
    from provider_connector.rules import domains, digest
    conn = tahor_db.get_db()
    try:
        values = domains([row['sender_domain'] for row in conn.execute("SELECT sender_domain FROM sender_rules WHERE rule='block_all'")])
    except Exception as error:
        from provider_connector.auth import ProtocolError
        if isinstance(error, ProtocolError):
            raise ValueError('At most 250 valid whole-domain rules are supported.') from None
        raise
    finally:
        conn.close()
    # Unblocking is represented by removing the corresponding domain.
    content = {'version': 1, 'enabled': bool(enabled), 'domains': values, 'digest': digest(values)}
    # The inbox directory is private to the service identities, not the repository.
    atomic_write(request, json.dumps(content, sort_keys=True))
    request.chmod(0o640)


def status():
    root = paths()
    if root is None:
        return {'state': 'not_configured', 'enabled': False, 'label': STATES['not_configured']}
    try:
        desired = json.loads((root / 'inbox/desired.json').read_text())
        enabled = desired.get('enabled') is True
    except (OSError, ValueError):
        enabled, desired = False, {}
    state = 'pending' if enabled else 'disabled'
    try:
        data = json.loads((root / 'outbox/status.json').read_text())
        if enabled:
            state = data.get('state') if data.get('state') in STATES else 'provider_unavailable'
            if time.time() - data.get('updated_at', 0) > 900:
                state = 'stale'
            elif data.get('digest') != desired.get('digest'):
                state = 'pending'
    except (OSError, ValueError, TypeError):
        pass
    return {'state': state, 'enabled': enabled, 'label': STATES[state]}
