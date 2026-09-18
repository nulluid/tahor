"""Original accounting evidence survives moves, exports, and private snapshots."""
from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import business_ledger as ledger
import expense_archive as archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'decisions.db'
        patched = patch.object(importlib.import_module('tahor_db'), 'DB_PATH', self.path)
        patched.start(); self.addCleanup(patched.stop)
        self.raw = b'From: Billing <billing@vendor.example>\r\nMessage-ID: <one@example.test>\r\nSubject: Your receipt\r\nContent-Type: multipart/mixed; boundary="demo"\r\n\r\n--demo\r\nContent-Type: text/plain\r\n\r\nAmount paid: USD 123.45\r\n--demo\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="invoice.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi10ZXN0\r\n--demo--\r\n'
        self.metadata = dict(business_key='example',matched_rule_id='rule',mailbox='INBOX',message_id='<one@example.test>',uid='7',uidvalidity='42',sender_email='billing@vendor.example',vendor='../../Example/Company',received_at='2026-01-02T00:00:00+00:00',subject='Your receipt')
        self.identifier = ledger.record_receipt(self.metadata, 'Amount paid: USD 123.45', verified_business=True)

    def client(self, raw=None):
        raw = self.raw if raw is None else raw
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.response.return_value = ('UIDVALIDITY', [b'42'])
        conn.uid.side_effect = [('OK', [b'1 (UID 7 RFC822.SIZE ' + str(len(raw)).encode() + b')']), ('OK', [(b'1 (UID 7 RFC822.SIZE ' + str(len(raw)).encode() + b' BODY[] {' + str(len(raw)).encode() + b'}', raw), b')'])]
        return conn

    def test_capture_is_complete_peek_and_immutable(self):
        client = self.client()
        with patch('fetch_batch.mailbox_uidvalidity', return_value='42'):
            digest = archive.capture(self.identifier, client=client)
        self.assertEqual(digest, hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(archive.read_original(self.identifier), self.raw)
        self.assertIn('BODY.PEEK[]', client.uid.call_args_list[-1].args[2])
        self.assertFalse(client.logout.called)
        self.assertEqual(archive.capture(self.identifier, client=Mock()), digest)
        with self.assertRaises(ValueError):
            archive.store_verified(self.identifier, self.raw + b'changed')
        self.assertEqual(archive.read_original(self.identifier), self.raw)

    def test_rejects_wrong_uid_generation_size_sender_and_identity(self):
        with patch('fetch_batch.mailbox_uidvalidity', return_value='43'), self.assertRaises(RuntimeError):
            archive.capture(self.identifier, client=self.client())
        for raw in (self.raw.replace(b'billing@vendor.example', b'other@vendor.example'), self.raw.replace(b'<one@example.test>', b'<two@example.test>')):
            with patch('fetch_batch.mailbox_uidvalidity', return_value='42'), self.assertRaises(ValueError):
                archive.capture(self.identifier, client=self.client(raw))
        client = self.client()
        client.uid.side_effect = [('OK', [b'1 (UID 8 RFC822.SIZE 10)'])]
        with patch('fetch_batch.mailbox_uidvalidity', return_value='42'), self.assertRaises(RuntimeError):
            archive.capture(self.identifier, client=client)
        client = self.client()
        client.uid.side_effect = [('OK', [b'1 (UID 7 RFC822.SIZE 3)']), ('OK', [(b'1 (UID 7 RFC822.SIZE 3)', b'ab')])]
        with patch('fetch_batch.mailbox_uidvalidity', return_value='42'), self.assertRaises(RuntimeError):
            archive.capture(self.identifier, client=client)
        with self.assertRaises(LookupError):
            archive.read_original(self.identifier)

    def test_generation_response_is_consumed_and_refreshed_after_capture(self):
        for changed in (False, True):
            client = self.client()
            state = {'value': None, 'selects': 0}
            def select(*args, **kwargs):
                state['selects'] += 1
                state['value'] = b'43' if changed and state['selects'] > 1 else b'42'
                return 'OK', []
            def response(name):
                value = state['value']
                state['value'] = None
                return name, [value]
            client.select.side_effect = select
            client.response.side_effect = response
            if changed:
                with self.assertRaisesRegex(RuntimeError, 'generation changed during'):
                    archive.capture(self.identifier, client=client)
            else:
                archive.capture(self.identifier, client=client)
                with closing(archive._db()) as db, db:
                    db.execute('DELETE FROM expense_originals')
            self.assertEqual(state['selects'], 2)

    def test_capacity_never_deletes_existing_source(self):
        with patch.object(archive, 'MAX_ARCHIVE_BYTES', len(self.raw) - 1), self.assertRaises(ValueError):
            archive.store_verified(self.identifier, self.raw)
        archive.store_verified(self.identifier, self.raw)
        with patch.object(archive, 'MAX_ARCHIVE_BYTES', 0):
            archive.store_verified(self.identifier, self.raw)
        self.assertEqual(archive.read_original(self.identifier), self.raw)

    def test_export_safe_names_original_attachments_and_missing_manifest(self):
        archive.store_verified(self.identifier, self.raw)
        missing = ledger.record_receipt(dict(self.metadata, message_id='<missing@example.test>'), 'Amount paid: USD 5.00', verified_business=True)
        rows = ledger.list_entries()
        rows[0]['category'] = '../../Cloud hosting'
        with archive.export_zip(rows, ledger.export_csv()) as stream, zipfile.ZipFile(stream) as zipped:
            names = zipped.namelist()
            self.assertFalse(any('..' in name for name in names))
            originals = [name for name in names if name.endswith('.eml')]
            self.assertEqual(len(originals), 1)
            self.assertIn('2026-01-', originals[0])
            self.assertEqual(zipped.read(originals[0]), self.raw)
            manifest = json.loads(zipped.read('manifest.json'))
            self.assertFalse(manifest['complete'])
            self.assertEqual(next(item['status'] for item in manifest['originals'] if item['entry_id'] == missing), 'missing_or_integrity_failed')
            self.assertIn(b'INCOMPLETE', zipped.read('README.txt'))
            self.assertEqual(len(json.loads(zipped.read('ledger.json'))), 2)
        self.assertEqual(archive.archive_status(rows)['missing'], 1)

    def test_integrity_failure_never_exports_corrupted_email(self):
        archive.store_verified(self.identifier, self.raw)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE expense_originals SET raw_message=x'0001'")
        with self.assertRaises(ValueError): archive.read_original(self.identifier)
        with archive.export_zip(ledger.list_entries(), ledger.export_csv()) as stream, zipfile.ZipFile(stream) as zipped:
            self.assertFalse(any(name.endswith('.eml') for name in zipped.namelist()))
            self.assertFalse(json.loads(zipped.read('manifest.json'))['complete'])

    def test_export_reviews_are_complete_and_limited_to_selected_entries(self):
        omitted = ledger.record_receipt(dict(self.metadata, message_id='<omit@example.test>'), 'Amount paid: USD 9.00', verified_business=True)
        ledger.confirm_entry(self.identifier, document_type='receipt', currency='USD', amount='125.45', document_date='2026-01-02')
        ledger.exclude_entry(omitted)
        selected = [ledger.get_entry(self.identifier)]
        with archive.export_zip(selected, 'test csv') as stream, zipfile.ZipFile(stream) as zipped:
            reviews = json.loads(zipped.read('reviews.json'))
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]['entry_id'], self.identifier)
            self.assertEqual(reviews[0]['before']['amount_minor'], 12345)
            self.assertEqual(reviews[0]['after']['amount_minor'], 12545)
            self.assertTrue(reviews[0]['changed_at'])
        with archive.export_zip([], 'headers only') as stream, zipfile.ZipFile(stream) as zipped:
            self.assertEqual(json.loads(zipped.read('reviews.json')), [])

    def test_zip_uses_seekable_disk_file_and_enforces_total_limit(self):
        archive.store_verified(self.identifier, self.raw)
        with archive.export_zip(ledger.list_entries(), ledger.export_csv()) as stream:
            self.assertTrue(stream.seekable())
            self.assertGreaterEqual(stream.fileno(), 0)
            self.assertEqual(stream.tell(), 0)
            self.assertTrue(zipfile.is_zipfile(stream))
        with patch.object(archive, 'MAX_EXPORT_BYTES', 1), self.assertRaises(RuntimeError):
            archive.export_zip(ledger.list_entries(), ledger.export_csv())
        with patch.object(archive, 'MAX_MESSAGE_BYTES', len(self.raw) - 1), self.assertRaises(ValueError):
            archive.store_verified(self.identifier, self.raw)

    def test_private_backup_and_restore_include_originals(self):
        from scripts import private_backup
        base = Path(self.temp.name).resolve()
        sources = {name: base / name for name in private_backup.NAMES}
        for name in private_backup.NAMES - {'decisions.db', 'business_filing.json', 'coupon_policies.json', 'ai_routing_state.json'}:
            sources[name].write_text('{}' if name.endswith('.json') else '')
        archive.store_verified(self.identifier, self.raw)
        snapshot = private_backup.backup(sources, base / 'backups')
        self.assertIn('decisions.db', private_backup.validate_snapshot(snapshot))
        with closing(sqlite3.connect(self.path)) as db, db: db.execute('DELETE FROM expense_originals')
        private_backup.restore(snapshot, sources, base / 'safety', services_stopped=True)
        self.assertEqual(archive.read_original(self.identifier), self.raw)

    def test_failed_capture_does_not_starve_next_entry(self):
        second = ledger.record_receipt(dict(self.metadata,message_id='<two@example.test>'), 'Amount paid: USD 1.00', verified_business=True)
        with patch.object(archive, 'capture', side_effect=[RuntimeError('offline'), 'hash']) as capture:
            result = archive.capture_missing(limit=2)
        self.assertEqual(result, dict(attempted=2, archived=1, pending=1))
        self.assertEqual(capture.call_args_list[1].args[0], second)
        with patch.object(archive, 'capture') as capture:
            archive.capture_missing(limit=2)
        capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
