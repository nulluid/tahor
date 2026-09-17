"""Backfill only adds classification/protection and defers mailbox-native drafting."""
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import reply_backfill
import reply_rules


class ReplyBackfillTests(unittest.TestCase):
    def setUp(self):
        self.rule = dict(id='a'*16, revision='b'*32, match_type='natural_language', match='Community updates', excluded_senders=[])
        self.rules = patch.object(reply_rules, 'get_rules', return_value=[self.rule])
        self.get_rules = self.rules.start()
        self.addCleanup(self.rules.stop)
        self.grace = patch.object(reply_backfill.mailbox_settings, 'get_inbox_grace_days', return_value={'read': 3, 'unread': 7})
        self.grace.start()
        self.addCleanup(self.grace.stop)
        self.conn = Mock()
        self.conn.uid.side_effect = self.command
        self.flags = patch.object(reply_backfill, 'store_flags', return_value=True)
        self.store = self.flags.start()
        self.addCleanup(self.flags.stop)
        self.records = patch.object(reply_backfill.tahor_db, 'record_reply_rule_match')
        self.record = self.records.start()
        self.addCleanup(self.records.stop)

    def command(self, command, *args):
        if command == 'SEARCH':
            return 'OK', [b'1 2 3']
        if command == 'FETCH':
            uid = args[0]
            metadata = b'1 (UID '+uid+b' FLAGS () INTERNALDATE "'+datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M:%S %z').encode()+b'")'
            raw = b'From: person@example.org\r\nMessage-ID: <'+uid+b'@example.org>\r\nSubject: Community update\r\n\r\nPlease help with the clinic.'
            return 'OK', [(metadata, raw)]
        return 'OK', []

    def result(self, record, matches=(), uncertain=(), **override):
        result = dict(id=record['id'], action='keep', reply_rule_matches=list(matches), reply_rule_uncertain=list(uncertain), reply_rule_versions={self.rule['id']: self.rule['revision']})
        result.update(override)
        return result

    def test_bounded_scan_uses_recent_inbox_and_never_moves_deletes_or_marks_read(self):
        def classify(records):
            self.assertEqual([r['id'] for r in records], ['reply-scan-3', 'reply-scan-2'])
            return [self.result(r, matches=[self.rule['id']]) for r in records]
        with patch.object(reply_backfill, 'classify_records', side_effect=classify):
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn, limit=2), 2)
        self.assertTrue(all(c.args[0] in ('SEARCH', 'FETCH') for c in self.conn.uid.call_args_list))
        self.assertEqual({c.args[1] for c in self.conn.uid.call_args_list if c.args[0] == 'FETCH'}, {b'2', b'3'})
        self.assertIn('SINCE', self.conn.uid.call_args_list[0].args)
        self.assertIn(reply_rules.scan_keyword(self.rule), self.conn.uid.call_args_list[0].args)
        for call in self.store.call_args_list:
            flags = call.args[2]
            self.assertNotIn('\\Seen', flags)
            self.assertNotIn('\\Deleted', flags)
        self.assertIn('reply-protected', self.store.call_args_list[0].args[2])
        self.assertIn('delete-pending', self.store.call_args_list[1].args[2])

    def test_no_natural_language_rules_means_no_backfill_api_calls(self):
        self.rule['match_type'] = 'sender_email'
        with patch.object(reply_backfill, 'classify_records') as classify:
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn), 0)
        classify.assert_not_called()
        self.conn.uid.assert_not_called()

    def test_classification_errors_and_stale_versions_remain_retryable(self):
        def classify(records):
            return [self.result(records[0], action='error'), self.result(records[1], reply_rule_versions={}), self.result(records[2])]
        with patch.object(reply_backfill, 'classify_records', side_effect=classify):
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn), 1)
        self.assertEqual(self.store.call_args_list[0].args[1], b'1')
        self.assertEqual(self.store.call_count, 2)

    def test_search_and_protection_failure_are_not_reported_as_success(self):
        self.conn.uid.side_effect = [('NO', [])]
        with self.assertRaises(RuntimeError):
            reply_backfill.refresh_recent_matches(self.conn)
        self.conn.uid.side_effect = self.command
        self.store.return_value = False
        with patch.object(reply_backfill, 'classify_records', side_effect=lambda records: [self.result(records[0], matches=[self.rule['id']])]), self.assertRaises(RuntimeError):
            reply_backfill.refresh_recent_matches(self.conn)
        self.record.assert_not_called()

    def test_settings_change_during_classification_prevents_application(self):
        def classify(records):
            result = self.result(records[0], matches=[self.rule['id']])
            self.get_rules.return_value = [dict(self.rule, revision='c'*32)]
            return [result]
        with patch.object(reply_backfill, 'classify_records', side_effect=classify):
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn), 0)
        self.store.assert_not_called()

    def test_failed_cleanup_does_not_mark_revision_complete(self):
        self.store.side_effect = [True, False]
        with patch.object(reply_backfill, 'classify_records', side_effect=lambda records: [self.result(records[0], matches=[self.rule['id']])]), self.assertRaises(RuntimeError):
            reply_backfill.refresh_recent_matches(self.conn)
        for call in self.store.call_args_list:
            self.assertNotIn(reply_rules.scan_keyword(self.rule), call.args[2])
        self.record.assert_not_called()

    def test_confident_nonmatch_removes_old_match_before_completing_scan(self):
        with patch.object(reply_backfill, 'classify_records', side_effect=lambda records: [self.result(records[0])]):
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn), 1)
        self.assertEqual(self.store.call_args_list[0].args[2:], ([reply_rules.keyword(self.rule)], '-'))
        self.assertEqual(self.store.call_args_list[1].args[2:], ([reply_rules.scan_keyword(self.rule)], '+'))

    def test_uncertain_draft_match_does_not_create_retention_decision(self):
        with patch.object(reply_backfill, 'classify_records', side_effect=lambda records: [self.result(records[0], uncertain=[self.rule['id']])]), patch.object(reply_backfill.tahor_db, 'queue_message_review') as review:
            self.assertEqual(reply_backfill.refresh_recent_matches(self.conn), 1)
        review.assert_not_called()
        additions = [flag for call in self.store.call_args_list if call.args[3] == '+' for flag in call.args[2]]
        self.assertIn('reply-protected', additions)
        self.assertNotIn('retention-pending-review', additions)
        self.assertNotIn('needs-attention', additions)
        self.assertNotIn(reply_rules.keyword(self.rule), additions)
