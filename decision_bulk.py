"""Durable owner-confirmed decision batches using the existing guarded executor."""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

KINDS = ('message_review', 'vendor_mapping')


def decision_revision(row):
    return hashlib.sha256(json.dumps([row[key] for key in ('id', 'kind', 'status', 'context', 'resolution')], ensure_ascii=True).encode()).hexdigest()


def eligible(row):
    return row is not None and row['kind'] in KINDS and row['status'] == 'pending' and not row['resolution']


def validate_choice(row, item):
    action = item.get('action')
    allowed = ('keep', 'keep_brief', 'trash', 'skip') if row['kind'] == 'message_review' else ('map', 'unsorted')
    if action not in allowed:
        raise ValueError('This action is not available for this decision.')
    choice = {'action': 'skip' if action == 'unsorted' else action}
    if action == 'map':
        for key in ('bucket', 'vendor_name'):
            value = item.get(key)
            if (not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 120
                    or any(ord(c) < 32 or c in '"\\' for c in value)
                    or any(part in ('', '.', '..') for part in value.split('/'))
                    or (key == 'vendor_name' and '/' in value)):
                raise ValueError('Enter a safe relative folder and vendor name.')
            choice[key] = value
    return choice


class SelectionConflict(ValueError):
    def __init__(self, ids):
        super().__init__('Some decisions changed or were already submitted. Review the remaining choices.')
        self.unavailable_ids = sorted(set(ids))



def record_choice_feedback(db, row, choice):
    if not isinstance(choice, dict) or choice.get('automatic_vendor_mapping'):
        return
    if choice.get('action') not in ('keep','keep_brief','trash','skip','map','unsorted'):
        return
    db.execute("CREATE TABLE IF NOT EXISTS decision_choice_feedback(id INTEGER PRIMARY KEY,decision_id INTEGER NOT NULL,kind TEXT NOT NULL,sender_email TEXT NOT NULL,choice TEXT NOT NULL,created_at REAL NOT NULL)")
    try: context = json.loads(row['context'] or '{}')
    except (ValueError, TypeError): context = {}
    sender = str(context.get('sender_email') or context.get('sender') or context.get('routing_key') or '')[:320] if isinstance(context, dict) else ''
    safe = {key:choice[key] for key in ('action','bucket','vendor_name') if key in choice}
    db.execute('INSERT INTO decision_choice_feedback(decision_id,kind,sender_email,choice,created_at) VALUES(?,?,?,?,?)', (row['id'],row['kind'],sender,json.dumps(safe),time.time()))


def record_skip_feedback(db, row):
    record_choice_feedback(db, row, {'action':'skip'})


def _db():
    import tahor_db
    db = tahor_db.get_db()
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS decision_batches(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,selections TEXT NOT NULL,created_at REAL NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS decision_batch_items(batch_id TEXT NOT NULL,decision_id INTEGER NOT NULL,
            status TEXT NOT NULL,resolution TEXT NOT NULL,retry_at REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(batch_id,decision_id))''')
    return db


def enqueue(selections, request_key):
    if not isinstance(request_key, str) or not 8 <= len(request_key) <= 100 or not isinstance(selections, list) or not 1 <= len(selections) <= 1000:
        raise ValueError('Choose decisions and supply a request identifier.')
    if any(not isinstance(item, dict) or type(item.get('decision_id')) is not int or not isinstance(item.get('source_revision'), str) for item in selections):
        raise ValueError('Invalid decision selections.')
    if len({item['decision_id'] for item in selections}) != len(selections):
        raise ValueError('Choose only one action per decision.')
    encoded = json.dumps(sorted(selections, key=lambda item:item['decision_id']), sort_keys=True)
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM decision_batches WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                if prior['selections'] != encoded:
                    raise ValueError('The request identifier belongs to different choices.')
                return get_job(prior['id'], db)
            checked, unavailable = [], []
            for item in selections:
                row = db.execute('SELECT * FROM decisions WHERE id=?', (item['decision_id'],)).fetchone()
                active = db.execute("SELECT 1 FROM decision_batch_items WHERE decision_id=? AND status IN ('queued','running')", (item['decision_id'],)).fetchone()
                if not eligible(row) or active or decision_revision(row) != item['source_revision']:
                    unavailable.append(item['decision_id']); continue
                checked.append((row, validate_choice(row, item)))
            if unavailable:
                raise SelectionConflict(unavailable)
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO decision_batches VALUES(?,?,?,?)', (identifier,request_key,encoded,time.time()))
            for row, choice in checked:
                skip = row['kind'] == 'message_review' and choice['action'] == 'skip'
                resolution = json.dumps(choice)
                # Skip leaves a review pending and cannot authorize mailbox work.
                record_choice_feedback(db, row, choice)
                if not skip:
                    db.execute("UPDATE decisions SET status='resolved',resolution=?,resolved_at=? WHERE id=?", (resolution,datetime.now(timezone.utc).isoformat(),row['id']))
                db.execute('INSERT INTO decision_batch_items(batch_id,decision_id,status,resolution) VALUES(?,?,?,?)', (identifier,row['id'],'done' if skip else 'queued',resolution))
        return get_job(identifier, db)
    finally:
        db.close()


def get_job(identifier, database=None):
    db = database if database is not None else _db()
    try:
        if not db.execute('SELECT 1 FROM decision_batches WHERE id=?',(identifier,)).fetchone():
            raise ValueError('Decision batch not found.')
        items = [dict(decision_id=row['decision_id'],status=row['status'],message=row['error'] or ('Choice saved.' if row['status']=='done' else 'Saved; waiting for background processing.'), retry_allowed=False) for row in db.execute('SELECT * FROM decision_batch_items WHERE batch_id=? ORDER BY decision_id',(identifier,))]
        return dict(job_id=identifier,status='running' if any(item['status'] in ('queued','running') for item in items) else 'complete',items=items)
    finally:
        if database is None: db.close()


def recent_jobs():
    db = _db()
    try:
        jobs=[]
        for row in db.execute('SELECT id FROM decision_batches ORDER BY created_at DESC LIMIT 10'):
            job=get_job(row[0],db)
            skipped={item[0] for item in db.execute("SELECT i.decision_id FROM decision_batch_items i JOIN decisions d ON d.id=i.decision_id WHERE i.batch_id=? AND i.status='done' AND d.kind='message_review' AND i.resolution=?",(row[0],json.dumps({'action':'skip'})))}
            job['items']=[item for item in job['items'] if item['decision_id'] not in skipped]
            if job['items']: jobs.append(job)
        return jobs
    finally: db.close()


def run_pending(limit=10):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parent/'decision-app'))
    import apply_decisions
    db = _db()
    processed = 0
    try:
        for item in db.execute("SELECT * FROM decision_batch_items WHERE status IN ('queued','running') AND retry_at<=? ORDER BY rowid LIMIT ?", (time.time(),max(1,min(limit,50)))).fetchall():
            with db:
                claimed = db.execute("UPDATE decision_batch_items SET status='running',retry_at=? WHERE batch_id=? AND decision_id=? AND retry_at<=?", (time.time()+600,item['batch_id'],item['decision_id'],time.time())).rowcount
            if not claimed: continue
            row = db.execute('SELECT * FROM decisions WHERE id=?',(item['decision_id'],)).fetchone()
            if row is None or row['resolution'] != item['resolution']:
                with db: db.execute("UPDATE decision_batch_items SET status='attention',error='The saved choice changed; no old action was repeated.' WHERE batch_id=? AND decision_id=?",(item['batch_id'],item['decision_id']))
                continue
            try:
                apply_decisions.apply_one(item['decision_id'])
                with db: db.execute("UPDATE decision_batch_items SET status='done',error='' WHERE batch_id=? AND decision_id=?",(item['batch_id'],item['decision_id']))
                processed += 1
            except Exception:
                with db: db.execute("UPDATE decision_batch_items SET status='queued',retry_at=?,error='The saved choice is waiting for mailbox recovery; automatic retries continue.' WHERE batch_id=? AND decision_id=?",(time.time()+300,item['batch_id'],item['decision_id']))
        return processed
    finally: db.close()


def main():
    run_pending()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
