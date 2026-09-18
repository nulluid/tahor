"""Bounded background receipt archiving, evidence extraction and category advice."""
import email
import json
import time

PARSER_VERSION = 1


def refresh_archived(limit=100):
    import business_ledger as ledger
    import expense_archive
    import fetch_batch
    db = ledger._db()
    refreshed = removed = processed = 0
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS expense_parser_versions(entry_id INTEGER PRIMARY KEY,version INTEGER NOT NULL)')
        for item in ledger.list_entries():
            if processed >= max(0, min(limit, 1000)): break
            old = db.execute('SELECT version FROM expense_parser_versions WHERE entry_id=?', (item['id'],)).fetchone()
            if old and old['version'] >= PARSER_VERSION:
                continue
            try:
                raw = expense_archive.read_original(item['id'])
            except (LookupError, ValueError):
                continue
            processed += 1
            message = email.message_from_bytes(raw)
            subject = fetch_batch.decode_str(message.get('Subject',''))
            text = fetch_batch.extract_body_text(raw)
            refreshed += bool(ledger.reprocess_entry(item['id'], text, subject=subject, source_date=item['received_at']))
            if ledger.is_payment_reminder(subject, text):
                removed += bool(ledger.exclude_non_receipt(item['id']))
            with db:
                db.execute('INSERT INTO expense_parser_versions VALUES (?,?) ON CONFLICT(entry_id) DO UPDATE SET version=excluded.version', (item['id'],PARSER_VERSION))
    finally:
        db.close()
    return {'refreshed':refreshed, 'non_receipts_removed':removed}


def run():
    import expense_archive, expense_categories
    result = expense_archive.capture_missing(limit=5,budget_seconds=45)
    result.update(refresh_archived())
    result['categories_suggested'] = expense_categories.suggest_pending(limit=1)
    return result


def main():
    print(json.dumps(run()), flush=True)


if __name__ == '__main__':
    main()
