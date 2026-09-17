"""Owner-authorized subscription batches with durable, non-repeating delivery."""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

ACTIONS = ('unsubscribe_block_marketing', 'unsubscribe', 'block_all', 'dismiss')


def _db():
    import tahor_db
    db = tahor_db.get_db()
    with db:
        db.executescript('''CREATE TABLE IF NOT EXISTS subscription_batches (
            id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscription_actions (
            id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, candidate_id INTEGER NOT NULL,
            action TEXT NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT, outcome TEXT, UNIQUE(batch_id,candidate_id));
        CREATE UNIQUE INDEX IF NOT EXISTS subscription_one_active ON subscription_actions(candidate_id)
            WHERE status IN ('queued','sending','applying','uncertain');''')
    return db


def enqueue(selections, request_key):
    if not isinstance(request_key, str) or not 8 <= len(request_key) <= 100 or not isinstance(selections, list) or not 1 <= len(selections) <= 1000:
        raise ValueError('Select at least one action and submit a valid request identifier')
    checked = []
    for item in selections:
        if not isinstance(item, dict) or type(item.get('candidate_id')) is not int or item.get('action') not in ACTIONS:
            raise ValueError('A selected subscription action is invalid')
        checked.append((item['candidate_id'], item['action']))
    if len({item[0] for item in checked}) != len(checked):
        raise ValueError('Each subscription must have only one selected action')
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM subscription_batches WHERE request_key=?', (request_key,)).fetchone()
            if existing:
                original = [(row['candidate_id'], row['action']) for row in db.execute('SELECT candidate_id,action FROM subscription_actions WHERE batch_id=?', (existing['id'],))]
                if sorted(original) != sorted(checked):
                    raise ValueError('This request identifier already belongs to different selections')
                return get_job(existing['id'], database=db)
            rows = []
            for candidate_id, action in checked:
                row = db.execute('SELECT * FROM unsubscribe_candidates WHERE id=? AND status="pending"', (candidate_id,)).fetchone()
                if row is None:
                    raise ValueError('A selected subscription was already handled. Reload its current state.')
                active = db.execute("SELECT 1 FROM subscription_actions WHERE candidate_id=? AND status IN ('queued','sending','applying','uncertain')", (candidate_id,)).fetchone()
                if active:
                    raise ValueError('A selected subscription already has a queued or unconfirmed request. Check its result first.')
                rows.append((candidate_id, action, json.dumps(dict(row))))
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO subscription_batches VALUES(?,?,?)', (identifier, request_key, datetime.now(timezone.utc).isoformat()))
            db.executemany("INSERT INTO subscription_actions(batch_id,candidate_id,action,snapshot,status) VALUES(?,?,?,?,'queued')", [(identifier, *row) for row in rows])
        return get_job(identifier, database=db)
    finally:
        db.close()


def get_job(identifier, database=None):
    db = database if database is not None else _db()
    try:
        if not db.execute('SELECT 1 FROM subscription_batches WHERE id=?', (identifier,)).fetchone():
            raise ValueError('This batch was not found')
        items = []
        for row in db.execute('SELECT * FROM subscription_actions WHERE batch_id=? ORDER BY id', (identifier,)):
            outcome = json.loads(row['outcome'] or '{}')
            items.append(dict(candidate_id=row['candidate_id'], action=row['action'], status=row['status'],
                              message=outcome.get('message', 'Queued for background processing.'), failed=outcome.get('failed', False)))
        return dict(job_id=identifier, status='running' if any(item['status'] in ('queued','sending','applying') for item in items) else 'complete', items=items)
    finally:
        if database is None:
            db.close()


def recent_jobs():
    db = _db()
    try:
        rows = db.execute('SELECT id FROM subscription_batches ORDER BY created_at DESC LIMIT 10').fetchall()
        latest = {row['candidate_id']: row['id'] for row in db.execute('SELECT candidate_id,MAX(id) AS id FROM subscription_actions GROUP BY candidate_id')}
        results = []
        for row in rows:
            job = get_job(row['id'], database=db)
            current = {item['candidate_id'] for item in db.execute('SELECT id,candidate_id FROM subscription_actions WHERE batch_id=?', (row['id'],)) if latest[item['candidate_id']] == item['id']}
            job['items'] = [item for item in job['items'] if item['candidate_id'] in current]
            if job['items']:
                results.append(job)
        return results
    finally:
        db.close()


def _finish(db, row, outcome):
    import tahor_db
    if row['action'] in ('unsubscribe_block_marketing', 'block_all'):
        snapshot = json.loads(row['snapshot'])
        rule = 'block_all' if row['action'] == 'block_all' else 'block_marketing'
        tahor_db.set_sender_rule(snapshot['sender_domain'], rule)
        outcome['message'] += (' Marketing block saved; transactional mail remains allowed.' if rule == 'block_marketing' else ' All-mail block saved, including transactional mail.')
        try:
            import generate_sieve
            generate_sieve.refresh_sieve()
        except Exception:
            outcome['message'] += ' Worker block is active; provider rules will need refresh.'
    status = 'resolved'
    if row['action'] == 'unsubscribe':
        status = 'pending' if outcome.get('failed') else 'unsubscribed'
    with db:
        db.execute("UPDATE unsubscribe_candidates SET status=?,non_compliant=0,unsubscribed_at=CASE WHEN ?='unsubscribed' THEN ? ELSE unsubscribed_at END WHERE id=?", (status, status, datetime.now(timezone.utc).isoformat(), row['candidate_id']))
        db.execute('UPDATE subscription_actions SET status=?,outcome=? WHERE id=?', ('attention' if outcome.get('failed') else 'done', json.dumps(outcome), row['id']))


def run_pending(limit=10):
    import config
    import tahor_db
    from unsubscribe import describe_failure
    db = _db()
    processed = 0
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        with db:
            db.execute("UPDATE subscription_actions SET status='uncertain',outcome=? WHERE status='sending' AND started_at<?", (json.dumps({'failed': True, 'message': 'Processing was interrupted after the request began. Check the sender’s subscription state; this request will not be repeated automatically.'}), cutoff))
        rows = db.execute("SELECT * FROM subscription_actions WHERE status IN ('queued','applying') ORDER BY id LIMIT ?", (max(1, min(int(limit), 50)),)).fetchall()
        for row in rows:
            if row['status'] == 'queued':
                with db:
                    claimed = db.execute("UPDATE subscription_actions SET status='sending',started_at=? WHERE id=? AND status='queued'", (datetime.now(timezone.utc).isoformat(), row['id']))
                if claimed.rowcount != 1:
                    continue
                snapshot = json.loads(row['snapshot'])
                current = db.execute('SELECT * FROM unsubscribe_candidates WHERE id=?', (row['candidate_id'],)).fetchone()
                keys = ('sender_domain', 'sender_email', 'unsubscribe_url', 'unsubscribe_mailto', 'one_click')
                if current is None or current['status'] != 'pending' or any(current[key] != snapshot[key] for key in keys):
                    with db:
                        db.execute("UPDATE subscription_actions SET status='attention',outcome=? WHERE id=?", (json.dumps({'failed': True, 'message': 'This subscription changed after selection. Review its current state; no request was sent.'}), row['id']))
                    continue
                outcome = {'message': 'Subscription kept.', 'failed': False}
                if row['action'] != 'dismiss':
                    try:
                        outcome['message'] = tahor_db.execute_unsubscribe(snapshot, os.environ.get('FASTMAIL_EMAIL'), os.environ.get('FASTMAIL_APP_PASSWORD'), config.SMTP_HOST, config.SMTP_PORT)
                    except Exception as exc:
                        outcome = {'message': describe_failure(exc), 'failed': True}
                with db:
                    db.execute("UPDATE subscription_actions SET status='applying',outcome=? WHERE id=?", (json.dumps(outcome), row['id']))
            else:
                outcome = json.loads(row['outcome'])
            try:
                _finish(db, row, outcome)
                processed += 1
            except Exception:
                # Local writes are idempotent. Retry them without repeating delivery.
                continue
        return {'processed': processed}
    finally:
        db.close()


def active_candidate(candidate_id):
    db = _db()
    try:
        return bool(db.execute("SELECT 1 FROM subscription_actions WHERE candidate_id=? AND status IN ('queued','sending','applying','uncertain')", (candidate_id,)).fetchone())
    finally:
        db.close()


def main():
    """Process one bounded pass independently of slower rule/filing work."""
    # The standalone worker needs the same trusted Sieve module as the web app.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'decision-app'))
    run_pending(limit=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
