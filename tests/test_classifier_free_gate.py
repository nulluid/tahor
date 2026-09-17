import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import classify
import backlog_worker as worker


class FreeClassifierGateTests(unittest.TestCase):
    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.records = [{'id': str(i)} for i in range(3)]
        self.start_patch(patch.dict(os.environ, {'TAHOR_CLASSIFY_FREE_ENABLED': '0'}))
        self.start_patch(patch.object(worker, '_paid_retry_at', 0))
        self.start_patch(patch.object(worker, '_free_retry_at', 0))
        self.start_patch(patch.object(worker, 'log'))
        self.http = self.start_patch(patch.object(classify.urllib.request, 'urlopen'))

    def assert_pending(self, results, count=3):
        self.assertEqual(len(results), count)
        self.assertTrue(all(r['action'] == 'error' for r in results))
        self.http.assert_not_called()

    def test_direct_free_call_and_backend_skip_http_and_credentials(self):
        backend = classify.BACKENDS['openrouter-free']
        result = classify.classify_one(backend['url'], {}, backend['default_model'], '', self.records[0])
        self.assert_pending([result], 1)
        with patch.object(worker, 'PROMPT_PATH') as prompt:
            self.assert_pending(worker.classify_with_backend(self.records, 'openrouter-free'))
        prompt.read_text.assert_not_called()

    def test_free_mode_never_substitutes_paid_or_records_throughput(self):
        with patch.object(worker, 'classify_with_backend') as backend, patch.object(
                worker.mailbox_settings, 'record_free_batch') as rate:
            free, paid = worker.classify_batch(self.records, 'free')
        self.assert_pending(free)
        self.assertEqual(paid, [])
        backend.assert_not_called()
        rate.assert_not_called()

    def test_paid_credit_failure_and_cooldown_remain_retryable(self):
        with patch.object(worker, 'classify_with_backend', return_value=[
                {'id': r['id'], 'action': 'error', 'http_status': 402} for r in self.records]) as backend:
            free, paid = worker.classify_batch(self.records, 'paid')
            self.assert_pending(paid)
            self.assertEqual(free, [])
            self.assertTrue(all(result['http_status'] == 402 for result in paid))
            free, paid = worker.classify_batch(self.records, 'paid')
            self.assert_pending(paid)
            self.assertEqual(free, [])
            self.assertTrue(all('cooling down' in result['reason'] for result in paid))
        backend.assert_called_once_with(self.records, 'openrouter-paid')

    def test_failed_recovery_probe_preserves_original_status_without_free_requests(self):
        worker._paid_retry_at = 1
        original = {'id': '0', 'action': 'error', 'http_status': 429, 'reason': 'rate limited'}
        with patch.object(worker, 'classify_with_backend', return_value=[original]) as backend:
            free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(free, [])
        self.assert_pending(paid)
        self.assertEqual(paid[0], original)
        self.assertTrue(all('cooling down' in result['reason'] for result in paid[1:]))
        backend.assert_called_once_with(self.records[:1], 'openrouter-paid')

    def test_partial_paid_failures_keep_diagnostics_and_logs_exclude_private_content(self):
        expected = [{'id': '0', 'action': 'keep'},
                    {'id': '1', 'action': 'error', 'http_status': 503, 'reason': 'PRIVATE-SOURCE'},
                    {'id': '2', 'action': 'error', 'reason': 'response deadline PRIVATE-SOURCE'}]
        with patch.object(worker, 'classify_with_backend', return_value=expected), patch.object(
                worker, '_classify_free_and_time') as free_backend, patch.object(worker, 'log') as log:
            free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(free, [])
        self.assertEqual(paid, expected)
        free_backend.assert_not_called()
        logged = ' '.join(str(call.args) for call in log.call_args_list)
        self.assertNotIn('PRIVATE-SOURCE', logged)
        self.assertIn('503', logged)
        self.assertIn('timeout', logged)
        self.assertIn('http_statuses', logged)

    def test_paid_probe_recovers_even_when_free_is_disabled(self):
        worker._paid_retry_at = 1
        with patch.object(worker, 'classify_with_backend', side_effect=lambda records, backend:
                          [{'id': r['id'], 'action': 'keep'} for r in records]) as backend:
            free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(free, [])
        self.assertEqual(len(paid), 3)
        self.assertEqual(worker._paid_retry_at, 0)
        self.assertTrue(all(call.args[1] == 'openrouter-paid' for call in backend.call_args_list))

    def test_auto_uses_paid_when_free_is_explicitly_disabled(self):
        with patch.object(worker, 'get_backlog_estimate', return_value=3), patch.object(
                worker.mailbox_settings, 'recent_free_rate', return_value=1), patch.object(
                worker.mailbox_settings, 'decide_backend_split', return_value=(1, 2)), patch.object(
                worker, 'classify_with_backend', side_effect=lambda records, backend:
                [{'id': r['id'], 'action': 'keep'} for r in records]) as backend:
            free, paid = worker.classify_batch(self.records, 'auto')
        self.assertEqual(free, [])
        self.assertEqual(len(paid), 3)
        backend.assert_called_once_with(self.records, 'openrouter-paid')

    def test_standalone_writes_retry_errors_without_credentials_or_paid_override(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            output = Path(directory) / 'output.json'
            source.write_text(json.dumps(self.records))
            with patch.dict(os.environ, {'CLASSIFY_BACKEND': 'openrouter-free'}), patch.object(
                    classify.sys, 'argv', ['classify.py', str(source), str(output),
                                          'missing-prompt', '--model', 'paid-model']):
                classify.main()
            self.assert_pending(json.loads(output.read_text()))

    def test_default_enabled_and_explicit_zero_disables(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(classify.free_classification_enabled())
        with patch.dict(os.environ, {'TAHOR_CLASSIFY_FREE_ENABLED': '0'}):
            self.assertFalse(classify.free_classification_enabled())
