"""IMAP mailbox discovery with wire names preserved for later commands."""
import re


def quote_mailbox(mailbox):
    return '"' + mailbox.replace('\\', '\\\\').replace('"', '\\"') + '"'


def list_mailboxes(conn):
    typ, rows = conn.list('""', '"*"')
    if typ != "OK":
        raise RuntimeError("Mailbox discovery failed")
    mailboxes = []
    for row in rows or []:
        if row in (None, b''):
            continue
        literal = row[1] if isinstance(row, tuple) else None
        line = row[0] if isinstance(row, tuple) else row
        match = re.fullmatch(rb'\(([^)]*)\)\s+(?:NIL|"(?:[^"\\]|\\.)*")\s+(.+)', line)
        if not match:
            raise RuntimeError("Unrecognized IMAP LIST response")
        if b'\\noselect' in match[1].lower().split():
            continue
        value = match[2]
        if literal is not None:
            value = literal
        elif value.startswith(b'"') and value.endswith(b'"'):
            value = re.sub(rb'\\(.)', rb'\1', value[1:-1])
        elif value.startswith(b'"') or value.startswith(b'{'):
            raise RuntimeError("Invalid IMAP mailbox name")
        name = value.decode('ascii')  # Preserve IMAP's modified UTF-7 wire name.
        if name.upper() == 'INBOX':
            name = 'INBOX'
        if name not in {item[0] for item in mailboxes}:
            mailboxes.append((name, {flag.decode("ascii").lower() for flag in match[1].split()}))
    if not mailboxes:
        raise RuntimeError("No selectable mailboxes discovered")
    return sorted(mailboxes, key=lambda item: (item[0] != 'INBOX', item[0]))
