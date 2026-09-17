from datetime import datetime, timezone
from unittest.mock import Mock, patch
import unittest
import filing_sweep


class ReplyFilingTests(unittest.TestCase):
    def test_reply_filing_respects_separate_read_and_unread_age_and_attention(self):
        rule = {'id':'a'*16, 'filing_folder':'Community/Updates'}
        conn = Mock()
        conn.uid.side_effect = [('OK',[b'1']), ('OK',[b'2'])]
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=[rule]):
            result = filing_sweep.reply_filing_destinations(conn, ('SEEN','BEFORE','14-Sep-2026'), ('UNSEEN','BEFORE','10-Sep-2026'))
        self.assertEqual(result, {b'1':'Community/Updates', b'2':'Community/Updates'})
        for call in conn.uid.call_args_list:
            self.assertIn('BEFORE', call.args)
            self.assertIn('needs-attention', call.args)
            self.assertIn('retention-pending-review', call.args)
        self.assertIn('SEEN',conn.uid.call_args_list[0].args)
        self.assertIn('UNSEEN',conn.uid.call_args_list[1].args)
        self.assertNotIn('reply-protected',conn.uid.call_args_list[1].args)

    def test_no_filing_destination_does_not_change_existing_filing(self):
        conn = Mock()
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=[{'id':'a'*16}]):
            self.assertEqual(filing_sweep.reply_filing_destinations(conn, (), ()), {})
        conn.uid.assert_not_called()

    def test_conflicting_destinations_and_failed_search_preserve_mail(self):
        conn = Mock()
        conn.uid.return_value = ('OK',[b'1'])
        rules = [{'id':'a'*16,'filing_folder':'Community/Updates'}, {'id':'b'*16,'filing_folder':'Different'}]
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=rules):
            with self.assertRaisesRegex(RuntimeError, 'disagree'):
                filing_sweep.reply_filing_destinations(conn, (), ())
        conn.uid.return_value = ('NO',[])
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=rules):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                filing_sweep.reply_filing_destinations(conn, (), ())
        self.assertTrue(all(call.args[0]=='SEARCH' for call in conn.uid.call_args_list))


class ReplyFiledReadStateTests(unittest.TestCase):
    def test_existing_low_attention_reply_matches_mark_read_but_attention_and_stale_matches_stay_unread(self):
        from datetime import timedelta
        rule = {'id': 'a'*16, 'revision': 'new', 'filing_folder': 'Community/Updates'}
        old = dict(rule, revision='old')
        current = {filing_sweep.reply_rules.keyword(rule), filing_sweep.reply_rules.scan_keyword(rule), 'retention-standard', 'category-personal-correspondence'}
        records = {b'1': current, b'2': current | {'needs-attention'}, b'3': current | {'\\Flagged'},
                   b'4': current | {'retention-pending-review'},
                   b'5': current - {'retention-standard'},
                   b'6': current - {filing_sweep.reply_rules.scan_keyword(rule)} | {filing_sweep.reply_rules.scan_keyword(old)},
                   b'7': current}
        conn = Mock()
        conn.select.return_value = ('OK', [])
        def command(operation, *args):
            if operation == 'STORE':
                return 'OK', []
            self.assertEqual(operation, 'SEARCH')
            terms = args[1:]
            def matches(uid, flags):
                tokens = iter(terms)
                def one(token):
                    if token == 'OR':
                        left = one(next(tokens)); right = one(next(tokens))
                        return left or right
                    if token == 'KEYWORD': return next(tokens) in flags
                    if token == 'UNKEYWORD': return next(tokens) not in flags
                    if token == 'UNSEEN': return '\\Seen' not in flags
                    if token == 'UNFLAGGED': return '\\Flagged' not in flags
                    if token == 'BEFORE':
                        cutoff = datetime.strptime(next(tokens), '%d-%b-%Y').replace(tzinfo=timezone.utc)
                        received = datetime.now(timezone.utc)-timedelta(days=1 if uid == b'7' else 20)
                        return received < cutoff
                    self.fail('Unexpected search predicate: '+token)
                return all([one(token) for token in tokens])
            return 'OK', [b' '.join(uid for uid, flags in records.items() if matches(uid, flags))]
        conn.uid.side_effect = command
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=[rule]), patch.object(filing_sweep.mailbox_settings, 'get_inbox_grace_days', return_value={'read':3,'unread':7}):
            self.assertEqual(filing_sweep.mark_filed_read(conn, 'Community/Updates'), 1)
        stores = [call.args for call in conn.uid.call_args_list if call.args[0] == 'STORE']
        self.assertEqual(stores, [('STORE', '1', '+FLAGS.SILENT', '(\\Seen)')])

    def test_reply_routing_and_read_cleanup_require_same_current_revision(self):
        rule = {'id':'a'*16, 'revision':'new', 'filing_folder':'Community/Updates'}
        conn = Mock()
        conn.uid.return_value = ('OK',[b'1'])
        with patch.object(filing_sweep.reply_rules, 'get_rules', return_value=[rule]):
            filing_sweep.reply_filing_destinations(conn, ('SEEN',), ('UNSEEN',))
        for call in conn.uid.call_args_list:
            self.assertIn(filing_sweep.reply_rules.scan_keyword(rule), call.args)
            self.assertIn(filing_sweep.reply_rules.keyword(rule), call.args)
            self.assertIn('UNFLAGGED', call.args)
            self.assertIn('needs-attention', call.args)
