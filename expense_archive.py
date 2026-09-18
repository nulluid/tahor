"""Private, immutable original receipt emails and portable accounting exports.

Originals live in decisions.db, so SQLite snapshots and the existing verified
SSH off-host recovery schedule include them with the ledger and owner notes.
"""
from datetime import datetime, timezone
import email
from email.utils import getaddresses
import hashlib
import json
import re
import tempfile
import time
import unicodedata
import zipfile

MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SCHEMA = '''CREATE TABLE IF NOT EXISTS expense_originals (
 entry_id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 raw_message BLOB NOT NULL, archived_at TEXT NOT NULL)'''


def _db():
    import business_ledger
    db = business_ledger._db()
    with db:
        db.execute(SCHEMA)
        db.execute('CREATE TABLE IF NOT EXISTS expense_archive_attempts (entry_id INTEGER PRIMARY KEY, attempted_at REAL NOT NULL, retry_after REAL NOT NULL)')
    return db


def _identity(raw, row):
    import fetch_batch
    message = email.message_from_bytes(raw)
    identifiers = message.get_all('Message-ID', [])
    expected = row['message_id']
    exact = len(identifiers) == 1 and str(identifiers[0]).strip() == expected
    local = not identifiers and (expected == fetch_batch.local_message_id(row['mailbox'], str(row['uidvalidity']), str(row['uid'])) or expected == '<tahor-content-' + hashlib.sha256(raw[:32768]).hexdigest() + '@localhost>')
    senders = getaddresses(message.get_all('From', []))
    if not (exact or local) or len(senders) != 1 or senders[0][1].lower() != row['sender_email'].lower():
        raise ValueError('Original receipt identity did not match its ledger entry')
    return message


def store_verified(entry_id, raw):
    """Store a complete captured source; differing originals never overwrite it."""
    import business_ledger
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise ValueError('Original receipt exceeds the archive size limit')
    row = business_ledger.get_entry(entry_id)
    if 'source_identity_collision' in row['review_reasons']:
        raise ValueError('Conflicting receipt identity cannot be archived')
    _identity(raw, row)
    import fetch_batch
    prefix_digest = hashlib.sha256(fetch_batch.extract_body_text(raw[:32768]).encode()).hexdigest()
    if prefix_digest != row['source_digest']:
        raise ValueError('Original receipt content did not match recorded source evidence')
    digest = hashlib.sha256(raw).hexdigest()
    db = _db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT sha256 FROM expense_originals WHERE entry_id=?', (entry_id,)).fetchone()
            if existing and existing['sha256'] != digest:
                raise ValueError('A different original is already archived; nothing was replaced')
            if not existing and db.execute('SELECT COALESCE(SUM(size_bytes),0) FROM expense_originals').fetchone()[0] + len(raw) > MAX_ARCHIVE_BYTES:
                raise ValueError('Private original-email archive reached its 512 MiB capacity; no source was deleted')
            db.execute('INSERT OR IGNORE INTO expense_originals VALUES (?,?,?,?,?)', (entry_id, digest, len(raw), raw, datetime.now(timezone.utc).isoformat()))
    finally:
        db.close()
    return digest


def read_original(entry_id):
    db = _db()
    try:
        row = db.execute('SELECT * FROM expense_originals WHERE entry_id=?', (entry_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise LookupError('Original receipt has not been archived yet')
    raw = bytes(row['raw_message'])
    if len(raw) != row['size_bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('Archived original failed its integrity check')
    return raw


def capture(entry_id, client=None, budget_seconds=20):
    """Fetch full RFC822 with attachments using PEEK, never marking it read.

    Only the saved exact UID in the saved mailbox generation is accepted. The
    ordinary filing pass refreshes locations after moves; no fuzzy ID search can
    archive a different copy. Missing originals remain explicit in exports.
    """
    import business_ledger
    import fetch_batch
    import message_reviews
    from mailbox_paths import quote_mailbox
    try:
        return hashlib.sha256(read_original(entry_id)).hexdigest()
    except LookupError:
        pass
    row = business_ledger.get_entry(entry_id)
    owns_client = client is None
    if owns_client:
        client = message_reviews._DeadlineMailbox(fetch_batch.connect(timeout=min(5, budget_seconds)), time.monotonic() + budget_seconds)
    try:
        # Shared filing clients remain writable; all reads below still use PEEK.
        if client.select(quote_mailbox(row['mailbox']), readonly=owns_client)[0] != 'OK':
            raise RuntimeError('Receipt folder is unavailable')
        if fetch_batch.mailbox_uidvalidity(client) != str(row['uidvalidity']):
            raise RuntimeError('Receipt folder generation changed')
        uid = str(row['uid'])
        status, rows = client.uid('FETCH', uid, '(UID RFC822.SIZE)')
        metadata = b' '.join(item for item in (rows or []) if isinstance(item, bytes))
        identities = re.findall(rb'\bUID (\d+)\b', metadata)
        sizes = re.findall(rb'\bRFC822.SIZE (\d+)\b', metadata)
        if status != 'OK' or identities != [uid.encode()] or len(sizes) != 1:
            raise RuntimeError('Original receipt size could not be verified')
        size = int(sizes[0])
        if not 0 < size <= MAX_MESSAGE_BYTES:
            raise ValueError('Original receipt exceeds the 50 MiB archive limit')
        status, rows = client.uid('FETCH', uid, '(UID RFC822.SIZE BODY.PEEK[]<0.' + str(size + 1) + '>)')
        items = [item for item in (rows or []) if isinstance(item, tuple)]
        if status != 'OK' or len(items) != 1:
            raise RuntimeError('Original receipt could not be read')
        metadata, raw = items[0]
        if re.findall(rb'\bUID (\d+)\b', metadata) != [uid.encode()] or re.findall(rb'\bRFC822.SIZE (\d+)\b', metadata) != [str(size).encode()] or not isinstance(raw, bytes) or len(raw) != size:
            raise RuntimeError('Original receipt was incomplete or changed')
        if fetch_batch.mailbox_uidvalidity(client) != str(row['uidvalidity']):
            raise RuntimeError('Receipt folder generation changed during capture')
        return store_verified(entry_id, raw)
    finally:
        if owns_client:
            client.logout()


def capture_missing(limit=10, budget_seconds=45):
    """Fair bounded retry worker, usable from existing filing maintenance."""
    db = _db()
    now = time.time()
    try:
        rows = db.execute('SELECT l.id FROM business_ledger l LEFT JOIN expense_originals o ON o.entry_id=l.id LEFT JOIN expense_archive_attempts a ON a.entry_id=l.id WHERE o.entry_id IS NULL AND (a.retry_after IS NULL OR a.retry_after<=?) ORDER BY COALESCE(a.attempted_at,0),l.id LIMIT ?', (now, max(1, min(int(limit), 100)))).fetchall()
        deadline = time.monotonic() + max(1, budget_seconds)
        result = {'attempted': 0, 'archived': 0, 'pending': 0}
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with db:
                claimed = db.execute('INSERT INTO expense_archive_attempts VALUES (?,?,?) ON CONFLICT(entry_id) DO UPDATE SET attempted_at=excluded.attempted_at,retry_after=excluded.retry_after WHERE expense_archive_attempts.retry_after<=?', (row['id'], now, now + 300, now))
            if claimed.rowcount != 1:
                continue
            result['attempted'] += 1
            try:
                capture(row['id'], budget_seconds=min(20, remaining))
                result['archived'] += 1
            except Exception:
                result['pending'] += 1
        return result
    finally:
        db.close()


def archive_status(rows):
    """Cheap archive coverage for download notices; export verifies full hashes."""
    db = _db()
    try:
        archived = {row[0] for row in db.execute('SELECT entry_id FROM expense_originals')}
        missing = sum(row['id'] not in archived for row in rows)
        return {'total': len(rows), 'archived': len(rows) - missing, 'missing': missing, 'complete': missing == 0}
    finally:
        db.close()


def _slug(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-zA-Z0-9]+', '-', text).strip('-').lower()[:70] or 'unknown'


def _reviews(rows):
    """Export only the audit history belonging to the selected ledger entries."""
    identifiers = sorted({int(row['id']) for row in rows})
    db = _db()
    reviews = []
    try:
        for offset in range(0, len(identifiers), 500):
            batch = identifiers[offset:offset + 500]
            query = 'SELECT * FROM business_ledger_reviews WHERE entry_id IN (' + ','.join('?' for _ in batch) + ') ORDER BY id'
            for record in db.execute(query, batch):
                review = dict(record)
                review['before'] = json.loads(review.pop('before_json'))
                review['after'] = json.loads(review.pop('after_json'))
                reviews.append(review)
        return sorted(reviews, key=lambda review: review['id'])
    finally:
        db.close()


def export_zip(rows, csv_text):
    """Return a seekable private temporary ZIP; manifest exposes every missing EML.

    Never fetch mail in the download request. Background capture supplies durable
    originals; incomplete bundles contain an explicit completeness warning.
    """
    rows = list(rows)
    manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'complete': True, 'originals': []}
    ledger_json = json.dumps(rows, ensure_ascii=False, indent=2)
    reviews_json = json.dumps(_reviews(rows), ensure_ascii=False, indent=2)
    total = sum(len(value.encode('utf-8')) for value in (csv_text, ledger_json, reviews_json))
    stream = tempfile.TemporaryFile(mode='w+b')
    try:
        if total > MAX_EXPORT_BYTES:
            raise RuntimeError('Expense export exceeds 512 MiB; download one year at a time')
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('expenses.csv', csv_text)
            archive.writestr('ledger.json', ledger_json)
            archive.writestr('reviews.json', reviews_json)
            for row in rows:
                item = {'entry_id': row['id'], 'message_id': row['message_id']}
                try:
                    raw = read_original(row['id'])
                    total += len(raw)
                    if total > MAX_EXPORT_BYTES:
                        raise RuntimeError('Expense export exceeds 512 MiB; download one year at a time')
                    date = str(row.get('document_date') or row.get('received_at') or '')
                    month = date[:7] if re.fullmatch(r'\d{4}-\d{2}', date[:7]) else 'undated'
                    category = row.get('category') or 'uncategorized'
                    name = 'emails/' + month + '-' + _slug(category) + '-' + _slug(row['vendor']) + '-' + str(int(row['id'])) + '.eml'
                    archive.writestr(name, raw)
                    item.update(file=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), status='archived')
                except (LookupError, ValueError):
                    manifest['complete'] = False
                    item['status'] = 'missing_or_integrity_failed'
                manifest['originals'].append(item)
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
            archive.writestr('README.txt', ('Complete original-email archive.\n' if manifest['complete'] else 'INCOMPLETE ORIGINAL-EMAIL ARCHIVE: inspect manifest.json for missing or damaged originals. Background capture will retry missing sources.\n') + 'expenses.csv and ledger.json include review state, not only confirmed amounts. reviews.json records before-and-after review history for the exported entries. Original .eml files retain attachments. Keep this private accounting export secure.\n')
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise
