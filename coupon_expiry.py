"""Private sender opt-ins and source-grounded coupon expiry, independent of AI dates."""
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
import json
import os
from pathlib import Path
import re

from mailbox_paths import quote_mailbox
from mailbox_search import search_uids
from message_expiry import metadata, PROTECTED

KEYWORD = 'retention-coupon'
PREFIX = 'coupon-expiry-'
MONTHS = {name.lower(): number for number, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'), 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
DATE = r'(?:20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2}|[A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+20\d{2}|\d{1,2}\s+[A-Za-z]+\.?\s+20\d{2})'
ANCHOR = r'\b(?:expires?|expiration(?:\s+date)?|valid\s+(?:until|through)|offer\s+ends?|redeem\s+by)\s*(?:on\s*)?[:\-]?\s*'
EXPIRY = re.compile(ANCHOR + '(' + DATE + r')\b', re.I)


def policies():
    default = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent)) / 'coupon_policies.json'
    path = Path(os.environ.get('TAHOR_COUPON_POLICIES_PATH') or default)
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        if os.environ.get('TAHOR_COUPON_POLICIES_PATH'):
            raise
        return {}
    if len(raw) > 65536:
        raise ValueError('Coupon policy exceeds size limit')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid coupon policy')
    for sender, policy in value.items():
        if (not isinstance(sender, str) or sender != sender.lower() or not re.fullmatch(r'[a-z0-9_.+@-]+', sender)
                or '.' not in sender or not isinstance(policy, dict) or set(policy) - {'folder', 'date_order'}
                or policy.get('date_order') not in (None, 'mdy', 'dmy')):
            raise ValueError('Invalid coupon policy')
        folder = policy.get('folder')
        if (not isinstance(folder, str) or not folder or len(folder) > 240
                or any(ord(c) < 32 or c in '\\"*%' for c in folder)
                or any(part in ('', '.', '..') for part in folder.split('/'))):
            raise ValueError('Invalid coupon folder')
    return value


def policy_for(sender, configured=None):
    configured = policies() if configured is None else configured
    address = parseaddr(sender or '')[1].lower()
    return configured.get(address) or configured.get(address.rsplit('@', 1)[-1]) if '@' in address else None


def parse_date(value, order=None):
    if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', value):
        return date.fromisoformat(value)
    if '/' in value:
        first, second, year = map(int, value.split('/'))
        if order is None and first <= 12 and second <= 12 and first != second:
            raise ValueError('Ambiguous numeric date')
        month, day = (second, first) if order == 'dmy' or (order is None and first > 12) else (first, second)
        return date(year, month, day)
    parts = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', value.lower()).replace(',', '').replace('.', '').split()
    if parts[0].isdigit():
        day, month, year = int(parts[0]), MONTHS[parts[1]], int(parts[2])
    else:
        month, day, year = MONTHS[parts[0]], int(parts[1]), int(parts[2])
    return date(year, month, day)


def expiration(source, order=None):
    if not isinstance(source, str) or len(source) > 131072:
        return None
    # Any unresolved expiry phrase might describe another offer; never discard it.
    if len(list(re.finditer(ANCHOR, source, re.I))) != len(list(EXPIRY.finditer(source))):
        return None
    dates = set()
    for match in EXPIRY.finditer(source or ''):
        try:
            dates.add(parse_date(match[1], order))
        except (ValueError, KeyError):
            return None
    return next(iter(dates)) if len(dates) == 1 else None


def keywords(source, policy):
    until = expiration(source, policy.get('date_order'))
    return [KEYWORD] + ([PREFIX + until.strftime('%Y%m%d')] if until else [])


def expired(flags, now):
    if (KEYWORD.encode() not in flags or b'category-marketing' not in flags
            or flags.intersection(PROTECTED)
            or any(flag.startswith(b'category-') and flag != b'category-marketing' for flag in flags)):
        return False
    values = [flag for flag in flags if flag.startswith(PREFIX.encode())]
    if len(values) != 1 or not re.fullmatch(rb'coupon-expiry-20\d{6}', values[0]):
        return False
    try:
        until = datetime.strptime(values[0].decode()[len(PREFIX):], '%Y%m%d').date()
    except ValueError:
        return False
    # With no verified coupon timezone, wait until that date ended even in UTC-12.
    boundary = datetime.combine(until + timedelta(days=1), datetime.min.time(), timezone.utc) + timedelta(hours=12)
    return now.tzinfo is not None and now >= boundary


def sweep(conn, path, dry_run, delete_uids, now=None):
    if conn.select(quote_mailbox(path), readonly=dry_run)[0] != 'OK':
        raise RuntimeError('Coupon mailbox unavailable')
    status, rows = search_uids(conn, 'KEYWORD', KEYWORD, 'KEYWORD', 'category-marketing', 'UNFLAGGED')
    if status != 'OK':
        raise RuntimeError('Coupon search failed')
    now = now or datetime.now(timezone.utc)
    found = deleted = 0
    for uid in rows[0].split() if rows and rows[0] else []:
        status, rows = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE)')
        if status != 'OK':
            raise RuntimeError('Coupon metadata unavailable')
        _, flags, delivered = metadata(rows, uid)
        if not expired(flags, now):
            continue
        status, rows = conn.uid('FETCH', uid, '(UID FLAGS INTERNALDATE)')
        if status != 'OK':
            raise RuntimeError('Coupon current state unavailable')
        _, current, current_date = metadata(rows, uid)
        if current != flags or current_date != delivered or not expired(current, now):
            continue
        matched, removed = delete_uids(conn, [uid], dry_run)
        found += matched
        deleted += removed
    return found, deleted


def protect_result(result, sender, source, configured):
    """Opted-in marketing is retained without weakening review or forever flags."""
    policy = policy_for(sender, configured)
    if not policy or result.get('category') != 'marketing' or result.get('action') == 'error':
        return
    if result.get('action') == 'trash':
        result['action'] = 'keep'
    if result.get('retention') not in ('forever', 'pending-review'):
        result['retention'] = 'standard'
    result['coupon_keywords'] = keywords(source, policy)
