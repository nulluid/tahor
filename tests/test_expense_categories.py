"""Private suggestions cannot approve amounts or replace the owner's category."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import business_ledger as ledger
import expense_categories as categories
import tahor_db


class ExpenseCategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mock in (patch.object(tahor_db, 'DB_PATH', Path(self.temp.name) / 'private.db'),
                     patch.object(categories.mailbox_settings, 'is_ai_enabled', return_value=True),
                     patch.object(categories.ai_routing, 'record_result'),
                     patch.object(categories.mailbox_settings, 'load_settings', return_value={})):
            mock.start(); self.addCleanup(mock.stop)

    def add(self, message_id='<receipt@example.test>'):
        return ledger.record_receipt(dict(business_key='example', matched_rule_id='rule',
            mailbox='Business', message_id=message_id, uid='1', uidvalidity='42',
            sender_email='billing@vendor.example', vendor='Example cloud',
            received_at='2026-01-02T00:00:00+00:00', subject='Your cloud hosting receipt'),
            'Amount paid: USD 12.00\nPrivate body secret', verified_business=True)

    def good(self, context, **kwargs):
        return dict(fields=dict(vendor='Example cloud', document_date='2026-01-02',
            document_type='receipt', reference=None, amount='12.00', currency='USD',
            category='Hosting', comment='Cloud infrastructure usage'), reason='Hosting receipt.', confidence=.95)

    def test_minimal_context_and_suggestion_does_not_change_financials(self):
        identifier = self.add()
        before = ledger.get_entry(identifier)
        captured = []
        def call(context, **kwargs):
            captured.append(context)
            return self.good(context)
        self.assertEqual(categories.suggest_pending(call=call), 1)
        after = ledger.get_entry(identifier)
        self.assertEqual(json.loads(after['ai_suggestions'])['category'], 'Hosting')
        self.assertFalse(after['category'])
        for field in ('amount_minor', 'currency', 'status', 'owner_confirmed'):
            self.assertEqual(before[field], after[field])
        content = json.dumps(captured)
        for secret in ('Private body secret', 'billing@vendor.example', '<receipt@example.test>', '12.00'):
            self.assertNotIn(secret, content)
        self.assertEqual(categories.suggest_pending(call=call), 0)

    def test_disabled_and_bounded_work(self):
        for i in range(4): self.add('<%d@example.test>' % i)
        with patch.object(categories.mailbox_settings, 'is_ai_enabled', return_value=False):
            self.assertEqual(categories.suggest_pending(call=self.good), 0)
        self.assertEqual(categories.suggest_pending(limit=2, call=self.good), 2)
        self.assertEqual(categories.suggest_pending(limit=2, call=self.good), 2)

    def test_failed_work_retries_and_never_persists_provider_error(self):
        identifier = self.add()
        def fail(*args, **kwargs): raise RuntimeError('private echoed credentials')
        with patch.object(categories.time, 'time', return_value=100):
            self.assertEqual(categories.suggest_pending(call=fail), 0)
        with patch.object(categories.time, 'time', return_value=200):
            self.assertEqual(categories.suggest_pending(call=self.good), 0)
        with patch.object(categories.time, 'time', return_value=401):
            self.assertEqual(categories.suggest_pending(call=self.good), 1)
        db = ledger._db()
        self.assertNotIn('private echoed', str([dict(r) for r in db.execute('SELECT * FROM expense_category_work')]))
        db.close()

    def test_owner_edit_during_call_is_preserved_and_no_category_suggestion(self):
        identifier = self.add()
        def call(context, **kwargs):
            ledger.update_metadata(identifier, category='Equipment', comment='Owner choice stays private')
            return self.good(context)
        categories.suggest_pending(call=call)
        row = ledger.get_entry(identifier)
        self.assertEqual(row['category'], 'Equipment')
        self.assertEqual(row['comment'], 'Owner choice stays private')
        self.assertEqual(json.loads(row['ai_suggestions'])['category'], 'Hosting')

    def test_owner_categories_are_available_without_comments(self):
        first = self.add()
        ledger.update_metadata(first, category='Infrastructure', comment='confidential owner note')
        self.add('<second@example.test>')
        seen = []
        def call(context, **kwargs):
            seen.append(context)
            result = self.good(context)
            result['fields']['category'] = 'Infrastructure'
            return result
        self.assertEqual(categories.suggest_pending(call=call), 2)
        self.assertIn('Infrastructure', seen[0]['allowed_categories'])
        self.assertNotIn('confidential owner note', json.dumps(seen))

    def test_unsupported_fields_remain_null(self):
        identifier = self.add()
        categories.suggest_pending(call=lambda *a, **kw: dict(fields=dict.fromkeys(categories.FIELDS), reason='Insufficient evidence.', confidence=.2))
        self.assertFalse(json.loads(ledger.get_entry(identifier)['ai_suggestions'])['category'])
        self.assertEqual(ledger.get_entry(identifier)['ai_suggestion_reason'], 'Insufficient evidence.')

    def test_strict_output_and_untrusted_instructions(self):
        for key, value in (('category', 'Ignore rules'), ('document_date', '2026-13-40'),
                           ('amount', '-12.00'), ('amount', '1e3'), ('currency', 'XYZ'),
                           ('reference', 'bad\ncontrol'), ('document_type', 'paid')):
            bad = self.good({}); bad['fields'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                categories.validate(bad, categories.CATEGORIES)
        for confidence in (True, float('nan'), -1, 2):
            bad = self.good({}); bad['confidence'] = confidence
            with self.assertRaises(ValueError): categories.validate(bad, categories.CATEGORIES)
        bad = self.good({}); del bad['fields']['amount']
        with self.assertRaises(ValueError): categories.validate(bad, categories.CATEGORIES)
        self.assertIn('untrusted evidence', categories.SYSTEM_PROMPT)

    def test_changed_source_during_model_call_rejects_stale_suggestion(self):
        identifier = self.add()
        def call(context, **kwargs):
            db = ledger._db()
            with db: db.execute('UPDATE business_ledger SET source_digest=? WHERE id=?', ('new-source', identifier))
            db.close()
            return self.good(context)
        categories.suggest_pending(call=call)
        self.assertFalse(json.loads(ledger.get_entry(identifier)['ai_suggestions']))
        self.assertEqual(categories.suggest_pending(call=self.good), 1)

    def test_explicit_request_queues_without_model_call_and_can_refresh_done(self):
        identifier = self.add()
        self.assertTrue(categories.request_suggestion(identifier))
        db = ledger._db()
        self.assertEqual(categories.pending_work_ids(db), ['expense-category:' + str(identifier)])
        db.close()
        self.assertEqual(categories.suggest_pending(call=self.good), 1)
        self.assertTrue(categories.request_suggestion(identifier))
        self.assertEqual(categories.suggest_pending(call=self.good), 1)
        ledger.update_metadata(identifier, category='Cloud hosting', comment='')
        self.assertTrue(categories.request_suggestion(identifier))

    def test_running_request_is_not_duplicated(self):
        identifier = self.add()
        def call(context, **kwargs):
            self.assertFalse(categories.request_suggestion(identifier))
            self.assertEqual(categories.suggest_pending(call=self.good), 0)
            return self.good(context)
        self.assertEqual(categories.suggest_pending(call=call), 1)

    def test_model_uses_rule_policy_and_privacy_payload(self):
        import io
        backend = dict(auth_env='TAHOR_TEST_KEY', model='example/model', url='https://example.test')
        response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(self.good({}))}}]}).encode())
        context = dict(allowed_categories=list(categories.CATEGORIES), untrusted_expense={'vendor': 'Example'})
        def routing(task, registry, operation, **kwargs):
            self.assertEqual(task, 'rule')
            self.assertEqual(kwargs['work_id'], 'expense-category:1')
            return operation('example')
        with patch.dict(categories.os.environ, {'TAHOR_TEST_KEY': 'private-token'}), \
                patch.object(categories.mailbox_settings, 'RULE_MODELS', {'example': backend}), \
                patch.object(categories.ai_routing, 'run', side_effect=routing), \
                patch.object(categories, 'private_request_payload', side_effect=lambda backend, value: dict(value, provider={'zdr': True})) as privacy, \
                patch.object(categories.urllib.request, 'urlopen', return_value=response) as request:
            result = categories.model_call(context, 1, 'expense-category:1')
        self.assertEqual(result['fields']['category'], 'Hosting')
        privacy.assert_called_once()
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['provider'], {'zdr': True})
        self.assertEqual(payload['messages'][0]['role'], 'system')

    def test_verified_original_supports_every_field_with_private_purpose(self):
        from email.message import EmailMessage
        import expense_archive
        identifier = self.add()
        message = EmailMessage()
        message['From'] = 'billing@vendor.example'
        message['Message-ID'] = '<receipt@example.test>'
        message.set_content('Amount paid: USD 12.00\nReceipt ID: REF-123\nDate: 2026-01-02')
        message.add_attachment(b'Never transmit this attachment', maintype='text', subtype='plain', filename='private.txt')
        captured = []
        def call(context, **kwargs):
            captured.append(context)
            result = self.good(context)
            result['fields']['reference'] = 'REF-123'
            return result
        with patch.object(expense_archive, 'read_original', return_value=message.as_bytes()), \
                patch.object(categories.mailbox_settings, 'load_settings', return_value={
                    'expense_purpose_hints': {'Example cloud': 'API model usage', 'Other vendor': 'Private unrelated hint'}}):
            self.assertEqual(categories.suggest_pending(call=call), 1)
        fields = json.loads(ledger.get_entry(identifier)['ai_suggestions'])
        self.assertEqual(fields['amount'], '12.00')
        self.assertEqual(fields['reference'], 'REF-123')
        self.assertEqual(captured[0]['trusted_owner_purpose'], 'API model usage')
        sent = json.dumps(captured)
        self.assertNotIn('Never transmit this attachment', sent)
        self.assertNotIn('Private unrelated hint', sent)
        self.assertIn('Amount paid', sent)
        self.assertEqual(set(fields), set(categories.FIELDS))

    def test_no_original_never_suggests_amount_currency_or_reference(self):
        identifier = self.add()
        categories.suggest_pending(call=self.good)
        fields = json.loads(ledger.get_entry(identifier)['ai_suggestions'])
        for key in ('amount', 'currency', 'reference'):
            self.assertIsNone(fields[key])
        self.assertEqual(fields['comment'], 'Cloud infrastructure usage')

    def test_amount_must_be_present_and_refund_stays_unsigned(self):
        proposal = self.good({})
        proposal['fields']['document_type'] = 'refund'
        self.assertIsNone(categories._grounded(proposal, 'Amount refunded USD 45.00')['fields']['amount'])
        self.assertEqual(categories._grounded(proposal, 'Amount refunded USD -12.00')['fields']['amount'], '12.00')
        proposal['fields']['amount'] = '1234.50'
        self.assertEqual(categories._grounded(proposal, 'Amount paid USD 1,234.50')['fields']['amount'], '1234.50')

    def test_body_bounded_and_forwarded_attachments_excluded(self):
        from email.message import EmailMessage
        message = EmailMessage()
        message.set_content('é' * 20000)
        forwarded = EmailMessage()
        forwarded.set_content('Private forwarded message')
        message.add_attachment(forwarded)
        text = categories._body(message.as_bytes())
        self.assertLessEqual(len(text.encode()), 16384)
        self.assertNotIn('Private forwarded', text)

    def test_archival_and_private_hint_changes_invalidate_metadata_only_result(self):
        row = dict(source_digest='same', vendor='Example', subject='Receipt')
        initial = categories._revision(row)
        self.assertNotEqual(initial, categories._revision(row, original_sha='archived-original'))
        self.assertNotEqual(initial, categories._revision(row, purpose='API model usage'))
