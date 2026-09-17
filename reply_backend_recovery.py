"""Private reply-provider cooldowns; no source text, credentials, or responses."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from data_changes import atomic_write
import tahor_db

COOLDOWN_SECONDS = 300


class ReplyBackendError(ValueError):
    """Provider/configuration failure, separate from a rejected draft's quality."""


def state_path():
    return tahor_db.DB_PATH.parent / 'reply_backend_state.json'


def identity(key, backend):
    return hashlib.sha256(json.dumps([key, backend], sort_keys=True).encode()).hexdigest()


@contextmanager
def locked_state():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            value = json.loads(path.read_text())
            state = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            state = {}
        yield path, state


def cooling_down(key, backend, now=None):
    now = time.time() if now is None else now
    with locked_state() as (_, state):
        entry = state.get(identity(key, backend), {})
        until = entry.get('retry_at', 0) if isinstance(entry, dict) else 0
        # Bound the hold even when a saved timestamp is corrupt or clocks move.
        return isinstance(until, (int, float)) and now < until <= now + COOLDOWN_SECONDS


def record_failure(key, backend, now=None):
    now = time.time() if now is None else now
    with locked_state() as (path, state):
        state = {key: value for key, value in state.items() if isinstance(value, dict) and isinstance(value.get('retry_at'), (int, float)) and value['retry_at'] > now}
        state[identity(key, backend)] = {'retry_at': now + COOLDOWN_SECONDS}
        atomic_write(path, json.dumps(state, sort_keys=True)+'\n')
        path.chmod(0o600)


def record_success(key, backend):
    with locked_state() as (path, state):
        state.pop(identity(key, backend), None)
        atomic_write(path, json.dumps(state, sort_keys=True)+'\n')
        path.chmod(0o600)
