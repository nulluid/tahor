"""Exercise sweep outcomes against an in-memory IMAP search implementation."""
from datetime import datetime, timedelta, timezone
import os
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filing_sweep
import retention_sweep
import mailbox_settings

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Mailbox:
    capabilities = (b'UIDPLUS', b'MOVE')

    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.moved = []
        self.readonly = None

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return 'OK', []

    def logout(self):
        pass

    def list(self, *args):
        return 'OK', [b'() "/" "Filed/Shopping/Shop"']

    def uid(self, command, *args):
        if command == 'SEARCH':
            def matches(row):
                tokens = iter(args[1:])
                def criterion(token):
                    if token == 'OR':
                        left = criterion(next(tokens))
                        right = criterion(next(tokens))
                        return left or right
                    if token == 'SEEN':
                        return row['read']
                    if token == 'UNSEEN':
                        return not row['read']
                    if token == 'BEFORE':
                        cutoff = datetime.strptime(next(tokens), '%d-%b-%Y').date()
                        return row['date'].date() < cutoff
                    if token in ('KEYWORD', 'UNKEYWORD'):
                        present = next(tokens) in row['tags']
                        return present if token == 'KEYWORD' else not present
                    raise AssertionError('Unsupported search term: ' + token)
                results = [criterion(token) for token in tokens]
                return all(results)
            return 'OK', [b' '.join(uid for uid, row in self.rows.items() if matches(row))]
        if command == 'FETCH':
            return 'OK', [(b'1', b'From: billing@example.com\r\n')]
        if command == 'STORE':
            assert not self.readonly
            assert args[0] in self.rows
            assert args[1:] == ('+FLAGS', '(\\Deleted)')
            return 'OK', []
        if command in ('EXPUNGE', 'MOVE'):
            assert not self.readonly
            (self.deleted if command == 'EXPUNGE' else self.moved).append(args[0])
            return 'OK', []
        raise AssertionError('Unexpected IMAP command: ' + command)


def message(age, read=False, *tags):
    return {'date': NOW - timedelta(days=age), 'read': read,
            'tags': {'retention-transient', 'category-receipt', *tags}}


class UnreadGraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = patch.object(mailbox_settings, 'SETTINGS_PATH', Path(temp.name) / 'settings.json')
        settings.start()
        self.addCleanup(settings.stop)
        self.rows = {
            b'1': message(0),
            b'2': message(6.999),
            b'3': message(7),
            b'4': message(8),
            b'5': message(2, True),
            b'6': message(8, True),
            b'7': message(100, False, 'retention-forever'),
            b'8': message(100, False, 'retention-pending-review'),
            b'9': message(100, False, 'needs-attention'),
        }

    def retention(self, days, dry_run=False):
        conn = Mailbox(self.rows)
        with patch.object(retention_sweep, 'datetime', FrozenDateTime):
            result = retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', days, dry_run)
        return conn, result

    def test_default_retention_deletes_old_unread_and_read_but_keeps_recent_and_protected(self):
        conn, result = self.retention(7)
        self.assertEqual(conn.deleted, [b'4', b'6'])
        self.assertEqual(result, (2, 2))

    def test_filing_grace_never_delays_expired_unread_deletion(self):
        conn, result = self.retention(1)
        self.assertEqual(conn.deleted, [b'2', b'3', b'4', b'5', b'6'])
        self.assertEqual(result, (5, 5))

    def test_longer_retention_is_not_shortened_by_the_unread_grace(self):
        self.rows = {b'1': message(8), b'2': message(1096)}
        conn, result = self.retention(1095)
        self.assertEqual(conn.deleted, [b'2'])
        self.assertEqual(result, (1, 1))

    def test_retention_preview_counts_old_unread_without_deleting(self):
        conn, result = self.retention(7, True)
        self.assertEqual(result, (2, 0))
        self.assertEqual(conn.deleted, [])
        self.assertTrue(conn.readonly)

    def filing(self, dry_run=False, **env):
        conn = Mailbox({key: row for key, row in self.rows.items() if int(key) < 7})
        conn.rows[b'10'] = message(0, True)
        argv = ['filing_sweep.py'] + (['--dry-run'] if dry_run else [])
        with patch.dict(os.environ, env, clear=True), patch.object(filing_sweep, 'datetime', FrozenDateTime), patch.object(filing_sweep, 'connect', return_value=conn), patch.object(filing_sweep.config, 'vendor_buckets', return_value={'example.com': ('Shopping', 'Shop')}), patch.object(sys, 'argv', argv):
            filing_sweep.main()
        return conn

    def test_filing_defaults_keep_read_three_days_and_unread_seven(self):
        conn = self.filing()
        self.assertEqual(set(conn.moved), {b'4', b'6'})

    def test_configured_shorter_filing_grace_is_honored(self):
        conn = self.filing(FILING_UNREAD_MIN_AGE_DAYS='1')
        self.assertEqual(set(conn.moved), {b'2', b'3', b'4', b'6'})

    def test_saved_settings_override_legacy_environment_and_feed_filing(self):
        mailbox_settings.set_inbox_grace_days(0, 30)
        conn = self.filing(FILING_READ_MIN_AGE_DAYS='99', FILING_UNREAD_MIN_AGE_DAYS='0')
        self.assertEqual(set(conn.moved), {b'5', b'6', b'10'})

    def test_fresh_unread_trash_is_deleted_but_protected_mail_is_not(self):
        for row in self.rows.values():
            row['tags'].add('delete-pending')
        conn = Mailbox(self.rows)
        self.assertEqual(retention_sweep.sweep_trash(conn, 'INBOX'), (6, 6))
        self.assertEqual(conn.deleted, [b'1', b'2', b'3', b'4', b'5', b'6'])

    def test_filing_preview_leaves_every_message_in_place(self):
        conn = self.filing(True)
        self.assertEqual(conn.moved, [])
        self.assertTrue(conn.readonly)
