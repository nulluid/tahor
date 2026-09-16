import email
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import draft_replies


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = patch.object(draft_replies.tahor_db, 'DB_PATH', self.root / 'db.sqlite')
        self.db.start()
        self.addCleanup(self.db.stop)
        draft_replies.tahor_db.init_db()
        self.config = patch.object(draft_replies.config, 'email_address', return_value='owner@example.com')
        self.config.start()
        self.addCleanup(self.config.stop)
        self.triggers = patch.object(draft_replies.mailbox_settings, 'get_reply_triggers', return_value=[{'type': 'sender_email', 'value': 'person@example.com'}])
        self.triggers.start()
        self.addCleanup(self.triggers.stop)
        self.matches = patch.object(draft_replies.mailbox_settings, 'matches_reply_trigger', return_value=True)
        self.matches.start()
        self.addCleanup(self.matches.stop)
        self.conn = Mock()
        self.raw = b'From: person@example.com\r\nTo: owner@example.com\r\nMessage-ID: <one@example.com>\r\nSubject: A question\r\n\r\nCan we meet?'
        self.conn.uid.side_effect = self.command

    def command(self, command, *args):
        if command == 'SEARCH':
            return 'OK', [b'42']
        if command == 'FETCH':
            return 'OK', [(b'42', self.raw)]
        return 'OK', []

    def test_append_failure_keeps_original_retryable_and_reuses_generated_body(self):
        with patch.object(draft_replies, 'draft_exists', return_value=False), patch.object(draft_replies, 'draft_reply_body', return_value='Thursday works.') as generate:
            self.conn.append.return_value = ('NO', [])
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            row = draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')
            self.assertEqual(row['status'], 'preparing')
            self.assertFalse(any(call.args[0] == 'STORE' for call in self.conn.uid.call_args_list))
            self.conn.append.return_value = ('OK', [])
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_called_once()
        self.assertEqual(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')['status'], 'pending')
        with patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            generate.assert_not_called()
        raw = self.conn.append.call_args.args[-1]
        message = email.message_from_bytes(raw)
        self.assertEqual(message['In-Reply-To'], '<one@example.com>')
        self.assertIn('tahor-draft-', message['Message-ID'])

    def test_lost_append_response_does_not_create_second_draft(self):
        draft_replies.tahor_db.prepare_reply_draft('<one@example.com>', '<one@example.com>', 'person@example.com', 'A question', 'Thursday works.', 'test')
        with patch.object(draft_replies, 'draft_exists', return_value=True), patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_not_called()
            self.conn.append.assert_not_called()

    def test_no_reply_needed_does_not_append(self):
        with patch.object(draft_replies, 'draft_reply_body', return_value='NO_REPLY_NEEDED'):
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()
        self.assertTrue(any(call.args[0] == 'STORE' for call in self.conn.uid.call_args_list))

    def test_idle_drains_multiple_untagged_events(self):
        conn = Mock()
        conn._new_tag.return_value = b'A1'
        conn.tagged_commands = {b'A1': None}
        conn.sock.pending.return_value = 0
        events = iter([None, b'* 2 EXISTS', b'* 1 EXPUNGE', b'A1 OK done'])
        def response():
            event = next(events)
            if event == b'A1 OK done':
                conn.tagged_commands[b'A1'] = ('OK', [])
            return event
        conn._get_response.side_effect = response
        with patch.object(draft_replies.select, 'select', return_value=([conn.sock], [], [])):
            self.assertTrue(draft_replies.wait_for_new_mail(conn))
        self.assertEqual(conn._get_response.call_count, 4)
        self.assertEqual(conn.tagged_commands, {})


if __name__ == '__main__':
    unittest.main()
