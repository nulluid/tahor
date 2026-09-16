from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retention_sweep


class RetentionSafetyTests(unittest.TestCase):
    def connection(self):
        conn = Mock()
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

    def test_only_targeted_read_unprotected_messages_deleted(self):
        conn = self.connection()
        self.assertEqual(retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-transient', 7, False), (1, 1))
        search = conn.uid.call_args_list[0].args
        for item in ('SEEN', 'retention-forever', 'retention-pending-review', 'needs-attention'):
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
