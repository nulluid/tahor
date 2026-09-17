import email
import json
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import draft_replies


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = patch.object(draft_replies.tahor_db, 'DB_PATH', self.root / 'db.sqlite')
        self.db.start()
        self.addCleanup(self.db.stop)
        draft_replies.tahor_db.init_db()
        self.config = patch.object(draft_replies.config, 'email_address', return_value='owner@example.com')
        self.config.start()
        self.addCleanup(self.config.stop)
        self.rule = {'id': 'a'*16, 'match_type': 'sender_email', 'match': 'person@example.com',
                     'name': 'Test', 'instructions': 'Answer helpfully.', 'signature': 'Best,\nExample Owner',
                     'max_sentences': 3, 'excluded_senders': [], 'start_at': '1970-01-01T00:00:00+00:00'}
        self.rules = patch.object(draft_replies.reply_rules, 'get_rules', return_value=[self.rule])
        self.rules.start()
        self.addCleanup(self.rules.stop)
        self.recipient = patch.object(draft_replies, 'reply_recipient', return_value='person@example.com')
        self.recipient.start()
        self.addCleanup(self.recipient.stop)
        self.grace = patch.object(draft_replies.mailbox_settings, 'get_inbox_grace_days', return_value={'read': 3, 'unread': 7})
        self.grace.start()
        self.addCleanup(self.grace.stop)
        self.metadata = ('42 (UID 42 FLAGS (\\Seen) INTERNALDATE "' + datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M:%S %z') + '")').encode()
        self.conn = Mock()
        self.raw = b'From: person@example.com\r\nTo: owner@example.com\r\nMessage-ID: <one@example.com>\r\nSubject: A question\r\n\r\nCan we meet?'
        self.conn.uid.side_effect = self.command

    def command(self, command, *args):
        if command == 'SEARCH':
            return 'OK', [b'42']
        if command == 'FETCH':
            return 'OK', [(self.metadata, self.raw)]
        return 'OK', []

    def test_append_failure_keeps_original_retryable_and_reuses_generated_body(self):
        with patch.object(draft_replies, 'draft_exists', return_value=False), patch.object(draft_replies, 'draft_reply_body', return_value='Thursday works.') as generate:
            self.conn.append.return_value = ('NO', [])
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            row = draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')
            self.assertEqual(row['status'], 'preparing')
            self.assertFalse(any(call.args[0] == 'STORE' and call.args[2] == '-FLAGS.SILENT' for call in self.conn.uid.call_args_list))
            self.conn.append.return_value = ('OK', [])
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_called_once()
        self.assertEqual(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')['status'], 'pending')
        with patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            generate.assert_not_called()
        raw = self.conn.append.call_args.args[-1]
        message = email.message_from_bytes(raw)
        self.assertEqual(message['In-Reply-To'], '<one@example.com>')
        self.assertIn('tahor-draft-', message['Message-ID'])

    def test_lost_append_response_does_not_create_second_draft(self):
        draft_replies.tahor_db.prepare_reply_draft('<one@example.com>', '<one@example.com>', 'person@example.com', 'A question', 'Thursday works.', 'test')
        with patch.object(draft_replies, 'draft_exists', return_value=True), patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_not_called()
            self.conn.append.assert_not_called()

    def test_confirmed_draft_leaves_source_unread_without_sending(self):
        self.conn.append.return_value = ('OK', [])
        with patch.object(draft_replies, 'draft_exists', return_value=False), patch.object(draft_replies, 'draft_reply_body', return_value='Thank you.'):
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
        self.conn.uid.assert_any_call('STORE', b'42', '-FLAGS.SILENT', '(\\Seen)')
        self.assertEqual(self.conn.append.call_args.args[:2], ('Drafts', '\\Draft'))
        self.assertFalse(any(call.args[0] in ('MOVE', 'COPY', 'EXPUNGE') for call in self.conn.uid.call_args_list))

    def test_excluded_sender_and_expired_source_are_not_drafted(self):
        with patch.object(draft_replies, 'draft_reply_body') as generate:
            self.rule['excluded_senders'] = ['person@example.com']
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            self.rule['excluded_senders'] = []
            self.metadata = b'42 (FLAGS () INTERNALDATE "01-Jan-2000 12:00:00 +0000")'
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            generate.assert_not_called()
        self.conn.append.assert_not_called()

    def test_optout_during_generation_prevents_append(self):
        def generate(*args, **kwargs):
            self.rule['excluded_senders'] = ['person@example.com']
            return 'Thank you.'
        with patch.object(draft_replies, 'draft_reply_body', side_effect=generate):
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()

    def test_followup_replies_to_incoming_message_not_thread_root(self):
        self.raw = self.raw.replace(b'Message-ID: <one@example.com>', b'Message-ID: <followup@example.com>\r\nReferences: <root@example.com> <one@example.com>')
        self.conn.append.return_value = ('OK', [])
        with patch.object(draft_replies, 'draft_exists', return_value=False), patch.object(draft_replies, 'draft_reply_body', return_value='Thank you.'):
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
        message = email.message_from_bytes(self.conn.append.call_args.args[-1])
        self.assertEqual(message['In-Reply-To'], '<followup@example.com>')
        self.assertEqual(message['References'], '<root@example.com> <one@example.com> <followup@example.com>')

    def test_failed_draft_search_cannot_append_duplicate(self):
        with patch.object(draft_replies, 'draft_exists', side_effect=RuntimeError('Mailbox unavailable')), patch.object(draft_replies, 'draft_reply_body', return_value='Thank you.'):
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()
        self.assertEqual(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')['status'], 'preparing')

    def test_invalid_model_outputs_rejected(self):
        for value in ('NO_REPLY_NEEDED', '{"sentences": []}', '{"sentences": ["One. Two."]}',
                      '{"sentences": ["One.", "Two.", "Three.", "Four."]}'):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                draft_replies.validate_draft(value, 3)
        self.assertEqual(draft_replies.validate_draft('{"sentences": ["Thank you for the update.", "I appreciate the project details."]}', 3),
                         'Thank you for the update. I appreciate the project details.')

    def test_model_body_and_signature_are_separate_and_malformed_output_retried(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read1.side_effect = [
            json.dumps({'choices': [{'message': {'content': 'NO_REPLY_NEEDED'}}]}).encode(), b'',
            json.dumps({'choices': [{'message': {'content': json.dumps({'sentences': ['Thank you for the update.', 'I appreciate the volunteer report.', 'May your week go well.']})}}]}).encode(), b'',
            json.dumps({'choices': [{'message': {'content': json.dumps({'approved': True, 'issues': [], 'needs_attention': False})}}]}).encode(), b'']
        with patch.dict(draft_replies.os.environ, {'OPENROUTER_API_KEY': 'test-only'}), patch.object(draft_replies.mailbox_settings, 'get_reply_model', return_value='nemotron-free'), patch.object(draft_replies.urllib.request, 'urlopen', return_value=response) as request:
            body = draft_replies.draft_reply_body('Update', 'person@example.com', 'Here is the latest volunteer report.', self.rule)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(body, 'Thank you for the update. I appreciate the volunteer report. May your week go well.\n\nBest,\nExample Owner')

    def prose_test(self, responses, verification=None):
        with patch.dict(draft_replies.os.environ, {'OPENROUTER_API_KEY': 'test-only'}), patch.object(draft_replies.mailbox_settings, 'get_reply_model', return_value='nemotron-free'), patch.object(draft_replies, 'reply_completion', side_effect=[json.dumps(r) for r in responses]) as completion:
            body = draft_replies.draft_reply_body('A question', 'person@example.com', 'Can you attend? Please confirm your availability.', self.rule, verification=verification)
        return body, completion

    def test_prose_rejection_regenerates_once_with_critique_then_verifies(self):
        responses = [{'sentences': ['I will attend.']},
                     {'approved': False, 'issues': ['Do not invent the owner’s availability.'], 'needs_attention': True},
                     {'sentences': ['[Please add your availability].']},
                     {'approved': True, 'issues': [], 'needs_attention': True}]
        verification = {}
        body, completion = self.prose_test(responses, verification)
        self.assertEqual(body, '[Please add your availability].\n\nBest,\nExample Owner')
        self.assertEqual(completion.call_count, 4)
        self.assertEqual(verification, {'needs_attention': True})
        correction = json.loads(completion.call_args_list[2].args[2]['messages'][-1]['content'])
        self.assertIn('availability', correction['issues'][0])
        verifier_input = json.loads(completion.call_args_list[-1].args[2]['messages'][1]['content'])
        self.assertEqual(verifier_input['candidate_reply'], '[Please add your availability].')
        self.assertNotIn('Example Owner', verifier_input['candidate_reply'])

    def test_second_prose_rejection_fails_closed(self):
        responses = [{'sentences': ['I will attend.']}, {'approved': False, 'issues': ['Unsupported commitment.'], 'needs_attention': True}]*2
        with self.assertRaisesRegex(ValueError, 'verification'):
            self.prose_test(responses)

    def test_malformed_verifier_never_approves_a_reply(self):
        for verdict in ({'approved': 'true', 'issues': [], 'needs_attention': False},
                        {'approved': True, 'issues': ['Unresolved problem.'], 'needs_attention': False},
                        {'approved': True, 'issues': []},
                        {'approved': False, 'issues': [], 'needs_attention': True},
                        {'approved': True, 'issues': [], 'needs_attention': False, 'extra': 'field'}):
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                self.prose_test([{'sentences': ['Thank you.']}, verdict])

    def test_prose_failure_prevents_append_and_leaves_source_retryable(self):
        with patch.object(draft_replies, 'draft_reply_body', side_effect=ValueError('Reply failed verification')):
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()
        self.assertIsNone(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>'))
        self.assertFalse(any(c.args[0] == 'STORE' and 'draft-created' in c.args[-1] for c in self.conn.uid.call_args_list))

    def test_personal_question_attention_survives_append_retry_without_blanket_newsletter_hold(self):
        for attention in (True, False):
            with self.subTest(attention=attention):
                database = draft_replies.tahor_db.get_db()
                with database:
                    database.execute('DELETE FROM reply_drafts')
                database.close()
                self.conn.reset_mock()
                def generate(*args, **kwargs):
                    kwargs['verification']['needs_attention'] = attention
                    return 'Thank you.'
                self.conn.append.return_value = ('NO', [])
                with patch.object(draft_replies, 'draft_exists', return_value=None), patch.object(draft_replies, 'draft_reply_body', side_effect=generate) as model:
                    self.assertEqual(draft_replies.process_new_mail(self.conn), [])
                    saved = draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')
                    self.assertEqual(json.loads(saved['trigger_reason'])['needs_attention'], attention)
                    self.conn.reset_mock()
                    self.conn.append.return_value = ('OK', [])
                    self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
                    model.assert_called_once()
                holds = [call for call in self.conn.uid.call_args_list if call.args[0] == 'STORE' and call.args[-1] == '(needs-attention)']
                self.assertEqual(bool(holds), attention)

    def test_legacy_prepared_body_is_regenerated_after_proving_absent(self):
        draft_replies.tahor_db.prepare_reply_draft('<one@example.com>', '<one@example.com>', 'person@example.com', 'Question', 'Unverified old text.', 'legacy')
        self.conn.append.return_value = ('OK', [])
        with patch.object(draft_replies, 'draft_exists', return_value=None), patch.object(draft_replies, 'draft_reply_body', return_value='Verified replacement.') as model:
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            model.assert_called_once()
        self.assertEqual(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')['draft_body'], 'Verified replacement.')

    def test_read_and_unread_windows_use_server_delivery_date(self):
        now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        for age, seen, expected in ((2, True, True), (4, True, False), (6, False, True), (8, False, False)):
            metadata = ('FLAGS (' + ('\\Seen' if seen else '') + ') INTERNALDATE "' + (now-timedelta(days=age)).strftime('%d-%b-%Y %H:%M:%S %z') + '"').encode()
            with self.subTest(age=age, seen=seen):
                self.assertEqual(draft_replies.within_inbox_window(metadata, self.rule, now), expected)
        with self.assertRaises(ValueError):
            draft_replies.within_inbox_window(b'FLAGS ()', self.rule, now)

    def test_sent_reply_after_lost_append_is_not_duplicated_or_marked_unread(self):
        draft_replies.tahor_db.prepare_reply_draft('<one@example.com>', '<one@example.com>', 'person@example.com', 'A question', 'Existing draft.', 'test')
        with patch.object(draft_replies, 'draft_exists', return_value='Sent'), patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
            generate.assert_not_called()
        self.conn.append.assert_not_called()
        self.assertFalse(any(call.args[0] == 'STORE' and call.args[2] == '-FLAGS.SILENT' for call in self.conn.uid.call_args_list))
        self.assertEqual(draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')['status'], 'pending')

    def test_rule_edit_during_generation_defers_append_then_regenerates(self):
        self.rule['revision'] = 'old'
        def generate(*args, **kwargs):
            self.rule['revision'] = 'new'
            return 'Old instructions.'
        with patch.object(draft_replies, 'draft_reply_body', side_effect=generate):
            self.assertEqual(draft_replies.process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()
        self.conn.append.return_value = ('OK', [])
        with patch.object(draft_replies, 'draft_exists', return_value=None), patch.object(draft_replies, 'draft_reply_body', return_value='New instructions.') as generate:
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_called_once()
        saved = draft_replies.tahor_db.get_reply_draft_for_thread('<one@example.com>')
        self.assertEqual(saved['draft_body'], 'New instructions.')
        self.assertEqual(json.loads(saved['trigger_reason'])['rule_revision'], 'new')

    def test_rule_revision_change_does_not_replace_existing_draft(self):
        self.rule['revision'] = 'new'
        draft_replies.tahor_db.prepare_reply_draft('<one@example.com>', '<one@example.com>', 'person@example.com', 'A question', 'Old but already appended.', json.dumps({'rule_id': self.rule['id'], 'rule_revision': 'old'}))
        with patch.object(draft_replies, 'draft_exists', return_value='Drafts'), patch.object(draft_replies, 'draft_reply_body') as generate:
            self.assertEqual(len(draft_replies.process_new_mail(self.conn)), 1)
            generate.assert_not_called()
        self.conn.append.assert_not_called()

    def test_source_changed_or_expired_during_generation_prevents_append(self):
        cases = ('expired', 'uid', 'message_id', 'deleted', 'missing')
        original_metadata, original_raw = self.metadata, self.raw
        for case in cases:
            with self.subTest(case=case):
                self.metadata, self.raw = original_metadata, original_raw
                self.conn.reset_mock()
                database = draft_replies.tahor_db.get_db()
                with database:
                    database.execute('DELETE FROM reply_drafts')
                database.close()
                def generate(*args, **kwargs):
                    if case == 'expired':
                        self.metadata = b'42 (UID 42 FLAGS () INTERNALDATE "01-Jan-2000 12:00:00 +0000")'
                    elif case == 'uid':
                        self.metadata = self.metadata.replace(b'UID 42', b'UID 99')
                    elif case == 'message_id':
                        self.raw = self.raw.replace(b'<one@example.com>', b'<other@example.com>')
                    elif case == 'deleted':
                        self.metadata = self.metadata.replace(b'FLAGS (', b'FLAGS (\\Deleted ')
                    elif case == 'missing':
                        self.conn.uid.side_effect = lambda *args: ('OK', [None])
                    return 'Thank you.'
                self.conn.uid.side_effect = self.command
                with patch.object(draft_replies, 'draft_reply_body', side_effect=generate):
                    self.assertEqual(draft_replies.process_new_mail(self.conn), [])
                self.conn.append.assert_not_called()

    def test_reconciliation_requires_exact_header_and_checks_sent_first(self):
        connection = Mock()
        connection.select.return_value = ('OK', [])
        connection.uid.side_effect = [('OK', [b'9']), ('OK', [(b'1 (UID 9)', b'Message-ID: <prefix-id@example.com>\r\n')]),
                                      ('OK', [b'10']), ('OK', [(b'1 (UID 10)', b'Message-ID: <id@example.com>\r\n')])]
        with patch.object(draft_replies.fetch_batch, 'connect', return_value=connection):
            self.assertEqual(draft_replies.draft_exists('<id@example.com>'), 'Drafts')
        self.assertEqual([call.args[0] for call in connection.select.call_args_list], ['"Sent"', '"Drafts"'])
        connection.logout.assert_called_once()
        connection.uid.side_effect = [('OK', [b'9']), ('OK', [(b'1 (UID 9)', b'Message-ID: <id@example.com>\r\n')])]
        with patch.object(draft_replies.fetch_batch, 'connect', return_value=connection):
            self.assertEqual(draft_replies.draft_exists('<id@example.com>'), 'Sent')

    def test_reconciliation_unavailable_sent_folder_is_not_absence(self):
        connection = Mock()
        connection.select.return_value = ('NO', [])
        with patch.object(draft_replies.fetch_batch, 'connect', return_value=connection), self.assertRaises(RuntimeError):
            draft_replies.draft_exists('<id@example.com>')
        connection.logout.assert_called_once()

    def test_semantic_rule_edit_requires_current_reclassification_before_drafting(self):
        self.rule.update(match_type='natural_language', match='Community project updates', revision='old')
        original_scan = draft_replies.reply_rules.scan_keyword(self.rule)
        match_keyword = draft_replies.reply_rules.keyword(self.rule)
        self.rule['revision'] = 'new'
        current_scan = draft_replies.reply_rules.scan_keyword(self.rule)
        available = {match_keyword, original_scan}
        def command(operation, *args):
            if operation == 'SEARCH':
                required = {args[i+1] for i, word in enumerate(args[:-1]) if word == 'KEYWORD'}
                return 'OK', [b'42' if required.issubset(available) else b'']
            if operation == 'FETCH':
                metadata = self.metadata.replace(b'FLAGS (', ('FLAGS ('+' '.join(sorted(available))+' ').encode())
                return 'OK', [(metadata, self.raw)]
            return 'OK', []
        self.conn.uid.side_effect = command
        self.conn.append.return_value = ('OK', [])
        with patch.object(draft_replies, 'draft_reply_body', return_value='Thank you for the update.') as generate, patch.object(draft_replies, 'draft_exists', return_value=None):
            self.assertEqual(draft_replies._process_new_mail(self.conn), [])
            generate.assert_not_called()
            self.conn.append.assert_not_called()
            available.add(current_scan)
            self.assertEqual(len(draft_replies._process_new_mail(self.conn)), 1)
            generate.assert_called_once()
        self.assertEqual(self.conn.append.call_count, 1)

    def test_semantic_match_removed_during_generation_prevents_append(self):
        self.rule.update(match_type='natural_language', match='Community project updates', revision='current')
        markers = (draft_replies.reply_rules.keyword(self.rule)+' '+draft_replies.reply_rules.scan_keyword(self.rule)).encode()
        self.metadata = self.metadata.replace(b'FLAGS (', b'FLAGS ('+markers+b' ')
        def generate(*args, **kwargs):
            self.metadata = self.metadata.replace(markers, b'')
            return 'Thank you for the update.'
        with patch.object(draft_replies, 'draft_reply_body', side_effect=generate), patch.object(draft_replies, 'draft_exists', return_value=None):
            self.assertEqual(draft_replies._process_new_mail(self.conn), [])
        self.conn.append.assert_not_called()

    def test_watcher_never_logs_provider_exception_content(self):
        import io
        output = io.StringIO()
        with patch.object(draft_replies.fetch_batch, 'connect', side_effect=RuntimeError('private-message-content')), patch.object(draft_replies.time, 'sleep', side_effect=KeyboardInterrupt), patch('sys.stdout', output):
            with self.assertRaises(KeyboardInterrupt):
                draft_replies.watch_forever()
        self.assertNotIn('private-message-content', output.getvalue())
        self.assertIn('reconnecting', output.getvalue())

    def test_idle_drains_multiple_untagged_events(self):
        conn = Mock()
        conn._new_tag.return_value = b'A1'
        conn.tagged_commands = {b'A1': None}
        conn.sock.pending.return_value = 0
        events = iter([None, b'* 2 EXISTS', b'* 1 EXPUNGE', b'A1 OK done'])
        def response():
            event = next(events)
            if event == b'A1 OK done':
                conn.tagged_commands[b'A1'] = ('OK', [])
            return event
        conn._get_response.side_effect = response
        with patch.object(draft_replies.select, 'select', return_value=([conn.sock], [], [])):
            self.assertTrue(draft_replies.wait_for_new_mail(conn))
        self.assertEqual(conn._get_response.call_count, 4)
        self.assertEqual(conn.tagged_commands, {})


if __name__ == '__main__':
    unittest.main()
