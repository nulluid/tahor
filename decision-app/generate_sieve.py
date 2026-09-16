#!/usr/bin/env python3
"""Maintain a reviewable Sieve block section without replacing custom rules."""
import os
import fcntl
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tahor_db
from data_changes import atomic_write, commit_data

DATA_DIR = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent.parent))
BEGIN = '# BEGIN TAHOR SENDER RULES'
END = '# END TAHOR SENDER RULES'


def domain_test(domain):
    labels = domain.split('.')
    if len(labels) < 2 or len(domain) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
        raise ValueError('Invalid sender domain')
    return f'address :domain :is "from" "{domain}"'


def build_sieve(block_all, block_marketing):
    parts = [BEGIN]
    for domain in block_all | block_marketing:
        domain_test(domain)
    if block_all:
        tests = ",\n    ".join(domain_test(domain) for domain in sorted(block_all))
        parts.append(f'if anyof (\n    {tests}\n) {{\n    discard;\n    stop;\n}}')
    if block_marketing - block_all:
        parts.append("# Marketing-only rules stay in the classifier so receipts are preserved.\n"
                     + "\n".join("# " + domain for domain in sorted(block_marketing - block_all)))
    parts.append(END)
    return "\n\n".join(parts) + "\n"


def merge_sieve(existing, generated):
    if BEGIN in existing or END in existing:
        if existing.count(BEGIN) != 1 or existing.count(END) != 1 or existing.index(END) < existing.index(BEGIN):
            raise ValueError('Sieve managed section is malformed; existing script preserved')
        start = existing.index(BEGIN)
        end = existing.index(END) + len(END)
        return existing[:start] + generated.rstrip('\n') + existing[end:]
    # Keep require statements before executable rules, and block before custom stop/keep rules.
    preamble = re.match(r'\A(?:\s+|#[^\n]*(?:\n|$)|/\*.*?\*/|require\s+(?:\[[^\]]*\]|"[^"]*")\s*;)*', existing, re.S)
    index = preamble.end()
    return existing[:index] + '\n' + generated + '\n' + existing[index:]


def refresh_sieve():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / ".sieve.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _refresh_sieve()


def _refresh_sieve():
    conn = tahor_db.get_db()
    try:
        rows = conn.execute('SELECT sender_domain, rule FROM sender_rules').fetchall()
    finally:
        conn.close()
    block_all = {r['sender_domain'] for r in rows if r['rule'] == 'block_all'}
    marketing = {r['sender_domain'] for r in rows if r['rule'] == 'block_marketing'}
    path = DATA_DIR / 'sieve.txt'
    existing = path.read_text() if path.exists() else ''
    generated = merge_sieve(existing, build_sieve(block_all, marketing))
    changed = generated != existing
    if changed:
        atomic_write(path, generated)
    if changed:
        conn = tahor_db.get_db()
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                conn.execute("UPDATE decisions SET status='resolved', resolved_at=? WHERE kind='sieve_update' AND status='pending'", (now,))
                conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES ('sieve_update', 'Sieve filter update recommended', ?, 'pending', ?)", (f'{len(block_all)} provider-side domain block(s). Marketing-only blocks stay in the classifier. Review and install this script in your mail provider.', now))
        finally:
            conn.close()
    commit_data(DATA_DIR, 'update sender blocking rules', ['sieve.txt'])
    return changed


def main():
    print('Sieve proposal updated.' if refresh_sieve() else 'Sieve proposal is current.')


if __name__ == '__main__':
    main()
