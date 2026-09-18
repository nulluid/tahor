from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import filing_sweep as filing
from test_filed_read_state import FolderMailbox
from test_unread_grace import message, FrozenDateTime


class FilingExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state.json'
        for target, options in [(patch.object(filing,'datetime',FrozenDateTime),{}),
                                (patch.object(filing.mailbox_settings,'get_inbox_grace_days',return_value={'read':3,'unread':7}),{}),
                                (patch.object(filing.reply_rules,'get_rules',return_value=[]),{}),
                                (patch.object(filing.business_filing,'load_rules',return_value=[]),{})]:
            target.start();self.addCleanup(target.stop)

    def row(self,category,**changes):
        row=message(10);row['tags']={'retention-standard','category-'+category};row.update(changes);return row

    def sweep(self,conn):
        with patch.object(filing,'connect',return_value=conn),patch.object(filing.config,'vendor_buckets',return_value={'billing@example.com':['Records','Example']}),patch.object(filing.config,'filing_root',return_value='Filed'),patch.object(filing,'list_mailboxes',return_value=[]),patch('sys.argv',['filing_sweep.py']):
            filing.main()

    def test_aged_legal_medical_and_correspondence_file_read_separately_from_receipts(self):
        rows={b'1':self.row('legal'),b'2':self.row('medical'),b'3':self.row('personal-correspondence'),b'4':self.row('receipt')}
        conn=FolderMailbox({'INBOX':rows,'Filed/Records/Example/Correspondence':{},'Filed/Records/Example/Receipts':{}})
        self.sweep(conn)
        self.assertEqual(len(conn.folders['Filed/Records/Example/Correspondence']),3)
        self.assertEqual(len(conn.folders['Filed/Records/Example/Receipts']),1)
        self.assertTrue(all(row['read'] for folder in conn.folders.values() for row in folder.values()))

    def test_attention_reply_draft_deleted_and_recent_mail_remain_inbox(self):
        rows={str(i).encode():self.row('medical') for i in range(1,7)}
        rows[b'1']['tags'].add('needs-attention');rows[b'2']['tags'].add('reply-protected')
        rows[b'3']['draft']=True;rows[b'4']['deleted']=True;rows[b'5']['tags'].add('delete-pending');rows[b'6']=self.row('legal',date=message(1)['date'])
        conn=FolderMailbox({'INBOX':rows})
        self.sweep(conn)
        self.assertEqual(set(conn.folders['INBOX']),set(rows));self.assertEqual(conn.moved,[])

    def test_changed_mailbox_generation_never_moves_saved_uid(self):
        conn=FolderMailbox({'INBOX':{b'1':self.row('legal')},'Filed/Records/Example/Correspondence':{}})
        calls=[0]
        def response(code):
            calls[0]+=1
            return code,[b'123' if calls[0]==1 else b'999']
        conn.response=response
        with self.assertRaisesRegex(RuntimeError,'generation changed'):self.sweep(conn)
        self.assertEqual(conn.moved,[])

    def test_old_vendor_root_reconciles_once_without_nested_receipt_folders(self):
        base='Filed/Records/Example';receipt=base+'/Receipts';other=base+'/Correspondence'
        conn=FolderMailbox({base:{b'1':self.row('receipt'),b'2':self.row('legal')},receipt:{},other:{}})
        with patch.object(filing.tahor_db,'relocate_vendor_samples'):
            self.assertEqual(filing.refile_unsorted(conn,{'billing@example.com':['Records','Example']},'Filed',state_path=self.state),2)
            self.assertEqual(filing.refile_unsorted(conn,{'billing@example.com':['Records','Example']},'Filed',state_path=self.state),0)
        self.assertEqual(set(conn.folders),{base,receipt,other})
        self.assertTrue(all(row['read'] for name in (receipt,other) for row in conn.folders[name].values()))
