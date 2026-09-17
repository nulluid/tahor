import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

import filing_sweep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'decision-app'))
spec = importlib.util.spec_from_file_location('vendor_apply_decisions', Path(__file__).resolve().parents[1] / 'decision-app/apply_decisions.py')
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class VendorRoutingTests(unittest.TestCase):
    def test_exact_merchant_mapping_does_not_route_other_platform_merchants(self):
        buckets = {'alpha@t.shopifyemail.com': ['Shopping', 'Alpha']}
        self.assertEqual(filing_sweep.vendor_for('Alpha <alpha@t.shopifyemail.com>', buckets), ['Shopping', 'Alpha'])
        self.assertEqual(filing_sweep.vendor_for('Beta <beta@t.shopifyemail.com>', buckets), ('_Unsorted', 't.shopifyemail.com'))

    def test_exact_sender_takes_precedence_and_legacy_domain_still_works(self):
        buckets = {'billing@example.com': ['Shopping', 'Billing'], 'example.com': ['Shopping', 'Company']}
        self.assertEqual(filing_sweep.vendor_for('Billing <BILLING@EXAMPLE.COM>', buckets), ['Shopping', 'Billing'])
        self.assertEqual(filing_sweep.vendor_for('alerts@example.com', buckets), ['Shopping', 'Company'])
        self.assertEqual(filing_sweep.vendor_for('alerts@sub.example.org', {'example': ['Shopping', 'Legacy']}), ['Shopping', 'Legacy'])

    def test_apply_stores_exact_key_without_broadening_to_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vendors.json'
            path.write_text('{"existing.example":["Shopping","Existing"]}')
            context = dict(sender_label='t.shopifyemail.com', sender_email='alpha@t.shopifyemail.com', routing_key='alpha@t.shopifyemail.com')
            with patch.object(apply, 'VENDOR_BUCKETS_PATH', path):
                apply.apply_vendor_mapping({'context': json.dumps(context), 'summary': 'Route merchant'},
                                           dict(action='map', bucket='Shopping', vendor_name='Alpha'))
            buckets = json.loads(path.read_text())
            self.assertIn('existing.example', buckets)
            self.assertEqual(buckets['alpha@t.shopifyemail.com'], ['Shopping', 'Alpha'])
            self.assertNotIn('t.shopifyemail.com', buckets)

    def test_legacy_shared_domain_and_inconsistent_exact_scope_fail_closed(self):
        cases = [dict(sender_label='t.shopifyemail.com'),
                 dict(sender_label='shopifyemail'),
                 dict(sender_label='example.com', sender_email='alpha@example.com'),
                 dict(sender_label='example.com', sender_email='alpha@example.com', routing_key='beta@example.com'),
                 dict(sender_label='example.com', sender_email='alpha@example.com', routing_key='example.com')]
        with patch.object(apply, 'atomic_write') as write:
            for context in cases:
                with self.subTest(context=context), self.assertRaises(ValueError):
                    apply.apply_vendor_mapping({'context': json.dumps(context), 'summary': 'Route merchant'},
                                               dict(action='map', bucket='Shopping', vendor_name='Merchant'))
            write.assert_not_called()

    def test_legacy_explicit_domain_mapping_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vendors.json'
            with patch.object(apply, 'VENDOR_BUCKETS_PATH', path):
                apply.apply_vendor_mapping({'context': '{"sender_label":"example.com"}', 'summary': 'Route company'},
                                           dict(action='map', bucket='Shopping', vendor_name='Company'))
            self.assertEqual(json.loads(path.read_text()), {'example.com': ['Shopping', 'Company']})

    def test_filing_queues_decoded_message_context_without_marking_read(self):
        client = Mock()
        client.capabilities = ('MOVE',)
        client.select.return_value = ('OK', [])
        client.list.return_value = ('OK', [b'folder'])
        def uid(command, *args):
            if command == 'FETCH':
                return 'OK', [(b'1 (UID 7 INTERNALDATE "17-Sep-2026 01:02:03 +0000")',
                    b'From: =?utf-8?q?Alpha_Store?= <alpha@t.shopifyemail.com>\r\nSubject: Receipt #123\r\nDate: Thu, 17 Sep 2026 01:00:00 +0000\r\n\r\n')]
            return 'OK', []
        client.uid.side_effect = uid
        with patch.object(filing_sweep, 'connect', return_value=client), \
             patch.object(filing_sweep.config, 'vendor_buckets', return_value={}), \
             patch.object(filing_sweep, 'eligible_uids', return_value={b'7'}), \
             patch.object(filing_sweep, 'reply_filing_destinations', return_value={}), \
             patch.object(filing_sweep, 'mark_filed_read', return_value=1), \
             patch.object(filing_sweep, 'reconcile_filed_mail', return_value=(0, 0)), \
             patch.object(filing_sweep.tahor_db, 'queue_vendor_mapping') as queue, \
             patch.object(filing_sweep.sys, 'argv', ['filing_sweep.py']):
            filing_sweep.main()
        metadata = queue.call_args.kwargs['metadata']
        self.assertEqual(metadata['sender_email'], 'alpha@t.shopifyemail.com')
        self.assertEqual(metadata['display_name'], 'Alpha Store')
        self.assertEqual(metadata['subject'], 'Receipt #123')
        self.assertEqual(metadata['received_at'], '17-Sep-2026 01:02:03 +0000')
        fetch = next(c for c in client.uid.call_args_list if c.args[0] == 'FETCH')
        self.assertIn('BODY.PEEK', fetch.args[2])
