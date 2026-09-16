"""Small, private status snapshot shared by the worker and web app."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from data_changes import atomic_write


def status_path():
    default = Path(os.environ.get('TAHOR_DB_PATH', Path(__file__).resolve().parent / 'decisions.db')).parent / 'worker_status.json'
    return Path(os.environ.get('TAHOR_STATUS_PATH', default))


def read_status():
    try:
        value = json.loads(status_path().read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(state, **fields):
    snapshot = read_status()
    snapshot.update(fields, state=state, updated_at=datetime.now(timezone.utc).isoformat())
    atomic_write(status_path(), json.dumps(snapshot, indent=2) + '\n')
    return snapshot


def describe_status(snapshot=None):
    snapshot = read_status() if snapshot is None else snapshot
    if not snapshot:
        return 'No worker activity recorded yet.'
    try:
        updated = datetime.fromisoformat(snapshot['updated_at'])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 'Worker status could not be read.'
    if age > 1800:
        return 'Worker has not reported for over 30 minutes. Check the service logs.'
    return {
        'fetching': 'Checking for unclassified mail.',
        'classifying': 'Classifying a batch of messages.',
        'applying': 'Saving message tags to the mailbox.',
        'processed': 'Processing mail normally.',
        'idle': 'Caught up. Watching for new mail.',
        'retrying': 'Waiting to retry after a backend failure.',
        'error': 'A processing step failed. Automatic retry is scheduled.',
    }.get(snapshot.get('state'), 'Worker is starting.')
