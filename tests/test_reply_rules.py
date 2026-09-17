"""Deterministic reply-rule policy and destination validation checks."""
import email
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import reply_rules
import reply_address
import mailbox_settings


class ReplyRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = patch.object(mailbox_settings, 'SETTINGS_PATH', Path(self.temp.name) / 'settings.json')
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def create(self, **kwargs):
        args = dict(name='Community updates', match_type='natural_language', match='Updates from community volunteers',
                    instructions='Thank the author and mention the most urgent request.', signature='Regards,\nExample Owner')
        args.update(kwargs)
        return reply_rules.save_rule(**args)

    def test_rule_edit_preserves_exclusions_and_initial_window(self):
        identifier = self.create()
        original = reply_rules.get_rules()[0]
        reply_rules.set_sender_excluded(identifier, 'PERSON@example.com', True)
        self.create(rule_id=identifier, instructions='Answer personal questions thoughtfully.', history_days=0)
        updated = reply_rules.get_rules()[0]
        self.assertEqual(updated['excluded_senders'], ['person@example.com'])
        self.assertEqual(updated['start_at'], original['start_at'])
        self.assertNotEqual(updated['revision'], original['revision'])
        reply_rules.set_enabled(identifier, False)
        self.assertEqual(reply_rules.get_rules(), [])
        self.assertEqual(len(reply_rules.get_rules(False)), 1)

    def test_optout_is_specific_to_rule_and_reversible(self):
        first, second = self.create(), self.create(name='Personal questions')
        reply_rules.set_sender_excluded(first, 'person@example.com', True)
        rules = {r['id']: r for r in reply_rules.get_rules()}
        self.assertEqual(rules[second]['excluded_senders'], [])
        reply_rules.set_sender_excluded(first, 'person@example.com', False)
        self.assertEqual(reply_rules.get_rules()[0]['excluded_senders'], [])

    def test_malformed_configuration_rejected(self):
        for overrides in ({'match_type': 'arbitrary'}, {'match_type': 'sender_email', 'match': 'name@example.com\r\nBcc: x@y.org'},
                          {'match_type': 'sender_domain', 'match': '-bad.example'}, {'max_sentences': 4}, {'instructions': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create(**overrides)

    def test_semantic_response_requires_valid_disjoint_explicit_arrays(self):
        identifier = self.create()
        rules = reply_rules.get_rules()
        for parsed in ({}, {'reply_rule_matches': [identifier]},
                       {'reply_rule_matches': ['unknown'], 'reply_rule_uncertain': []},
                       {'reply_rule_matches': [identifier, identifier], 'reply_rule_uncertain': []},
                       {'reply_rule_matches': [identifier], 'reply_rule_uncertain': [identifier]}):
            with self.subTest(parsed=parsed), self.assertRaises(ValueError):
                reply_rules.classification_matches(parsed, rules, 'person@example.com')
        self.assertEqual(reply_rules.classification_matches({'reply_rule_matches': [], 'reply_rule_uncertain': [identifier]}, rules, 'person@example.com'), ([], [identifier]))

    def test_explicit_sender_matching_does_not_use_substring(self):
        self.create(match_type='sender_domain', match='example.com')
        rule = reply_rules.get_rules()[0]
        self.assertTrue(reply_rules.sender_matches(rule, 'NAME@EXAMPLE.COM'))
        self.assertFalse(reply_rules.sender_matches(rule, 'name@notexample.com'))
        self.assertFalse(reply_rules.sender_matches(rule, 'name@example.com.evil.org'))
        self.assertEqual(reply_rules.classification_matches({}, [rule], 'name@example.com'), ([rule['id']], []))


class ReplyAddressTests(unittest.TestCase):
    def recipient(self, headers):
        message = email.message_from_string(headers+'\n\nNewsletter')
        return reply_address.reply_recipient(message, 'owner@example.com', check_dns=False)

    def test_reply_to_preferred_even_for_bulk_newsletter(self):
        self.assertEqual(self.recipient('From: no-reply@example.org\nReply-To: Volunteer <person@example.org>\nPrecedence: bulk\nList-ID: updates.example.org'), 'person@example.org')

    def test_invalid_reply_to_does_not_fall_back_to_from(self):
        for address in ('not-an-address', 'a@example.org, b@example.org', 'no-reply@example.org', 'owner@example.com'):
            with self.subTest(address=address):
                self.assertIsNone(self.recipient('From: person@example.org\nReply-To: '+address))

    def test_duplicate_reply_to_and_no_reply_from_are_rejected(self):
        self.assertIsNone(self.recipient('From: person@example.org\nReply-To: a@example.org\nReply-To: b@example.org'))
        self.assertIsNone(self.recipient('From: noreply@example.org'))

    def test_inconclusive_dns_is_retryable_not_an_invalid_address(self):
        message = email.message_from_string('From: person@example.org\n\nUpdate')
        with patch.object(reply_address, 'validate_email', return_value=SimpleNamespace(ascii_email='person@example.org', mx=None)), self.assertRaises(RuntimeError):
            reply_address.reply_recipient(message, 'owner@example.com')
        with patch.object(reply_address, 'validate_email', return_value=SimpleNamespace(ascii_email='person@example.org', mx=[(10, 'mx.example.org')])):
            self.assertEqual(reply_address.reply_recipient(message, 'owner@example.com'), 'person@example.org')


class ReplyClassificationApplicationTests(unittest.TestCase):
    def test_reply_uncertainty_protects_mail_without_creating_retention_review(self):
        import process_batch
        import sys
        rule = dict(id='a'*16, match_type='natural_language', match='Community updates', excluded_senders=[])
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'batch')
            inputs = [dict(id=key, subject='Community update', **{'from': 'person@example.org'}, date='2026-09-16T12:00:00+00:00') for key in ('certain', 'uncertain', 'mixed')]
            outputs = [dict(id=key, action='trash', retention='transient', category='marketing',
                            reply_rule_matches=[rule['id']] if key != 'uncertain' else [],
                            reply_rule_uncertain=[rule['id']] if key == 'uncertain' else [],
                            reply_rule_versions={rule['id']: rule['id']}) for key in ('certain', 'uncertain', 'mixed')]
            outputs[2].update(action='mixed', retention='pending-review', needs_attention=True)
            envelopes = [dict(message_id=key, uid=str(i), from_email='person@example.org', subject='Community update',
                              internaldate='16-Sep-2026 12:00:00 +0000') for i, key in enumerate(('certain', 'uncertain', 'mixed'), 1)]
            for suffix, values in [('in', inputs), ('out', outputs), ('env', envelopes)]:
                Path(prefix+'_'+suffix+'.json').write_text(json.dumps(values))
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(process_batch.reply_rules, 'get_rules', return_value=[rule]), patch.object(process_batch.tahor_db, 'get_sender_rule', side_effect=['block_all', 'block_all', None]), patch.object(process_batch.tahor_db, 'record_reply_rule_match'), patch.object(process_batch.tahor_db, 'queue_message_review') as review:
                process_batch.main()
            ops = {row['message_id']: row for row in json.loads(Path(prefix+'_ops.json').read_text())}
            self.assertFalse(any(row.get('delete') for row in ops.values()))
            self.assertIn('reply-protected', ops['certain']['add'])
            self.assertIn('reply-rule-'+rule['id'], ops['certain']['add'])
            self.assertIn('delete-pending', ops['certain']['remove'])
            self.assertIn('retention-standard', ops['certain']['add'])
            self.assertIn('retention-standard', ops['uncertain']['add'])
            self.assertNotIn('retention-pending-review', ops['uncertain']['add'])
            self.assertNotIn('needs-attention', ops['uncertain']['add'])
            self.assertIn('delete-pending', ops['uncertain']['remove'])
            self.assertNotIn('reply-rule-'+rule['id'], ops['uncertain']['add'])
            self.assertIn('retention-standard', ops['mixed']['add'])
            self.assertIn('needs-attention', ops['mixed']['add'])
            self.assertNotIn('retention-pending-review', ops['mixed']['add'])
            review.assert_not_called()

    def test_classifier_missing_semantic_fields_fails_closed_even_for_trash(self):
        import classify
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read1.side_effect = [json.dumps({'choices': [{'message': {'content': '{"action": "trash"}'}}]}).encode(), b'']
        rules = [dict(id='a'*16, match_type='natural_language', match='Community updates')]
        with patch.object(reply_rules, 'get_rules', return_value=rules), patch.object(classify.urllib.request, 'urlopen', return_value=response):
            result = classify.classify_one('https://example.org/classify', {}, 'test', 'Classify', {'id': 'message', 'from': 'person@example.org'}, retries=0)
        self.assertEqual(result['action'], 'error')


class MissingReplyClassificationVersionTests(unittest.TestCase):
    def test_old_classification_is_preserved_then_reclassified_on_next_batch(self):
        import backlog_worker
        import process_batch
        rule = {'id': 'b'*16, 'revision': 'current', 'match_type': 'natural_language', 'match': 'Community updates'}
        record = {'id': '<update@example.org>', 'subject': 'Community update', 'from': 'person@example.org', 'date': '2026-09-16T12:00:00+00:00'}
        old = dict(id=record['id'], action='trash', retention='transient', category='marketing')
        current = dict(old, reply_rule_matches=[rule['id']], reply_rule_uncertain=[], reply_rule_versions={rule['id']: 'current'})
        envelope = {'message_id': record['id'], 'uid': '42', 'from_email': record['from'], 'subject': record['subject'], 'internaldate': '16-Sep-2026 12:00:00 +0000'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root/'current_batch'
            Path(str(prefix)+'_in.json').write_text(json.dumps([record]))
            Path(str(prefix)+'_env.json').write_text(json.dumps([envelope]))
            processed = root/'processed.txt'
            def apply(operations):
                self.assertFalse(any(op.get('delete') for op in operations))
                return {'applied': [op['message_id'] for op in operations]}
            with patch.object(backlog_worker.ai_routing, 'record_results'), patch.object(backlog_worker, 'STATE_DIR', root), patch.object(backlog_worker, 'PROCESSED_IDS_PATH', processed), patch.object(backlog_worker, 'delete_pending_trash'), patch.object(backlog_worker, 'fetch', return_value=[record]) as fetch, patch.object(backlog_worker, 'classify_batch', side_effect=[([], [old]), ([], [current])]) as classify, patch.object(backlog_worker.runtime_status, 'write_status'), patch.object(backlog_worker.mailbox_settings, 'get_classify_mode', return_value='paid'), patch.object(backlog_worker.mailbox_settings, 'decrement_backlog_estimate'), patch.object(backlog_worker.keyword_tool, 'apply_ops', side_effect=apply), patch.object(process_batch.reply_rules, 'get_rules', return_value=[rule]), patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None), patch.object(process_batch.tahor_db, 'record_reply_rule_match'):
                self.assertEqual(backlog_worker.process_one_batch('INBOX'), 'error')
                self.assertEqual(processed.read_text(), '')
                self.assertEqual(json.loads(Path(str(prefix)+'_ops.json').read_text()), [])
                self.assertEqual(backlog_worker.process_one_batch('INBOX'), 'processed_paid')
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(classify.call_count, 2)
                self.assertEqual(processed.read_text().splitlines(), [record['id']])
            operations = json.loads(Path(str(prefix)+'_ops.json').read_text())
            self.assertIn('reply-protected', operations[0]['add'])
            self.assertIn(reply_rules.scan_keyword(rule), operations[0]['add'])
