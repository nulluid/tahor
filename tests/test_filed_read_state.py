from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_unread_grace import Mailbox, message, FrozenDateTime
import filing_sweep
import mailbox_settings

DEST = 'Filed/Shopping/Shop'


class FolderMailbox(Mailbox):
    def __init__(self, folders):
        super().__init__({})
        self.folders = folders
        self.selected = None
        self.operations = []
        self.fail_move = False
        self.fail_seen = False
        self.special_flags = {}

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        self.rows = self.folders[self.selected]
        return super().select(mailbox, readonly)

    def list(self, reference='""', pattern='"*"'):
        names = self.folders if pattern == '"*"' else [pattern.strip('"')] if pattern.strip('"') in self.folders else []
        return 'OK', [f'({self.special_flags.get(name, "")}) "/" "{name}"'.encode() for name in names]

    def uid(self, command, *args):
        self.operations.append((self.selected, command, args))
        if command == 'MOVE':
            if self.fail_move:
                return 'NO', []
            destination = args[1].strip('"')
            self.folders[destination][str(int(args[0]) + 1000).encode()] = self.rows.pop(args[0])
            self.moved.append(args[0])
            return 'OK', []
        if command == 'STORE' and self.fail_seen:
            return 'NO', []
        return super().uid(command, *args)


class FiledReadStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(mailbox_settings, 'SETTINGS_PATH', Path(temp.name) / 'settings.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        mailbox_settings.set_inbox_grace_days(3, 7)

    def run_sweep(self, conn, dry_run=False):
        with patch.object(filing_sweep, 'connect', return_value=conn), patch.object(filing_sweep, 'datetime', FrozenDateTime), patch.object(filing_sweep.config, 'vendor_buckets', return_value={'example.com': ('Shopping', 'Shop')}), patch.object(sys, 'argv', ['filing_sweep.py'] + (['--dry-run'] if dry_run else [])):
            filing_sweep.main()

    def test_move_marks_destination_read_and_keeps_attention_review_starred_and_recent_inbox(self):
        conn = FolderMailbox({'INBOX': {b'1': message(10), b'2': message(10, False, 'needs-attention'), b'3': message(10, False, 'retention-pending-review'), b'4': message(2), b'5': {**message(10), 'flagged': True}}, DEST: {}})
        self.run_sweep(conn)
        self.assertEqual(conn.moved, [b'1'])
        self.assertTrue(conn.folders[DEST][b'1001']['read'])
        self.assertEqual(set(conn.folders['INBOX']), {b'2', b'3', b'4', b'5'})
        self.assertFalse(any(row['read'] for row in conn.folders['INBOX'].values()))
        stores = [op for op in conn.operations if op[1] == 'STORE']
        self.assertTrue(stores)
        self.assertTrue(all(op[0] != 'INBOX' for op in stores))

    def test_failed_move_keeps_message_unread_in_inbox(self):
        conn = FolderMailbox({'INBOX': {b'1': message(10)}, DEST: {}})
        conn.fail_move = True
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.run_sweep(conn)
        self.assertFalse(conn.folders['INBOX'][b'1']['read'])
        self.assertFalse(conn.folders[DEST])
        self.assertFalse(any(op[1] == 'STORE' for op in conn.operations))

    def test_backfill_runs_with_empty_inbox_and_preserves_ineligible_mail(self):
        folder = {b'1': message(10), b'2': message(2), b'3': message(10, False, 'needs-attention'), b'4': message(10, False, 'retention-pending-review'), b'5': {**message(10), 'flagged': True}, b'6': message(10, False, 'delete-pending'), b'7': {'date': message(10)['date'], 'read': False, 'tags': set()}, b'8': message(10, False, 'retention-forever')}
        conn = FolderMailbox({'INBOX': {}, 'Legacy/Receipts': folder, 'Drafts': {b'1': message(10)}, 'Localized draft folder': {b'1': message(10)}})
        conn.special_flags['Localized draft folder'] = '\\Drafts'
        self.run_sweep(conn)
        self.assertEqual({uid for uid, row in folder.items() if row['read']}, {b'1', b'8'})
        self.assertFalse(conn.folders['Drafts'][b'1']['read'])
        self.assertFalse(conn.folders['Localized draft folder'][b'1']['read'])
        stores = len([op for op in conn.operations if op[1] == 'STORE'])
        self.run_sweep(conn)
        self.assertEqual(len([op for op in conn.operations if op[1] == 'STORE']), stores)

    def test_read_flag_failure_is_reported_and_repaired_next_run(self):
        conn = FolderMailbox({'INBOX': {b'1': message(10)}, DEST: {}})
        conn.fail_seen = True
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.run_sweep(conn)
        self.assertFalse(conn.folders[DEST][b'1001']['read'])
        conn.fail_seen = False
        self.run_sweep(conn)
        self.assertTrue(conn.folders[DEST][b'1001']['read'])

    def test_preview_never_moves_or_marks_read(self):
        conn = FolderMailbox({'INBOX': {b'1': message(10)}, DEST: {b'2': message(10)}})
        self.run_sweep(conn, True)
        self.assertFalse(any(op[1] in ('MOVE', 'STORE') for op in conn.operations))
        self.assertFalse(conn.folders['INBOX'][b'1']['read'])
        self.assertFalse(conn.folders[DEST][b'2']['read'])
