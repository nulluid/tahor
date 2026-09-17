import io
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import http_response
import classify
import draft_replies

APP_DIR = Path(__file__).resolve().parents[1] / 'decision-app'
spec = importlib.util.spec_from_file_location('http_rule_apply', APP_DIR / 'apply_decisions.py')
rule_apply = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(APP_DIR)] + sys.path):
    spec.loader.exec_module(rule_apply)


class ResponseBoundTests(unittest.TestCase):
    def test_chunked_response_preserves_exact_bytes(self):
        response = MagicMock()
        response.read1.side_effect = [b'{"a":', b'1}', b'']
        with patch.object(http_response.time, 'monotonic', return_value=1):
            self.assertEqual(http_response.read_bounded(response, deadline=2), b'{"a":1}')
        response.read.assert_not_called()

    def test_dripping_whitespace_stops_at_total_deadline(self):
        response = MagicMock()
        response.read1.return_value = b' '
        with patch.object(http_response.time, 'monotonic', side_effect=[0, 1, 2, 3, 4, 5]):
            with self.assertRaisesRegex(TimeoutError, 'deadline'):
                http_response.read_bounded(response, deadline=5)
        self.assertEqual(response.read1.call_count, 3)

    def test_slow_final_read_cannot_hide_elapsed_deadline(self):
        response = MagicMock()
        response.read1.return_value = b''
        with patch.object(http_response.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                http_response.read_bounded(response, deadline=10)

    def test_size_limit_and_exact_limit(self):
        with patch.object(http_response.time, 'monotonic', return_value=0):
            self.assertEqual(http_response.read_bounded(io.BytesIO(b'abcd'), 10, 4), b'abcd')
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                http_response.read_bounded(io.BytesIO(b'abcde'), 10, 4)

    def test_classifier_timeout_closes_response_and_retains_retryable_error(self):
        response = MagicMock()
        with patch.object(classify.urllib.request, 'urlopen', return_value=response), patch.object(classify, 'read_bounded', side_effect=TimeoutError('Model response deadline exceeded')), patch('reply_rules.get_rules', return_value=[]):
            result = classify.classify_one('https://example.org', {}, 'model', 'prompt', {'id': 'sample'}, retries=1)
        self.assertEqual(result['action'], 'error')
        self.assertIn('deadline', result['reason'])
        self.assertEqual(response.__exit__.call_count, 2)

    def test_draft_timeout_closes_response_without_inventing_a_body(self):
        response = MagicMock()
        with patch.dict(draft_replies.os.environ, {'OPENROUTER_API_KEY': 'synthetic', 'FASTMAIL_EMAIL': 'owner@example.com'}), patch.object(draft_replies.mailbox_settings, 'get_reply_model', return_value='ling-free'), patch.object(draft_replies.urllib.request, 'urlopen', return_value=response), patch.object(draft_replies, 'read_bounded', side_effect=TimeoutError('Model response deadline exceeded')):
            with self.assertRaises(TimeoutError):
                draft_replies._draft_reply_body('Update', 'sender@example.org', 'Private input')
        response.__exit__.assert_called_once()

    def test_rule_writer_dripping_body_hits_deadline_and_closes_response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read1.return_value = b' '
        with patch.dict(rule_apply.os.environ, {'OPENROUTER_API_KEY': 'synthetic'}), patch.object(
                rule_apply.mailbox_settings, 'get_rule_model', return_value='gpt5'), patch.object(
                rule_apply.urllib.request, 'urlopen', return_value=response), patch.object(
                rule_apply.time, 'monotonic', side_effect=[0, 1, 91]):
            with self.assertRaisesRegex(TimeoutError, '^Model response deadline exceeded$'):
                rule_apply.rule_model_call('Synthetic private directions')
        response.read.assert_not_called()
        response.__exit__.assert_called_once()

    def test_rule_writer_oversize_body_is_rejected_and_closed(self):
        response = io.BytesIO(b'x' * (http_response.MODEL_RESPONSE_BYTES + 1))
        with patch.dict(rule_apply.os.environ, {'OPENROUTER_API_KEY': 'synthetic'}), patch.object(
                rule_apply.mailbox_settings, 'get_rule_model', return_value='gpt5'), patch.object(
                rule_apply.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(ValueError, '^Model response exceeds byte limit$'):
                rule_apply.rule_model_call('Synthetic private directions')
        self.assertTrue(response.closed)
