#!/usr/bin/env python3
"""Opt-in self-addressed operational mail; no message content or model calls."""
from datetime import datetime, timezone
from email.message import EmailMessage
import fcntl
import hashlib
import json
import os
from pathlib import Path
import email
import imaplib
import re
import time
import uuid
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import config
import fetch_batch
from mailbox_paths import list_mailboxes, quote_mailbox
from data_changes import atomic_write
import provider_bridge
import runtime_status
import tahor_db

CHECK_INTERVAL = 900
PROGRESS_TIMEOUT = 3600
PROBLEM_LABELS = {
    'ai_classification': 'Some email classification work has remained pending for at least 30 minutes.',
    'ai_reply': 'Some reply drafting work has remained pending for at least 30 minutes.',
    'ai_subscriptions': 'Some subscription recommendations have remained pending for at least 30 minutes.',
    'ai_rule': 'Some rule drafting work has remained pending for at least 30 minutes.',
    'worker_missing': 'The email worker has not published a readable status.',
    'worker_stale': 'The email worker has not reported activity for over an hour.',
    'worker_stalled': 'The email worker has not completed a batch for over an hour.',
    'connector_auth': 'The optional Fastmail connector needs administrator enrollment or sign-in.',
    'connector_stopped': 'The optional Fastmail connector needs administrator review.',
    'backup_stale': 'A verified off-host recovery copy is overdue. Check the backup computer and its scheduled SSH transfer.',
}


def state_path():
    return Path(os.environ.get('TAHOR_NOTIFICATION_STATE', str(tahor_db.DB_PATH.parent / 'notifications.json')))


def timestamp(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.timestamp() if date.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def inspect_health(state, now):
    snapshot = runtime_status.read_status()
    updated = timestamp(snapshot.get('updated_at'))
    progress = timestamp(snapshot.get('last_success_at'))
    # Observe progress across checks, even when a retrying worker updates its heartbeat.
    if snapshot.get('state') == 'idle' or (progress and progress != state.get('last_progress')):
        state['activity_since'] = now
    state.setdefault('activity_since', now)
    state['last_progress'] = progress
    problems = []
    if updated is None:
        problems.append('worker_missing')
    elif now - updated > PROGRESS_TIMEOUT:
        problems.append('worker_stale')
    elif snapshot.get('state') != 'idle' and now - max(progress or 0, state['activity_since']) > PROGRESS_TIMEOUT:
        problems.append('worker_stalled')
    connector = provider_bridge.status()
    if connector.get('enabled'):
        if connector.get('state') in ('credentials_required', 'authentication_required'):
            problems.append('connector_auth')
        elif connector.get('state') in ('protocol_changed', 'rule_conflict', 'invalid_request', 'stale'):
            problems.append('connector_stopped')
    import ai_routing
    problems.extend(ai_routing.persistent_problems(now))
    if os.environ.get('TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS'):
        try:
            hours = float(os.environ['TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS'])
            if not 1 <= hours <= 8760:
                raise ValueError()
            path = Path(os.environ.get('TAHOR_OFFHOST_BACKUP_STATUS', '/var/lib/tahor/runtime/state/offhost_backup.json'))
            verified = json.loads(path.read_text()).get('verified_at')
            if type(verified) not in (int, float) or not now - hours * 3600 <= verified <= now + 300:
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            problems.append('backup_stale')
    healthy = 'Caught up; watching for new mail.' if snapshot.get('state') == 'idle' and not problems else ('Processing mail normally.' if not problems else 'Administrator attention may be needed.')
    return problems, healthy


def digest_counts():
    database = tahor_db.get_db()
    try:
        pending = database.execute("SELECT COUNT(*) FROM decisions WHERE status='pending'").fetchone()[0]
        # Historical local journal, not a live count of drafts still in Fastmail.
        drafted = database.execute("SELECT COUNT(*) FROM reply_drafts WHERE status IN ('pending','reviewed') AND created_at>=?",
                                   (datetime.fromtimestamp(time.time()-86400, timezone.utc).isoformat(),)).fetchone()[0]
        retrying = database.execute("SELECT COUNT(*) FROM reply_drafts WHERE status='preparing'").fetchone()[0]
        return pending, drafted, retrying
    finally:
        database.close()


def enabled(name):
    value = os.environ.get(name, '0')
    if value not in ('0', '1'):
        raise ValueError('Notification opt-in must be 0 or 1')
    return value == '1'


def persist(path, state):
    atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    path.chmod(0o600)


def notification_message_id(event_id):
    """Stable identity shared by delivery reconciliation and digest retention."""
    return '<tahor-notification-' + hashlib.sha256(event_id.encode()).hexdigest() + '@localhost>'


def website_url():
    """Return the configured browser origin without accepting secret-bearing URLs."""
    value = os.environ.get('BASE_URL', '')
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or '\\' in value):
            return None
        parsed.port  # Reject malformed or out-of-range ports before rendering.
    except ValueError:
        return None
    return value.rstrip('/') + '/'


def make_message(event, event_id):
    owner = config.email_address()
    if '\r' in owner or '\n' in owner or owner.count('@') != 1:
        raise ValueError('Invalid owner address')
    message = EmailMessage()
    message['From'] = owner
    message['To'] = owner
    message['Subject'] = event['subject']
    message['Auto-Submitted'] = 'auto-generated'
    message['X-Tahor-Notification'] = '1'
    message['Message-ID'] = notification_message_id(event_id)
    body = event['body']
    if event.get('kind') == 'digest':
        message['X-Tahor-Notification-Kind'] = 'digest'
        url = website_url()
        if url and not event.get('body_prepared'):
            body = body.rstrip() + '\n\nOpen Tahor: ' + url + '\n'
    message.set_content(body)
    return owner, message


def find_notice(client, message_id, all_folders=False):
    mailboxes = [name for name, _ in list_mailboxes(client)] if all_folders else ['INBOX']
    for mailbox in mailboxes:
        if client.select(quote_mailbox(mailbox), readonly=True)[0] != 'OK':
            raise RuntimeError('Notification mailbox cannot be checked')
        status, rows = client.uid('SEARCH', None, 'HEADER', 'Message-ID', '"'+message_id+'"')
        if status != 'OK':
            raise RuntimeError('Notification search failed')
        for uid in rows[0].split() if rows and rows[0] else []:
            status, items = client.uid('FETCH', uid, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO X-TAHOR-NOTIFICATION)])')
            matches = [item for item in (items or []) if isinstance(item, tuple)]
            if status != 'OK' or len(matches) != 1:
                raise RuntimeError('Notification identity could not be checked')
            metadata, headers = matches[0]
            actual_uid = re.search(rb'\bUID (\d+)\b', metadata)
            if not actual_uid or actual_uid[1] != uid:
                raise RuntimeError('Notification UID changed')
            message = email.message_from_bytes(headers)
            if message.get_all('Message-ID', []) != [message_id]:
                continue  # HEADER search is a substring match, not exact identity.
            if (message.get_all('From', []) != [config.email_address()] or
                    message.get_all('To', []) != [config.email_address()] or
                    message.get_all('X-Tahor-Notification', []) != ['1']):
                raise RuntimeError('Notification headers conflict with its identity')
            return True
    return False


def deliver(path, state, event_id, now):
    event = state['events'][event_id]
    if event['status'] == 'sent' or event.get('next_attempt', 0) > now:
        return False
    # Preserve the old SMTP uncertainty boundary during migration. Known rejected
    # SMTP attempts are pending and can safely use the mailbox transport instead.
    if event['status'] == 'uncertain' and event.get('transport') != 'imap':
        return False
    _, message = make_message(event, event_id)
    # Save the exact rendered plain-text body before APPEND. Retention uses the
    # private journal to verify digest identity; retries reuse these same bytes.
    event.update(body=message.get_content(), body_prepared=True)
    client = None
    uncertain = event['status'] == 'uncertain'
    try:
        client = fetch_batch.connect()
        if find_notice(client, message['Message-ID'], all_folders=uncertain):
            event.update(status='sent', sent_at=now, transport='imap')
            persist(path, state)
            return True
        event.update(status='uncertain', transport='imap')
        persist(path, state)  # A crash after APPEND must trigger reconciliation.
        uncertain = True
        flags = '(category-notification retention-standard)'
        if event.get('kind') == 'digest':
            flags = '(category-notification category-tahor-digest retention-standard)'
        status, _ = client.append('INBOX', flags,
                                 imaplib.Time2Internaldate(now), message.as_bytes())
        if status != 'OK':
            uncertain = False  # An explicit NO/BAD means APPEND was rejected.
            raise RuntimeError('Notification append rejected')
        if not find_notice(client, message['Message-ID']):
            raise RuntimeError('Notification append not yet confirmed')
        event.update(status='sent', sent_at=now, transport='imap')
        persist(path, state)
        return True
    except Exception:
        pass  # Never copy credentials, provider responses, or message bodies to logs.
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
    attempts = event.get('attempts', 0) + 1
    event.update(status='uncertain' if uncertain else 'pending', attempts=attempts,
                 next_attempt=now + min(14400, CHECK_INTERVAL * 2 ** min(attempts-1, 4)))
    persist(path, state)
    print('Notification mailbox write needs retry or confirmation; private state records the outcome.', flush=True)
    return False


def run(now=None):
    health, digest = enabled('TAHOR_NOTIFY_HEALTH'), enabled('TAHOR_NOTIFY_DIGEST')
    if not health and not digest:
        return 0
    now = time.time() if now is None else now
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            # Corrupt state fails closed rather than losing the deduplication ledger.
            state = json.loads(path.read_text())
            if not isinstance(state, dict) or not isinstance(state.get('events'), dict):
                raise ValueError('Notification state needs administrator repair')
        else:
            state = {'events': {}, 'problems': {}}
        if now - state.get('last_check', 0) >= CHECK_INTERVAL:
            problems, summary = inspect_health(state, now)
            previous = state.get('problems', {})
            state['problems'] = {}
            for problem in problems:
                row = previous.get(problem, {'count': 0, 'incident': uuid.uuid4().hex})
                row['count'] += 1
                state['problems'][problem] = row
                if health and (row['count'] >= 3 or problem.startswith('ai_')):
                    event_id = 'health:'+problem+':'+row['incident']
                    state['events'].setdefault(event_id, dict(kind='health', status='pending', created_at=now,
                        subject='Tahor needs attention', body=PROBLEM_LABELS[problem]+'\n\nOrdinary automatic recovery continues. Check Tahor Settings and service status.\n'))
            state.update(last_check=now, health_summary=summary)
        if digest:
            zone = ZoneInfo(os.environ.get('TAHOR_NOTIFY_TIMEZONE', 'UTC'))
            hour = int(os.environ.get('TAHOR_NOTIFY_HOUR', '9'))
            if not 0 <= hour <= 23:
                raise ValueError('Digest hour must be between 0 and 23')
            local = datetime.fromtimestamp(now, zone)
            event_id = 'digest:'+local.date().isoformat()
            if local.hour >= hour and event_id not in state['events']:
                pending, drafted, retrying = digest_counts()
                body = ('Tahor daily summary\n\n'+state.get('health_summary', 'Worker status unavailable.')+
                        f'\nPending decisions: {pending}\nDrafts prepared in the last 24 hours: {drafted}\nDraft preparations awaiting retry: {retrying}\n\nDraft counts are from Tahor’s local journal, not the current mailbox Drafts count. Review replies in Fastmail and rules in Tahor Settings.\n')
                state['events'][event_id] = dict(kind='digest', status='pending', created_at=now, subject='Tahor daily summary', body=body)
        # Keep sent digest evidence for safe cleanup even after a long outage.
        # Other old outcomes expire after 90 days; active incidents are retained.
        active_ids = {'health:'+key+':'+row['incident'] for key, row in state.get('problems', {}).items()}
        state['events'] = {key: value for key, value in state['events'].items() if key in active_ids or (value.get('kind') == 'digest' and value.get('status') == 'sent') or now-value['created_at'] < 90*86400}
        persist(path, state)
        sent = 0
        for key, event in state['events'].items():
            if (event['kind'] == 'health' and not health) or (event['kind'] == 'digest' and not digest):
                continue
            # Do not send outdated digests or alerts for recovered incidents.
            if event['kind'] == 'health' and key not in active_ids:
                continue
            if event['kind'] == 'digest' and now-event['created_at'] > 86400:
                continue
            sent += deliver(path, state, key, now)
        return sent


if __name__ == '__main__':
    try:
        print(f'Notifications delivered: {run()}')
    except Exception:
        print('Notification check failed; inspect private configuration and state.', flush=True)
        raise SystemExit(1) from None
