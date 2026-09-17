import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import filing_sweep as filing


class UnsortedRefileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cursors.json'
        self.conn = Mock()
        self.conn.select.return_value = ('OK', [])
        self.conn.capabilities = (b'IMAP4rev1', b'MOVE')
        self.header = b'1 (UID 9 FLAGS (category-receipt retention-forever) INTERNALDATE "01-Jan-2026 00:00:00 +0000")'
        self.body = b'From: Receipts <billing@example.com>\r\nMessage-ID: <sample@example.com>\r\n\r\n'

    def run_refile(self, changed=False, dry=False, mapped=True):
        self.conn.uid.side_effect = [('OK', [(self.header, self.body)]), ('OK', [self.header.replace(b'FLAGS (', b'FLAGS (needs-attention ') if changed else self.header]), ('OK', []), ('OK', [])]
        with patch.object(filing, 'list_mailboxes', return_value=[('Filed/_Unsorted/example.com', set())]), patch.object(filing, 'eligible_uids', return_value={b'9'}), patch.object(filing, 'ensure_folder'), patch.object(filing.mailbox_settings, 'get_inbox_grace_days', return_value={'read':3, 'unread':7}), patch.object(filing.tahor_db, 'relocate_vendor_samples') as relocate:
            result = filing.refile_unsorted(self.conn, {'billing@example.com': ('Shopping', 'Example')} if mapped else {}, 'Filed', dry, self.path)
        return result, relocate

    def test_known_mapping_refiles_old_receipt_without_losing_forever(self):
        result, relocate = self.run_refile()
        self.assertEqual(result, 1)
        self.assertEqual(self.conn.uid.call_args_list[-1].args, ('MOVE', b'9', '"Filed/Shopping/Example"'))
        self.assertEqual(self.conn.uid.call_args_list[-2].args, ('STORE', b'9', '+FLAGS.SILENT', '(\\Seen)'))
        relocate.assert_called_once_with('Filed/_Unsorted/example.com', 'Filed/Shopping/Example', ['<sample@example.com>'])
        self.assertEqual(json.loads(self.path.read_text())['uids']['Filed/_Unsorted/example.com'], 9)

    def test_attention_race_or_unknown_mapping_never_moves(self):
        result, relocate = self.run_refile(changed=True)
        self.assertEqual(result, 0)
        self.assertTrue(all(call.args[0] == 'FETCH' for call in self.conn.uid.call_args_list))
        relocate.assert_not_called()
        self.conn.reset_mock()
        result, _ = self.run_refile(mapped=False)
        self.assertEqual(result, 0)
        self.assertEqual(self.conn.uid.call_count, 1)

    def test_dry_run_preserves_mail_and_cursor(self):
        result, relocate = self.run_refile(dry=True)
        self.assertEqual(result, 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.conn.uid.call_count, 1)
        relocate.assert_not_called()

    def test_folder_rotation_is_bounded_and_reaches_later_folders(self):
        folders = [(f'Filed/_Unsorted/vendor{i}', set()) for i in range(8)]
        with patch.object(filing, 'list_mailboxes', return_value=folders), patch.object(filing, 'eligible_uids', return_value=set()), patch.object(filing.mailbox_settings, 'get_inbox_grace_days', return_value={'read':3, 'unread':7}):
            filing.refile_unsorted(self.conn, {}, 'Filed', state_path=self.path)
            self.assertEqual(self.conn.select.call_count, 3)
            self.assertEqual(json.loads(self.path.read_text())['folder'], folders[2][0])
            self.conn.reset_mock()
            filing.refile_unsorted(self.conn, {}, 'Filed', state_path=self.path)
            self.assertEqual(self.conn.select.call_count, 3)
            self.assertEqual(json.loads(self.path.read_text())['folder'], folders[5][0])
