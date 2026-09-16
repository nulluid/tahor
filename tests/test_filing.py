from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filing_sweep


class FilingTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(filing_sweep, 'list_mailboxes', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_move_fails_the_scheduled_command(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.capabilities = ('MOVE',)
        conn.list.return_value = ('OK', [b'folder'])
        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [b'1']
            if command == 'FETCH':
                return 'OK', [(b'1', b'From: billing@example.com\r\n')]
            return 'NO', []
        conn.uid.side_effect = uid
        with patch.object(filing_sweep, 'connect', return_value=conn), patch.object(filing_sweep.config, 'vendor_buckets', return_value={'example.com': ('Shopping', 'Shop')}), patch.object(sys, 'argv', ['filing_sweep.py']):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                filing_sweep.main()
        conn.logout.assert_called_once()

    def test_failed_search_cannot_report_an_empty_successful_sweep(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.uid.return_value = ('NO', [])
        with patch.object(filing_sweep, 'connect', return_value=conn), patch.object(filing_sweep.config, 'vendor_buckets', return_value={}), patch.object(sys, 'argv', ['filing_sweep.py']):
            with self.assertRaisesRegex(RuntimeError, 'search failed'):
                filing_sweep.main()
        self.assertFalse(any(call.args[0] == 'MOVE' for call in conn.uid.call_args_list))

    def test_full_domain_mapping_and_unmapped_country_domains(self):
        self.assertEqual(filing_sweep.vendor_for('Shop <billing@example.co.uk>', {}), ('_Unsorted', 'example.co.uk'))
        self.assertEqual(filing_sweep.vendor_for('Shop <billing@example.co.uk>', {'example.co.uk': ['Shopping', 'Shop']}), ['Shopping', 'Shop'])

    def test_failed_folder_creation_is_not_treated_as_success(self):
        conn = Mock()
        conn.list.return_value = ('OK', [None])
        conn.create.return_value = ('NO', [])
        created = set()
        with self.assertRaises(RuntimeError):
            filing_sweep.ensure_folder(conn, 'Filed/Shop', created)
        self.assertEqual(created, set())

    def test_successful_move_never_uses_global_expunge(self):
        conn = Mock()
        conn.capabilities = (b'MOVE', b'UIDPLUS')
        conn.select.return_value = ('OK', [])
        conn.list.return_value = ('OK', [b'folder'])
        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [b'1']
            if command == 'FETCH':
                return 'OK', [(b'1', b'From: Billing <billing@example.com>\r\n')]
            return 'OK', []
        conn.uid.side_effect = uid
        with patch.object(filing_sweep, 'connect', return_value=conn), patch.object(filing_sweep.config, 'vendor_buckets', return_value={'example.com': ('Shopping', 'Shop')}), patch.object(sys, 'argv', ['filing_sweep.py']):
            filing_sweep.main()
        commands = [call.args[0] for call in conn.uid.call_args_list]
        self.assertIn('MOVE', commands)
        self.assertNotIn('COPY', commands)
        self.assertGreater(commands.index('STORE'), commands.index('MOVE'))
        self.assertEqual(conn.uid.call_args_list[-1].args[-1], '(\\Seen)')
        conn.expunge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
