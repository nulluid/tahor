"""Search bounded UID intervals without increasing imaplib's response limits."""
import re

UID_WINDOW = 10000
MAX_UID = 2 ** 32 - 1


def _cached_number(conn, name):
    responses = getattr(conn, 'untagged_responses', {})
    rows = responses.get(name) if isinstance(responses, dict) else None
    if not rows:
        return None
    value = rows[-1]
    if not isinstance(value, bytes) or not value.isdigit():
        raise RuntimeError('Invalid mailbox search boundary')
    return int(value)


def _highest_uid(conn):
    # Read the SELECT cache without consuming UIDNEXT/EXISTS responses needed
    # by other callers. Empty mailboxes must not use IMAP's special * endpoint.
    if _cached_number(conn, 'EXISTS') == 0:
        return 0
    uidnext = _cached_number(conn, 'UIDNEXT')
    if uidnext is not None:
        if not 1 <= uidnext <= MAX_UID + 1:
            raise RuntimeError('Invalid mailbox UIDNEXT')
        return uidnext - 1
    # UIDNEXT is recommended, but not mandatory. This FETCH returns at most one
    # message and no content; it cannot overflow on a large mailbox.
    status, rows = conn.uid('FETCH', '*', '(UID)')
    if status != 'OK':
        raise RuntimeError('Could not determine mailbox UID boundary')
    values = []
    for row in rows or []:
        if row is None:
            continue
        if isinstance(row, tuple):
            row = row[0]
        if not isinstance(row, bytes):
            raise RuntimeError('Invalid mailbox UID boundary response')
        match = re.fullmatch(rb'\d+ \(UID (\d+)\)', row)
        if not match or not 1 <= int(match[1]) <= MAX_UID:
            raise RuntimeError('Invalid mailbox UID boundary response')
        values.append(int(match[1]))
    if len(values) > 1:
        raise RuntimeError('Ambiguous mailbox UID boundary')
    return values[0] if values else 0


def search_uids(conn, *criteria):
    """Return an imaplib-style SEARCH result after every interval succeeds.

    Preserve the caller's search keys, ANDed with each UID interval. Each reply
    contains at most 10,000 ten-digit UIDs (under 111 KB). Concurrent expunges
    cannot shift these UID boundaries. New arrivals beyond the SELECT UIDNEXT
    snapshot are deliberately picked up on the next mailbox visit.
    """
    highest = _highest_uid(conn)
    found = set()
    # Small sparse folders need one request even with large historical UIDs.
    # The frozen upper UID excludes arrivals after the SELECT snapshot.
    count = _cached_number(conn, 'EXISTS')
    step = max(1, highest) if count is not None and count <= UID_WINDOW else UID_WINDOW
    for first in range(1, highest + 1, step):
        last = min(highest, first + step - 1)
        status, rows = conn.uid('SEARCH', None, 'UID', '{}:{}'.format(first, last), *criteria)
        if status != 'OK':
            raise RuntimeError('Mailbox UID search failed; partial results discarded')
        received = 0
        for row in rows or []:
            if row is None:
                continue
            if not isinstance(row, bytes):
                raise RuntimeError('Invalid mailbox UID search response')
            tokens = row.split()
            received += len(tokens)
            if received > UID_WINDOW:
                raise RuntimeError('Mailbox UID search exceeded its requested range')
            for value in tokens:
                if not value.isdigit() or not first <= int(value) <= last:
                    raise RuntimeError('Mailbox UID search returned an out-of-range value')
                found.add(int(value))
    return 'OK', [b' '.join(str(value).encode('ascii') for value in sorted(found))]
