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
        self.conn.response.return_value = ('UIDVALIDITY', [b'42'])
        self.conn.capabilities = (b'IMAP4rev1', b'MOVE')
        self.header = b'1 (UID 9 FLAGS (category-receipt retention-forever) INTERNALDATE "01-Jan-2026 00:00:00 +0000")'
        self.body = b'From: Receipts <billing@example.com>\r\nMessage-ID: <sample@example.com>\r\n\r\n'

    def run_refile(self, changed=False, dry=False, mapped=True):
        self.conn.uid.side_effect = [('OK', [(self.header, self.body)]), ('OK', [(self.header.replace(b'FLAGS (', b'FLAGS (needs-attention ') if changed else self.header,self.body)]), ('OK', []), ('OK', [])]
        with patch.object(filing, 'list_mailboxes', return_value=[('Filed/_Unsorted/example.com', set())]), patch.object(filing, 'eligible_uids', return_value={b'9'}), patch.object(filing, 'ensure_folder'), patch.object(filing, 'mark_filed_read'), patch.object(filing.mailbox_settings, 'get_inbox_grace_days', return_value={'read':3, 'unread':7}), patch.object(filing.tahor_db, 'relocate_vendor_samples') as relocate:
            result = filing.refile_unsorted(self.conn, {'billing@example.com': ('Shopping', 'Example')} if mapped else {}, 'Filed', dry, self.path)
        return result, relocate

    def test_known_mapping_refiles_old_receipt_without_losing_forever(self):
        result, relocate = self.run_refile()
        self.assertEqual(result, 1)
        self.assertEqual(self.conn.uid.call_args_list[-1].args, ('MOVE', b'9', '"Filed/Shopping/Example/Receipts"'))
        self.assertFalse(any(call.args[0] == 'STORE' for call in self.conn.uid.call_args_list))
        relocate.assert_called_once_with('Filed/_Unsorted/example.com', 'Filed/Shopping/Example/Receipts', ['<sample@example.com>'])
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

    def test_large_folder_continuation_uses_frozen_highwater_and_skips_new_arrivals(self):
        seen=[]
        def fetch(command,uid,*args):
            self.assertEqual(command,'FETCH');seen.append(int(uid))
            header=b'1 (UID '+uid+b' FLAGS (category-receipt retention-forever) INTERNALDATE "01-Jan-2099 00:00:00 +0000")'
            return 'OK',[(header,self.body)]
        self.conn.uid.side_effect=fetch
        folders=[('Filed/_Unsorted/large',set())]+[(f'Filed/_Unsorted/z{i}',set()) for i in range(5)]
        selected=['']
        def select(name,**kwargs):selected[0]=name.strip('"');return 'OK',[]
        self.conn.select.side_effect=select
        uids={str(i).encode() for i in range(1,151)}
        with patch.object(filing,'list_mailboxes',return_value=folders),patch.object(filing,'eligible_uids',side_effect=lambda *args: uids if selected[0].endswith('/large') else set()):
            filing.refile_unsorted(self.conn,{},'Filed',state_path=self.path)
            self.assertEqual(seen,list(range(1,101)))
            self.assertEqual(json.loads(self.path.read_text())['continuations']['Filed/_Unsorted/large']['high'],150)
            seen.clear();uids.add(b'151')
            filing.refile_unsorted(self.conn,{},'Filed',state_path=self.path)
            self.assertEqual(seen,list(range(101,151)))
            self.assertNotIn('Filed/_Unsorted/large',json.loads(self.path.read_text())['continuations'])

    def test_daily_and_maintenance_passes_share_nonblocking_lock(self):
        import fcntl
        diagnostics={}
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(filing.refile_unsorted(self.conn,{},'Filed',state_path=self.path,diagnostics=diagnostics),0)
        self.assertTrue(diagnostics['busy'])
        self.conn.select.assert_not_called()

    def test_exhausted_budget_leaves_folder_cursor_for_next_pass(self):
        with patch.object(filing,'list_mailboxes',return_value=[('Filed/_Unsorted/example',set())]):
            self.assertEqual(filing.refile_unsorted(self.conn,{},'Filed',state_path=self.path,budget_seconds=0),0)
        self.conn.select.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_maintenance_does_not_run_global_read_cleanup_and_always_logs_out(self):
        with patch.object(filing,'connect',return_value=self.conn),patch.object(filing.business_filing,'run_sweep',return_value={'moved':1}) as business,patch.object(filing,'refile_unsorted',return_value=2) as refile,patch.object(filing.config,'vendor_buckets',return_value={}),patch.object(filing,'reconcile_filed_mail') as global_sweep:
            filing.maintenance()
        business.assert_called_once_with(self.conn,limit=100,budget_seconds=45)
        self.assertEqual(refile.call_args.kwargs['budget_seconds'],45)
        global_sweep.assert_not_called();self.conn.logout.assert_called_once()
