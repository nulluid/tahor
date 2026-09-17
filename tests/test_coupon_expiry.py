import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import coupon_expiry as coupon
import retention_sweep


class CouponTests(unittest.TestCase):
    POLICY = {'offers@example.com': {'folder': 'Shopping/Coupons/Example', 'date_order': 'mdy'}}

    def test_source_dates_require_explicit_year_and_unambiguous_expiry(self):
        for source in ('Expires September 30, 2026', 'Valid through 2026-09-30', 'Redeem by 30 September 2026'):
            self.assertEqual(coupon.expiration(source), date(2026, 9, 30))
        for source in ('Expires September 30', 'Expires 09/10/2026', 'Expires 2026-02-30',
                       'Expires 2026-09-30. Another offer expires October 2, 2026',
                       'Expires 2026-09-30. Another offer expires October 2',
                       'Sale September 30, 2026', 'Expires 2026-09-30' + 'x' * 131072):
            self.assertIsNone(coupon.expiration(source), source[:80])
        self.assertEqual(coupon.expiration('Expires 09/10/2026', 'mdy'), date(2026, 9, 10))
        self.assertEqual(coupon.expiration('Expires 09/10/2026', 'dmy'), date(2026, 10, 9))

    def test_policy_exact_address_and_folder_validation(self):
        self.assertEqual(coupon.policy_for('Example <offers@example.com>', self.POLICY), self.POLICY['offers@example.com'])
        self.assertIsNone(coupon.policy_for('other@example.com', self.POLICY))
        self.assertIsNone(coupon.policy_for('offers@sub.example.com', self.POLICY))
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'DATA_DIR': directory, 'TAHOR_COUPON_POLICIES_PATH': ''}):
            self.assertEqual(coupon.policies(), {})
            path = Path(directory) / 'coupon_policies.json'
            path.write_text(json.dumps(self.POLICY))
            self.assertEqual(coupon.policies(), self.POLICY)
            path.write_text(json.dumps({'offers@example.com': {'folder': '../Trash'}}))
            with self.assertRaises(ValueError):
                coupon.policies()

    def test_retention_preserves_review_receipts_and_model_errors(self):
        base = dict(action='trash', category='marketing', retention='transient')
        coupon.protect_result(base, 'offers@example.com', 'Expires 2026-09-30', self.POLICY)
        self.assertEqual((base['action'], base['retention']), ('keep', 'standard'))
        self.assertIn('coupon-expiry-20260930', base['coupon_keywords'])
        for category, action, retention in [('receipt', 'keep', 'forever'), ('marketing', 'error', 'standard')]:
            value = dict(category=category, action=action, retention=retention)
            before = copy.deepcopy(value)
            coupon.protect_result(value, 'offers@example.com', '', self.POLICY)
            self.assertEqual(value, before)
        for retention in ('forever', 'pending-review'):
            value = dict(category='marketing', action='mixed', retention=retention, needs_attention=True)
            coupon.protect_result(value, 'offers@example.com', '', self.POLICY)
            self.assertEqual(value['retention'], retention)
            self.assertEqual(value['action'], 'mixed')
            self.assertTrue(value['needs_attention'])
            self.assertEqual(value['coupon_keywords'], ['retention-coupon'])

    def test_expiry_waits_for_date_everywhere_and_honors_protections(self):
        flags = {b'retention-coupon', b'category-marketing', b'coupon-expiry-20260930'}
        before = datetime(2026, 10, 1, 11, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        self.assertFalse(coupon.expired(flags, before))
        self.assertTrue(coupon.expired(flags, after))
        for flag in coupon.PROTECTED | {b'category-receipt', b'coupon-expiry-20261002', b'coupon-expiry-invalid'}:
            self.assertFalse(coupon.expired(flags | {flag}, after), flag)
        self.assertFalse(coupon.expired(flags - {b'coupon-expiry-20260930'}, after))

    def test_sweep_rechecks_attention_before_deleting(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        row = b'1 (UID 9 FLAGS (retention-coupon category-marketing coupon-expiry-20260930) INTERNALDATE "01-Sep-2026 00:00:00 +0000")'
        protected = row.replace(b'FLAGS (', b'FLAGS (needs-attention ')
        delete = Mock(return_value=(1, 1))
        with patch.object(coupon, 'search_uids', return_value=('OK', [b'9'])):
            conn.uid.side_effect = [('OK', [row]), ('OK', [protected])]
            self.assertEqual(coupon.sweep(conn, 'INBOX', False, delete, datetime(2026, 10, 2, tzinfo=timezone.utc)), (0, 0))
            delete.assert_not_called()
            conn.uid.side_effect = [('OK', [row]), ('OK', [row])]
            self.assertEqual(coupon.sweep(conn, 'INBOX', False, delete, datetime(2026, 10, 2, tzinfo=timezone.utc)), (1, 1))
            delete.assert_called_once_with(conn, [b'9'], False)

    def test_other_retention_paths_exclude_coupon_marker(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        with patch.object(retention_sweep, 'search_uids', return_value=('OK', [b''])) as search:
            retention_sweep.sweep_mailbox(conn, 'INBOX', 'retention-standard', 30, True)
            retention_sweep.sweep_trash(conn, 'INBOX', True)
            retention_sweep.sweep_short_lived(conn, 'INBOX', True)
            for call in search.call_args_list:
                args = call.args
                position = args.index(coupon.KEYWORD)
                self.assertEqual(args[position - 1], 'UNKEYWORD')

    def test_pipeline_tags_coupon_as_complete_and_never_deletes_it(self):
        import process_batch
        import sys
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'batch')
            inputs = [dict(id='coupon', subject='An offer', **{'from': 'offers@example.com'}, coupon_source='Expires September 30, 2026', date='2026-09-01T00:00:00+00:00')]
            outputs = [dict(id='coupon', action='trash', category='marketing', retention='transient', needs_attention=False)]
            envelopes = [dict(message_id='coupon', uid='10', subject='An offer', from_email='offers@example.com', internaldate='01-Sep-2026 00:00:00 +0000')]
            for suffix, rows in [('in', inputs), ('out', outputs), ('env', envelopes)]:
                Path(prefix + '_' + suffix + '.json').write_text(json.dumps(rows))
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(coupon, 'policies', return_value=self.POLICY), patch.object(process_batch.reply_rules, 'get_rules', return_value=[]), patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None) as sender_rule, patch.object(process_batch.tahor_db, 'get_unsubscribe_candidate', return_value=None) as unsubscribe, patch.object(process_batch.tahor_db, 'has_sender_sample', return_value=False):
                process_batch.main()
                normal_ops = json.loads(Path(prefix + '_ops.json').read_text())
                for block_rule, status in [('block_all', None), ('block_marketing', None), (None, {'status': 'unsubscribed'})]:
                    sender_rule.return_value = block_rule
                    unsubscribe.return_value = status
                    process_batch.main()
                    blocked_ops = json.loads(Path(prefix + '_ops.json').read_text())
                    self.assertTrue(blocked_ops[0].get('delete'))
                    self.assertNotIn('retention-coupon', blocked_ops[0]['add'])
            operations = normal_ops
            self.assertEqual(len(operations), 1)
            self.assertFalse(operations[0].get('delete'))
            self.assertTrue({'retention-standard', 'retention-coupon', 'coupon-expiry-20260930'} <= set(operations[0]['add']))

    def test_coupon_filing_search_obeys_read_grace_and_attention(self):
        import filing_sweep
        conn = Mock()
        with patch.object(filing_sweep, 'search_uids', return_value=('OK', [b''])) as search:
            filing_sweep.eligible_uids(conn, ('UNSEEN', 'BEFORE', '10-Sep-2026'))
        args = search.call_args_list[-1].args
        self.assertIn(coupon.KEYWORD, args)
        for term in ('UNSEEN', 'BEFORE', '10-Sep-2026', 'category-marketing', 'needs-attention', 'reply-protected', 'UNFLAGGED'):
            self.assertIn(term, args)

    def test_global_opt_in_requires_coupon_language_and_derives_safe_folder(self):
        policies = {'*': {'folder': 'Shopping/Coupons'}}
        policy = coupon.policy_for('Offers <news@example.org>', policies)
        self.assertEqual(policy['folder'], 'Shopping/Coupons/example.org')
        self.assertIsNone(coupon.policy_for('news@../Trash', policies))
        for source in ('Here is your coupon.', 'Use promo code SAVE20', 'Your voucher expires 2026-12-31'):
            result = dict(action='trash', category='marketing', retention='transient')
            coupon.protect_result(result, 'news@example.org', source, policies)
            self.assertEqual(result['action'], 'keep')
            self.assertIn('retention-coupon', result['coupon_keywords'])
        for source in ('Our weekly newsletter', 'Sale ends 2026-12-31. Everything 20% off.'):
            result = dict(action='trash', category='marketing', retention='transient')
            original = dict(result)
            coupon.protect_result(result, 'news@example.org', source, policies)
            self.assertEqual(result, original)
        configured = dict(policies, **self.POLICY)
        self.assertEqual(coupon.policy_for('offers@example.com', configured)['folder'], 'Shopping/Coupons/Example')
        self.assertNotIn('detect_coupon', coupon.policy_for('offers@example.com', configured))
        result = dict(action='trash', category='marketing', retention='transient')
        coupon.protect_result(result, 'news@example.org', 'Coupon expires 2026-09-30. ' + 'x' * 131072, policies)
        self.assertEqual(result['coupon_keywords'], ['retention-coupon'])
