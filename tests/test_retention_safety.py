from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retention_sweep


class RetentionSafetyTests(unittest.TestCase):
    def test_search_failure_is_not_reported_as_no_eligible_mail(self):
        conn = self.connection()
        conn.uid.side_effect = [('NO', [])]
        with self.assertRaisesRegex(RuntimeError, 'search failed'):
            retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', 7, False)

    def test_partial_deletion_fails_the_scheduled_command(self):
        conn = self.connection()
        with patch.object(retention_sweep, 'connect', return_value=conn), patch.object(retention_sweep, 'list_all_paths', return_value=['INBOX']), patch.object(retention_sweep, 'sweep_mailbox', return_value=(2, 1)), patch.object(sys, 'argv', ['retention_sweep.py']):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                retention_sweep.main()
        conn.logout.assert_called_once()

    def connection(self):
        conn = Mock()
        conn.untagged_responses = {'UIDNEXT': [b'10000'], 'EXISTS': [b'100']}
        conn.capabilities = (b'IMAP4rev1', b'UIDPLUS')
        conn.select.return_value = ('OK', [])
        conn.uid.side_effect = [('OK', [b'42']), ('OK', []), ('OK', [])]
        return conn

    def test_dry_run_cannot_expunge_existing_deleted_mail(self):
        conn = self.connection()
        self.assertEqual(retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', 7, True), (1, 0))
        conn.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertEqual(conn.uid.call_count, 1)
        conn.close.assert_not_called()
        conn.expunge.assert_not_called()

    def test_only_targeted_eligible_unprotected_messages_deleted(self):
        conn = self.connection()
        self.assertEqual(retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', 7, False), (1, 1))
        search = conn.uid.call_args_list[0].args
        for item in ('retention-forever', 'retention-pending-review', 'needs-attention'):
            self.assertIn(item, search)
        self.assertEqual(conn.uid.call_args_list[-1].args, ('EXPUNGE', b'42'))
        conn.expunge.assert_not_called()
        conn.close.assert_not_called()

    def test_no_targeted_expunge_support_means_no_deletion(self):
        conn = self.connection()
        conn.capabilities = (b'IMAP4rev1',)
        with self.assertRaises(RuntimeError):
            retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', 7, False)
        self.assertEqual(conn.uid.call_count, 1)


if __name__ == '__main__':
    unittest.main()
