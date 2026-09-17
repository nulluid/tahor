"""Exact delivery-age and current-read-state checks for brief mailbox notices."""
from datetime import datetime, timedelta
import re

PROTECTED = {b'\\flagged', b'retention-forever', b'retention-pending-review',
             b'needs-attention', b'reply-protected', b'\\draft'}


def metadata(rows, uid, content=False):
    matches = [row for row in (rows or []) if isinstance(row, tuple)] if content else [
        row for row in (rows or []) if isinstance(row, bytes)]
    if len(matches) != 1:
        raise RuntimeError('Message expiry identity could not be verified')
    header = matches[0][0] if content else matches[0]
    actual = re.search(rb'\bUID (\d+)\b', header)
    flags = re.search(rb'\bFLAGS \(([^)]*)\)', header)
    date = re.search(rb'\bINTERNALDATE "([^"]+)"', header)
    if not actual or actual[1] != uid or not flags or not date:
        raise RuntimeError('Message expiry metadata could not be verified')
    delivered = datetime.strptime(date[1].decode('ascii'), '%d-%b-%Y %H:%M:%S %z')
    return header, set(flags[1].lower().split()), delivered


def expired(flags, delivered, now, grace):
    days = grace['read' if b'\\seen' in flags else 'unread']
    return not flags.intersection(PROTECTED) and now >= delivered + timedelta(days=days)
