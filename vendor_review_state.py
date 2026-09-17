"""Reflect confirmed per-message decisions in their originating vendor samples."""
from datetime import datetime, timezone
import json


def sample_review(db, sample):
    matches = []
    for row in db.execute("SELECT * FROM decisions WHERE kind='message_review'"):
        try:
            identity = json.loads(row['context'] or '{}')
        except (TypeError, ValueError):
            continue
        if isinstance(identity, dict) and identity.get('message_id') == sample.get('message_id'):
            matches.append((row, identity))
    exact = [item for item in matches if item[1].get('mailbox') == sample.get('mailbox')
             and not any(sample.get(k) and item[1].get(k) and str(sample[k]) != str(item[1][k])
                         for k in ('uid', 'uidvalidity'))]
    if len(exact) == 1:
        return exact[0]
    moved = [item for item in matches if item[1].get('mailbox') != sample.get('mailbox') and isinstance(item[1].get('vendor_source_identity'), dict)
             and item[1]['vendor_source_identity'] == {key: sample.get(key) for key in ('mailbox', 'message_id', 'uid', 'uidvalidity')}]
    return moved[0] if len(moved) == 1 else (None, {})


def completed_action(db, sample):
    review, identity = sample_review(db, sample)
    if review is None or identity.get('applied') is not True:
        return None
    try:
        action = json.loads(review['resolution'] or '{}').get('action')
    except (TypeError, ValueError, AttributeError):
        return None
    return action if action in ('keep', 'keep_brief', 'trash') else None


def reconcile(db, row, context):
    samples = context.get('samples', [])
    actual = [sample for sample in samples if isinstance(sample, dict) and sample.get('mailbox') and sample.get('message_id')] if isinstance(samples, list) else []
    if not actual or not all(completed_action(db, sample) in ('keep_brief', 'trash') for sample in actual):
        return False
    if row['status'] == 'pending' and row['resolution'] is None:
        updated = dict(context, samples_handled=True, applied=True, outcome='Sample messages handled individually; no sender-wide rule added')
        with db:
            db.execute("UPDATE decisions SET status='resolved',context=?,resolution=?,resolved_at=? WHERE id=? AND context=? AND status='pending' AND resolution IS NULL",
                       (json.dumps(updated), json.dumps({'action': 'skip', 'reason': 'samples_handled'}), datetime.now(timezone.utc).isoformat(), row['id'], row['context']))
    return True
