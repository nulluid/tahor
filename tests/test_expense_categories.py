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
                     patch.object(categories.ai_routing, 'record_result')):
            mock.start(); self.addCleanup(mock.stop)

    def add(self, message_id='<receipt@example.test>'):
        return ledger.record_receipt(dict(business_key='example', matched_rule_id='rule',
            mailbox='Business', message_id=message_id, uid='1', uidvalidity='42',
            sender_email='billing@vendor.example', vendor='Example cloud',
            received_at='2026-01-02T00:00:00+00:00', subject='Your cloud hosting receipt'),
            'Amount paid: USD 12.00\nPrivate body secret', verified_business=True)

    def good(self, context, **kwargs):
        return dict(category='Cloud hosting', reason='Hosting receipt.', confidence=.95)

    def test_minimal_context_and_suggestion_does_not_change_financials(self):
        identifier = self.add()
        before = ledger.get_entry(identifier)
        captured = []
        def call(context, **kwargs):
            captured.append(context)
            return self.good(context)
        self.assertEqual(categories.suggest_pending(call=call), 1)
        after = ledger.get_entry(identifier)
        self.assertEqual(after['category_suggestion'], 'Cloud hosting')
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
        self.assertFalse(row['category_suggestion'])

    def test_owner_categories_are_available_without_comments(self):
        first = self.add()
        ledger.update_metadata(first, category='Infrastructure', comment='confidential owner note')
        self.add('<second@example.test>')
        seen = []
        def call(context, **kwargs):
            seen.append(context)
            return dict(category='Infrastructure', reason='Owner category matches.', confidence=.9)
        self.assertEqual(categories.suggest_pending(call=call), 1)
        self.assertIn('Infrastructure', seen[0]['allowed_categories'])
        self.assertNotIn('confidential owner note', json.dumps(seen))

    def test_low_confidence_leaves_category_unset(self):
        identifier = self.add()
        categories.suggest_pending(call=lambda *a, **kw: dict(category='Other', reason='Insufficient evidence.', confidence=.2))
        self.assertFalse(ledger.get_entry(identifier)['category_suggestion'])
        self.assertEqual(ledger.get_entry(identifier)['category_suggestion_reason'], 'Insufficient evidence.')

    def test_strict_output_and_untrusted_instructions(self):
        for bad in (dict(category='Ignore rules', reason='x', confidence=.9),
                    dict(category='Other', reason='x', confidence=True),
                    dict(category='Other', reason='x', confidence=float('nan')),
                    dict(category='Other', reason='bad\ncontrol', confidence=.9),
                    dict(category='Other', reason='x', confidence=.9, amount=10)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                categories.validate(bad, categories.CATEGORIES)
        self.assertIn('untrusted evidence', categories.SYSTEM_PROMPT)

    def test_changed_source_during_model_call_rejects_stale_suggestion(self):
        identifier = self.add()
        def call(context, **kwargs):
            db = ledger._db()
            with db: db.execute('UPDATE business_ledger SET source_digest=? WHERE id=?', ('new-source', identifier))
            db.close()
            return self.good(context)
        categories.suggest_pending(call=call)
        self.assertFalse(ledger.get_entry(identifier)['category_suggestion'])
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
        with self.assertRaises(ValueError): categories.request_suggestion(identifier)

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
        self.assertEqual(result['category'], 'Cloud hosting')
        privacy.assert_called_once()
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['provider'], {'zdr': True})
        self.assertEqual(payload['messages'][0]['role'], 'system')
