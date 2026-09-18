#!/usr/bin/env python3
"""
File already-classified mail out of the inbox once it's had a fair chance
to be seen: read and older than FILING_READ_MIN_AGE_DAYS, or still unread
past FILING_UNREAD_MIN_AGE_DAYS. Filing this on arrival would mean it's
never seen at all. The configurable defaults are three days for read mail
and seven days for unread mail.

Usage:
  python3 filing_sweep.py [--dry-run]

Vendor routing comes from vendor_buckets.json (see vendor_buckets.example.json):
exact sender address or legacy domain label -> [bucket, display name]. An unmapped sender files
under "<root>/_Unsorted/<label>" instead of blocking, and is printed so the
table can grow.
"""
import email.utils
import email.policy
import imaplib
import json
import os
from pathlib import Path
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
from mailbox_search import search_uids
import tahor_db
import mailbox_settings
import reply_rules
import coupon_expiry
import business_filing
from mailbox_paths import list_mailboxes, quote_mailbox

CATEGORY_KEYWORDS = ['category-' + category for category in (
    'receipt', 'statement', 'government-tax', 'security-alert', 'shipping',
    'subscription', 'legal', 'medical', 'travel', 'financial-account', 'utility',
    'personal-correspondence',
)]
HEADER_FETCH = '(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])'
MOVE_GUARDS = {b'\\flagged', b'\\draft', b'\\deleted', b'needs-attention',
               b'retention-pending-review', b'delete-pending'}


def move_snapshot(conn, uid):
    from message_expiry import metadata
    status, rows = conn.uid('FETCH', uid, HEADER_FETCH)
    if status != 'OK':
        raise RuntimeError('Could not verify filing source')
    _, flags, delivered = metadata(rows, uid, content=True)
    body = next(row[1] for row in rows if isinstance(row, tuple))
    return flags, delivered, body


def move_allowed(snapshot, grace, reply_rule=None):
    flags, delivered, _ = snapshot
    if flags & MOVE_GUARDS:
        return False
    if reply_rule is None and (b'reply-protected' in flags or any(flag.startswith(b'reply-rule-') for flag in flags)):
        return False
    if reply_rule is not None:
        if not {reply_rules.keyword(reply_rule).encode(), reply_rules.scan_keyword(reply_rule).encode()} <= flags:
            return False
    days = grace['read' if b'\\seen' in flags else 'unread']
    return datetime.now(timezone.utc) >= delivered + timedelta(days=days)



def connect():
    conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=60)
    conn.login(config.email_address(), config.app_password())
    return conn


def vendor_for(from_header, buckets):
    _, addr = email.utils.parseaddr(from_header or "")
    addr = addr.strip().lower()
    if addr in buckets:
        return buckets[addr]
    domain = re.sub(r"^www\.", "", addr.split("@")[-1].lower() if "@" in addr else "")
    parts = domain.split(".") if domain else []
    # The registrable label is the second-to-last segment, not the leftmost
    # one, or every subdomain (notification.example.com) becomes its own vendor.
    label = (parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")).lower()
    if domain in buckets:
        return buckets[domain]
    if label in buckets:
        return buckets[label]
    return ("_Unsorted", domain if domain else "Unknown")


def filing_destination(root, bucket, vendor, flags):
    return f'{root}/{bucket}/{vendor}/' + ('Receipts' if b'category-receipt' in flags else 'Correspondence')


def ensure_folder(conn, path, created):
    if path in created:
        return
    # LIST rather than SELECT to probe existence — a failed SELECT drops the
    # session out of the selected state, breaking whatever comes after it.
    typ, data = conn.list('""', quote_mailbox(path))
    if typ != "OK" or not data or not data[0]:
        typ, _ = conn.create(quote_mailbox(path))
        if typ != "OK":
            raise RuntimeError(f"Could not create filing destination {path!r}")
    created.add(path)



PROTECTED = ("UNDRAFT", "UNDELETED", "UNFLAGGED", "UNKEYWORD", "needs-attention", "UNKEYWORD", "retention-pending-review", "UNKEYWORD", "delete-pending")
CLASSIFIED = ("OR", "KEYWORD", "retention-standard", "OR", "KEYWORD", "retention-transient", "KEYWORD", "retention-forever")
SPECIAL_FOLDERS = {"inbox", "drafts", "sent", "trash", "spam", "junk", "scheduled", "snoozed"}
SPECIAL_FLAGS = {"\\drafts", "\\sent", "\\trash", "\\junk", "\\all"}


def eligible_uids(conn, criteria):
    candidates = set()
    reply_guards = tuple(value for rule in reply_rules.get_rules() for value in ('UNKEYWORD', reply_rules.keyword(rule)))
    for keyword in CATEGORY_KEYWORDS + [coupon_expiry.KEYWORD]:
        coupon_guards = ('KEYWORD', 'category-marketing') if keyword == coupon_expiry.KEYWORD else ()
        typ, data = search_uids(conn, *criteria, "KEYWORD", keyword, *coupon_guards, *reply_guards, "UNKEYWORD", "reply-protected", *PROTECTED, *CLASSIFIED)
        if typ != "OK":
            raise RuntimeError("Filing search failed; retry the sweep")
        if data and data[0]:
            candidates.update(data[0].split())
    return candidates


def mark_filed_read(conn, mailbox, dry_run=False):
    if mailbox.upper() == "INBOX":
        raise ValueError("Read-state reconciliation must not mark INBOX messages read")
    typ, _ = conn.select(quote_mailbox(mailbox), readonly=dry_run)
    if typ != "OK":
        raise RuntimeError("Could not select filed mailbox")
    days = mailbox_settings.get_inbox_grace_days()["unread"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    criteria = ("UNSEEN", "BEFORE", cutoff) if days else ("UNSEEN",)
    candidates = eligible_uids(conn, criteria)
    for rule in reply_rules.get_rules():
        if rule.get('filing_folder'):
            candidates.update(reply_filing_uids(conn, rule, criteria))
    candidates = sorted(candidates, key=int)
    if dry_run:
        return len(candidates)
    changed = 0
    for offset in range(0, len(candidates), 200):
        batch = candidates[offset:offset + 200]
        typ, _ = conn.uid("STORE", b','.join(batch).decode(), "+FLAGS.SILENT", "(\\Seen)")
        if typ != "OK":
            raise RuntimeError("Could not mark filed mail read; unread messages will be retried")
        changed += len(batch)
    return changed


def reconcile_filed_mail(conn, dry_run=False):
    total = failures = 0
    for name, flags in list_mailboxes(conn):
        if name.lower() in SPECIAL_FOLDERS or flags & SPECIAL_FLAGS:
            continue
        try:
            changed = mark_filed_read(conn, name, dry_run)
            total += changed
            if changed:
                print(f"{name}: {'would mark' if dry_run else 'marked'} {changed} filed message(s) read")
        except (RuntimeError, imaplib.IMAP4.error, OSError) as exc:
            failures += 1
            print(f"{name}: read-state cleanup failed: {exc}")
    print(f"Filed-mail read-state cleanup: {total} {'eligible' if dry_run else 'marked read'}, {failures} folder failure(s).")
    return total, failures


def reply_filing_uids(conn, rule, criteria):
    # A stable rule ID alone can describe a match under an obsolete owner policy.
    status, rows = search_uids(conn, *criteria, 'KEYWORD', reply_rules.keyword(rule),
                            'KEYWORD', reply_rules.scan_keyword(rule), *PROTECTED, *CLASSIFIED)
    if status != 'OK':
        raise RuntimeError('Reply-rule filing search failed')
    return set(rows[0].split() if rows and rows[0] else [])


def reply_filing_destinations(conn, read_criteria, unread_criteria):
    destinations = {}
    for rule in reply_rules.get_rules():
        target = rule.get('filing_folder', '')
        if not target:
            continue
        for criteria in (read_criteria, unread_criteria):
            for uid in reply_filing_uids(conn, rule, criteria):
                if uid in destinations and destinations[uid] != target:
                    raise RuntimeError('Reply rules disagree on a filing destination; messages preserved')
                destinations[uid] = target
    return destinations


def refile_unsorted(conn, buckets, root, dry_run=False, state_path=None):
    """Revisit three staging folders and at most 100 messages each per sweep."""
    from data_changes import atomic_write
    from message_expiry import metadata
    state_path = Path(state_path or Path(os.environ.get('TAHOR_STATE_DIR', Path(__file__).resolve().parent)) / 'refile_cursors.json')
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = {}
    vendor_roots = {f'{root}/{value[0]}/{value[1]}' for value in buckets.values() if isinstance(value, (list, tuple)) and len(value) == 2}
    paths = [name for name, flags in list_mailboxes(conn)
             if (name.startswith(root + '/_Unsorted/') or name in vendor_roots) and not flags.intersection(SPECIAL_FLAGS)]
    if not paths:
        return 0
    paths.sort()
    after = state.get('folder', '')
    ordered = [name for name in paths if name > after] + [name for name in paths if name <= after]
    moved = 0
    created = set()
    grace = mailbox_settings.get_inbox_grace_days()
    now = datetime.now(timezone.utc)
    capabilities = {value.decode().upper() if isinstance(value, bytes) else value.upper() for value in conn.capabilities}
    if not dry_run and 'MOVE' not in capabilities:
        raise RuntimeError('Refiling requires IMAP MOVE')
    for source in ordered[:3]:
        if conn.select(quote_mailbox(source), readonly=dry_run)[0] != 'OK':
            raise RuntimeError('Could not select unsorted mailbox')
        response = conn.response('UIDVALIDITY')
        validity = response[1][0] if isinstance(response, tuple) and len(response) == 2 and response[1] else None
        candidates = sorted(eligible_uids(conn, ('UNKEYWORD', 'reply-protected')), key=int)
        previous = int(state.get('uids', {}).get(source, 0))
        candidates = [uid for uid in candidates if int(uid) > previous] + [uid for uid in candidates if int(uid) <= previous]
        for uid in candidates[:100]:
            status, rows = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID SUBJECT DATE)])')
            if status != 'OK':
                raise RuntimeError('Could not fetch unsorted message')
            _, flags, delivered = metadata(rows, uid, content=True)
            blocked = {b'needs-attention', b'reply-protected', b'\\flagged', b'\\draft', b'retention-pending-review', b'delete-pending'}
            if not flags.intersection(blocked) and flags.intersection({key.encode() for key in CATEGORY_KEYWORDS}) and now >= delivered + timedelta(days=grace['read' if b'\\seen' in flags else 'unread']):
                body = next(row[1] for row in rows if isinstance(row, tuple))
                message = email.message_from_bytes(body, policy=email.policy.default)
                if flags.intersection({b'business-receipt', b'business-correspondence'}) or business_filing.match_message(
                        dict(id=str(message.get('Message-ID', '')), **{'from': str(message.get('From', ''))},
                             subject=str(message.get('Subject', '')), date=str(message.get('Date', ''))), delivered=delivered, flags=flags):
                    continue
                bucket, vendor = vendor_for(str(message.get('From', '')), buckets)
                if bucket != '_Unsorted':
                    target = filing_destination(root, bucket, vendor, flags)
                    if target != source:
                        if not dry_run:
                            ensure_folder(conn, target, created)
                            if conn.select(quote_mailbox(source))[0] != 'OK' or conn.response('UIDVALIDITY') != ('UIDVALIDITY', [validity]) or not isinstance(validity, bytes) or not validity.isdigit():
                                raise RuntimeError('Refiling mailbox generation changed')
                            current, current_date, current_body = move_snapshot(conn, uid)
                            if current != flags or current_date != delivered or current_body != body or not move_allowed((current,current_date,current_body), grace):
                                continue
                            if conn.uid('MOVE', uid, quote_mailbox(target))[0] != 'OK':
                                raise RuntimeError('Could not refile message')
                            tahor_db.relocate_vendor_samples(source, target, [str(message.get('Message-ID', ''))])
                            mark_filed_read(conn, target)
                            if conn.select(quote_mailbox(source))[0] != 'OK':
                                raise RuntimeError('Could not resume refiling source')
                        moved += 1
            state.setdefault('uids', {})[source] = int(uid)
        state['folder'] = source
        if not dry_run:
            atomic_write(state_path, json.dumps(state) + '\n')
    return moved


def main():
    dry_run = "--dry-run" in sys.argv
    buckets = config.vendor_buckets()
    root = config.filing_root()
    grace = mailbox_settings.get_inbox_grace_days()
    read_min_age, unread_min_age = grace["read"], grace["unread"]

    conn = connect()
    business_filing.run_sweep(conn, dry_run=dry_run)
    typ, _ = conn.select('"INBOX"', readonly=dry_run)
    if typ != "OK":
        sys.exit("Could not select INBOX.")
    response = conn.response('UIDVALIDITY')
    values = response[1] if isinstance(response, tuple) and len(response) == 2 else []
    uidvalidity = values[0].decode('ascii') if values and isinstance(values[0], bytes) and values[0].isdigit() else ''

    read_cutoff = (datetime.now(timezone.utc) - timedelta(days=read_min_age)).strftime("%d-%b-%Y")
    unread_cutoff = (datetime.now(timezone.utc) - timedelta(days=unread_min_age)).strftime("%d-%b-%Y")

    read_criteria = ("SEEN", "BEFORE", read_cutoff) if read_min_age > 0 else ("SEEN",)
    unread_criteria = ("UNSEEN", "BEFORE", unread_cutoff) if unread_min_age > 0 else ("UNSEEN",)
    try:
        candidates = eligible_uids(conn, read_criteria) | eligible_uids(conn, unread_criteria)
        reply_destinations = reply_filing_destinations(conn, read_criteria, unread_criteria)
        candidates |= set(reply_destinations)
    except Exception:
        conn.logout()
        raise

    by_dest = defaultdict(list)
    snapshots = {}
    grace = {'read': read_min_age, 'unread': unread_min_age}
    active_filing_rules = [rule for rule in reply_rules.get_rules() if rule.get('filing_folder')]
    reply_move_rules = {}
    queued_message_ids = {}
    unsorted_labels = set()
    failures = 0
    for uid in candidates:
        snapshot = move_snapshot(conn, uid)
        rule = next((r for r in active_filing_rules if r.get('filing_folder') == reply_destinations.get(uid) and {reply_rules.keyword(r).encode(), reply_rules.scan_keyword(r).encode()} <= snapshot[0]), None) if uid in reply_destinations else None
        if uid in reply_destinations and rule is None:
            continue
        reply_move_rules[uid] = rule
        if not move_allowed(snapshot, grace, rule):
            continue
        snapshots[uid] = snapshot
        flags, delivered, raw_headers = snapshot
        if uid in reply_destinations:
            by_dest[reply_destinations[uid]].append(uid)
            continue
        message = email.message_from_bytes(raw_headers, policy=email.policy.default)
        from_header = message.get('From', '')
        if coupon_expiry.KEYWORD.encode() in flags and b'category-marketing' in flags:
            policy = coupon_expiry.policy_for(str(from_header))
            if any(flag.startswith(b'category-') and flag != b'category-marketing' for flag in flags):
                continue
            if policy is None or flags.intersection({b'needs-attention', b'reply-protected', b'\\flagged', b'retention-pending-review'}):
                continue
            by_dest[f"{root}/{policy['folder']}"].append(uid)
            continue
        business_route = business_filing.match_message(dict(id=str(message.get('Message-ID', '')), **{'from': str(from_header)}, subject=str(message.get('Subject', '')), date=str(message.get('Date', ''))), delivered=delivered, flags=flags)
        if business_route:
            # The resumable business sweep alone owns these moves and revalidates
            # UIDVALIDITY, exact message identity, and attention state first.
            continue
        bucket, vendor = vendor_for(from_header, buckets)
        if bucket == "_Unsorted":
            unsorted_labels.add(vendor)
            if not dry_run and vendor != "Unknown":
                display_name, sender_email = email.utils.parseaddr(from_header)
                queued_message_ids[uid] = str(message.get('Message-ID', ''))
                tahor_db.queue_vendor_mapping(vendor, metadata={
                    'sender_email': sender_email.strip().lower(),
                    'display_name': display_name, 'subject': str(message.get('Subject', '')),
                    'suggested_vendor': display_name,
                    'mailbox': 'INBOX', 'message_id': str(message.get('Message-ID', '')),
                    'uid': uid.decode('ascii'), 'uidvalidity': uidvalidity,
                    'date': str(message.get('Date', '')),
                    'received_at': delivered.isoformat(),
                })
        by_dest[filing_destination(root, bucket, vendor, flags)].append(uid)

    capabilities = {c.decode().upper() if isinstance(c, bytes) else c.upper() for c in conn.capabilities}
    if candidates and not dry_run and "MOVE" not in capabilities:
        raise RuntimeError("Filing requires IMAP MOVE to avoid copying or deleting unrelated mail")
    created, total_moved = set(), 0
    for dest, uids in sorted(by_dest.items()):
        verb = "would move" if dry_run else "moving"
        print(f"{dest}: {verb} {len(uids)} message(s)")
        if dry_run:
            continue
        typ, _ = conn.select('"INBOX"')
        if typ != "OK":
            raise RuntimeError("Could not reselect INBOX")
        ensure_folder(conn, dest, created)
        moved_here = 0
        for uid in uids:
            if conn.select('"INBOX"')[0] != 'OK':
                raise RuntimeError('Could not reselect filing source')
            response = conn.response('UIDVALIDITY')
            values = response[1] if isinstance(response, tuple) and len(response) == 2 else []
            current_validity = values[0].decode('ascii') if values and isinstance(values[0], bytes) and values[0].isdigit() else ''
            if not uidvalidity or current_validity != uidvalidity:
                raise RuntimeError('Filing mailbox generation changed; no stale UID was moved')
            current = move_snapshot(conn, uid)
            rule = reply_move_rules.get(uid)
            if current != snapshots[uid] or not move_allowed(current, grace, rule):
                failures += 1
                continue
            typ, _ = conn.uid("MOVE", uid, quote_mailbox(dest))
            if typ == "OK":
                total_moved += 1
                moved_here += 1
                if queued_message_ids.get(uid):
                    tahor_db.relocate_vendor_samples('INBOX', dest, [queued_message_ids[uid]])
            else:
                failures += 1
                print(f"  FAILED to move uid {uid.decode()} to {dest}")
        if moved_here:
            try:
                mark_filed_read(conn, dest)
            except (RuntimeError, imaplib.IMAP4.error, OSError) as exc:
                failures += 1
                print(f"{dest}: moved mail needs a read-state retry: {exc}")
    try:
        refiled = refile_unsorted(conn, buckets, root, dry_run)
        if refiled:
            print(f'Refiled vendor mail: {refiled} message(s).')
        _, read_failures = reconcile_filed_mail(conn, dry_run)
        failures += read_failures
    finally:
        conn.logout()

    if unsorted_labels:
        print(f"\n{len(unsorted_labels)} unmapped sender(s), add to vendor_buckets.json: {sorted(unsorted_labels)}")
    total = sum(len(v) for v in by_dest.values())
    if dry_run:
        print(f"\nDRY RUN. {total} messages would be filed across {len(by_dest)} folder(s).")
    else:
        print(f"\nDone. {total_moved} messages filed.")
    if failures:
        raise RuntimeError(f"Filing incomplete: {failures} message operation(s) failed; retry the sweep")


if __name__ == "__main__":
    main()
