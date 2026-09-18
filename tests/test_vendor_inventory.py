import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import vendor_inventory as inventory
import vendor_suggestions

QUEUE_VENDOR = inventory.tahor_db.queue_vendor_mapping


class VendorInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'decisions.db'
        with self.db() as conn:
            conn.execute('CREATE TABLE decisions (id INTEGER PRIMARY KEY,kind TEXT,status TEXT,context TEXT,resolution TEXT, summary TEXT, created_at TEXT)')
        self.start(patch.object(inventory.tahor_db, 'get_db', side_effect=self.db))
        self.start(patch.object(inventory.config, 'filing_root', return_value='Filed'))
        self.client = Mock(); self.client.list.side_effect = lambda reference, folder: ('OK', [b'folder'] if folder == '"Filed/_Unsorted/delivery.example"' else [None])
        self.client.select.return_value = ('OK', [])
        self.client.response.return_value = ('UIDVALIDITY', [b'123'])
        self.client.uid.side_effect = self.uid
        self.connect = self.start(patch.object(inventory.fetch_batch, 'connect', return_value=self.client))
        self.start(patch.object(inventory, 'search_uids', return_value=('OK', [b'1 2'])))
        self.queue = self.start(patch.object(inventory.tahor_db, 'queue_vendor_mapping'))

    def start(self, p):
        value = p.start(); self.addCleanup(p.stop); return value

    def db(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close); return conn

    def add(self, label='delivery.example', number=1):
        with self.db() as conn:
            conn.execute('INSERT INTO decisions (id,kind,status,context,resolution) VALUES (?, ?, ?, ?, NULL)',
                         (number, 'vendor_mapping', 'pending', json.dumps({'sender_label': label})))

    def uid(self, command, uid, spec):
        self.assertEqual(command, 'FETCH'); self.assertIn('BODY.PEEK[]<0.8192>', spec)
        return 'OK', [(b'1 (UID ' + uid + b' INTERNALDATE "17-Sep-2026 12:00:00 +0000")',
            b'From: Store '+uid+b' <store'+uid+b'@delivery.example>\r\nMessage-ID: <receipt'+uid+b'@example.com>\r\nSubject: Receipt\r\nContent-Type: text/plain\r\n\r\nYour completed purchase receipt.')]

    def test_shared_platform_samples_keep_exact_sender_and_mailbox_identity(self):
        self.add(); self.assertEqual(inventory.enrich_pending(), 0)
        self.assertEqual(self.queue.call_count, 2)
        values = [call.kwargs['metadata'] for call in self.queue.call_args_list]
        self.assertEqual({value['sender_email'] for value in values}, {'store1@delivery.example', 'store2@delivery.example'})
        self.assertTrue(all(value['uidvalidity'] == '123' and value['mailbox'] == 'Filed/_Unsorted/delivery.example' for value in values))
        self.assertIn('completed purchase', values[0]['excerpt'])
        self.client.select.assert_called_once_with('"Filed/_Unsorted/delivery.example"', readonly=True)
        self.assertTrue(all(call.args[0] == 'FETCH' for call in self.client.uid.call_args_list))

    def test_real_queue_splits_platform_senders_and_preserves_excerpt(self):
        self.add()
        with patch.object(inventory.tahor_db, 'queue_vendor_mapping', side_effect=QUEUE_VENDOR):
            self.assertEqual(inventory.enrich_pending(), 0)
        with self.db() as conn:
            contexts = [json.loads(row['context']) for row in conn.execute('SELECT context FROM decisions')]
        self.assertEqual(len(contexts), 2)
        self.assertEqual({item['routing_key'] for item in contexts}, {'store1@delivery.example', 'store2@delivery.example'})
        self.assertTrue(all('completed purchase' in item['samples'][0]['excerpt'] for item in contexts))

    def test_limit_and_missing_folder_do_not_block_other_work(self):
        for number in range(1, 6): self.add('vendor'+str(number)+'.example', number)
        self.client.list.side_effect = None; self.client.list.return_value = ('OK', [None])
        self.assertEqual(inventory.enrich_pending(), 0)
        self.assertEqual(self.client.list.call_count, 12)
        self.queue.assert_not_called(); self.client.select.assert_not_called()

    def test_legacy_work_is_automatic_until_missing_folder_becomes_an_exception(self):
        self.add()
        with self.db() as conn:
            self.assertEqual(vendor_suggestions.pending_work_ids(conn), ['vendor:1'])
        self.client.list.side_effect = None; self.client.list.return_value = ('OK', [None])
        inventory.enrich_pending()
        with self.db() as conn:
            self.assertEqual(vendor_suggestions.pending_work_ids(conn), [])
            self.assertEqual(json.loads(conn.execute('SELECT context FROM decisions').fetchone()[0])['inventory_status'], 'missing_folder')

    def test_uid_mismatch_never_publishes_wrong_sample(self):
        self.add(); self.client.uid.return_value = ('OK', [(b'1 (UID 99)', b'From: a@example.com\r\n')])
        self.client.uid.side_effect = None
        self.assertEqual(inventory.enrich_pending(), 1)
        self.queue.assert_not_called()

    def test_path_like_legacy_label_does_not_trigger_mailbox_lookup(self):
        self.add('../Inbox')
        self.assertEqual(inventory.enrich_pending(), 0)
        self.connect.assert_not_called()

    def test_new_receipts_and_correspondence_subfolders_are_sampled(self):
        self.add()
        self.client.list.side_effect = lambda reference, folder: ('OK', [b'folder'] if folder.endswith('/Receipts"') or folder.endswith('/Correspondence"') else [None])
        self.assertEqual(inventory.enrich_pending(), 0)
        self.assertEqual({call.kwargs['metadata']['mailbox'] for call in self.queue.call_args_list},
                         {'Filed/_Unsorted/delivery.example/Receipts', 'Filed/_Unsorted/delivery.example/Correspondence'})
        self.assertTrue(all(call.args[1:] == ('ALL',) for call in inventory.search_uids.call_args_list))
        self.assertFalse(any(call.args[0] == '"INBOX"' for call in self.client.select.call_args_list))

    def test_missing_legacy_folder_recovers_from_inbox_without_category_filter(self):
        self.add()
        self.client.list.side_effect = lambda reference, folder: ('OK', [b'folder'] if folder == '"INBOX"' else [None])
        self.assertEqual(inventory.enrich_pending(), 0)
        self.assertEqual(self.queue.call_count, 2)
        self.assertTrue(all(call.kwargs['metadata']['mailbox'] == 'INBOX' for call in self.queue.call_args_list))
        inventory.search_uids.assert_called_once_with(self.client, 'FROM', '"@delivery.example"')

    def test_unrelated_and_deceptive_from_domains_are_not_evidence(self):
        self.add()
        original = self.uid
        for sender in (b'person@unrelated.example', b'person@delivery.example.evil.test',
                       b'person@evildelivery.example', b'a@delivery.example, b@delivery.example'):
            with self.subTest(sender=sender):
                def response(command, uid, spec):
                    status, rows = original(command, uid, spec)
                    return status, [(rows[0][0], b'From: ' + sender + b'\r\nMessage-ID: <one@example.com>\r\nSubject: example\r\n\r\n')]
                self.client.uid.side_effect = response
                self.assertEqual(inventory.enrich_pending(), 0)
                self.queue.assert_not_called()

    def test_sample_fetch_is_bounded(self):
        self.add()
        inventory.search_uids.return_value = ('OK', [b' '.join(str(i).encode() for i in range(1, 101))])
        self.assertEqual(inventory.enrich_pending(), 0)
        self.assertEqual(self.client.uid.call_count, 25)
        self.assertEqual(self.client.uid.call_args_list[0].args[1], b'76')
