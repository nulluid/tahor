"""Recovery tests use fake backends; no mailbox or network access."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(os.environ.get('TAHOR_TEST_ROOT', Path(__file__).resolve().parents[1]))

def load(name):
    spec = importlib.util.spec_from_file_location('test_' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

classifier = load('classify')
with patch.dict(sys.modules, {name: Mock() for name in ('fetch_batch', 'process_batch', 'keyword_tool', 'mailbox_settings', 'classify')}):
    worker = load('backlog_worker')
real_classify_with_backend = worker.classify_with_backend


def ok(records):
    return [dict(id=r['id'], action='keep') for r in records]


def errors(records, status=402):
    return [dict(id=r['id'], action='error', http_status=status) for r in records]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        worker._paid_retry_at = 0
        self.records = [dict(id=str(i)) for i in range(3)]
        self.log = patch.object(worker, 'log').start()
        self.backend = patch.object(worker, 'classify_with_backend').start()
        self.free = patch.object(worker, '_classify_free_and_time', side_effect=ok).start()
        self.clock = patch.object(worker.time, 'monotonic', return_value=100).start()
        self.addCleanup(patch.stopall)

    def test_paid_success_does_not_use_free(self):
        self.backend.return_value = ok(self.records)
        free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(free, [])
        self.assertEqual(paid, ok(self.records))
        self.free.assert_not_called()

    def test_credit_failure_falls_back_and_cools_down(self):
        self.backend.return_value = errors(self.records)
        self.assertEqual(worker.classify_batch(self.records, 'paid'), (ok(self.records), []))
        self.assertEqual(worker._paid_retry_at, 400)
        worker.classify_batch(self.records, 'paid')
        self.assertEqual(self.backend.call_count, 1)

    def test_probe_recovers_to_paid(self):
        worker._paid_retry_at = 99
        self.backend.side_effect = lambda records, backend: ok(records)
        free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(free, [])
        self.assertEqual(paid, ok(self.records))
        self.assertEqual(self.backend.call_args_list[0].args[0], self.records[:1])
        self.assertEqual(worker._paid_retry_at, 0)
        self.free.assert_not_called()

    def test_failed_probe_uses_free_and_reschedules(self):
        worker._paid_retry_at = 99
        self.backend.return_value = errors(self.records[:1])
        self.assertEqual(worker.classify_batch(self.records, 'paid'), (ok(self.records), []))
        self.assertEqual(worker._paid_retry_at, 400)
        self.assertEqual(self.backend.call_count, 1)

    def test_partial_failure_retries_only_failed_messages(self):
        self.backend.return_value = ok(self.records[:2]) + errors(self.records[2:])
        free, paid = worker.classify_batch(self.records, 'paid')
        self.free.assert_called_once_with(self.records[2:])
        self.assertEqual(len(free + paid), 3)
        self.assertEqual(len({r['id'] for r in free + paid}), 3)

    def test_free_mode_never_probes_paid(self):
        worker._paid_retry_at = 99
        self.assertEqual(worker.classify_batch(self.records, 'free'), (ok(self.records), []))
        self.backend.assert_not_called()

    def test_both_fail_remain_errors_and_retry_is_bounded(self):
        self.backend.return_value = errors(self.records)
        self.free.side_effect = lambda records: errors(records, 429)
        free, paid = worker.classify_batch(self.records, 'paid')
        self.assertEqual(worker.batch_status(free + paid), 'backend_unavailable')
        self.assertLessEqual(worker.BACKEND_RETRY_SECONDS, 300)
        self.assertEqual({r['id'] for r in free + paid}, {r['id'] for r in self.records})

    def test_any_progress_avoids_worker_wide_backoff(self):
        self.assertEqual(worker.batch_status(ok(self.records[:1]) + errors(self.records[1:])), 'processed')
        self.assertEqual(worker.batch_status(errors(self.records[:1])), 'backend_unavailable')

    def test_auto_keeps_successful_free_results_on_paid_failure(self):
        worker.mailbox_settings.decide_backend_split.return_value = (1, 2)
        worker.mailbox_settings.recent_free_rate.return_value = 1
        with patch.object(worker, 'get_backlog_estimate', return_value=1000):
            self.backend.side_effect = lambda records, backend: errors(records)
            free, paid = worker.classify_batch(self.records, 'auto')
        self.assertEqual(paid, [])
        self.assertEqual(free, ok(self.records))

    def test_failed_messages_are_not_marked_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'current_batch_ops.json').write_text('[]')
            (root / 'current_batch_trash_ids.json').write_text('[]')
            worker.process_batch.main.return_value = {'0': '0'}
            worker.keyword_tool.apply_ops.return_value = {'applied': {'0'}, 'failed': set(), 'missing': set()}
            with patch.object(worker, 'REPO', root), patch.object(worker, 'PROCESSED_IDS_PATH', root / 'processed.txt'), patch.object(worker, 'fetch', return_value=self.records), patch.object(worker, 'classify_batch', return_value=(errors(self.records[1:]), ok(self.records[:1]))), patch.object(worker.os, 'chdir'):
                self.assertEqual(worker.process_one_batch('INBOX'), 'processed')
            self.assertEqual((root / 'processed.txt').read_text(), '0\n')
            results = json.loads((root / 'current_batch_out.json').read_text())
            self.assertEqual(sum(r['action'] == 'error' for r in results), 2)

    def test_transport_exception_becomes_retryable_error(self):
        worker.classify.BACKENDS = {'test': {'auth_header': lambda: 'fake', 'default_concurrency': 1, 'url': 'https://example.invalid', 'default_model': 'test'}}
        with patch.object(worker, 'PROMPT_PATH') as prompt, patch.object(worker.classify, 'classify_one', side_effect=RuntimeError('transport failed')):
            prompt.read_text.return_value = 'test'
            results = real_classify_with_backend(self.records[:1], 'test')
        self.assertEqual(results[0]['action'], 'error')
        self.assertEqual(results[0]['id'], '0')



class ClassifierTests(unittest.TestCase):
    def test_402_returns_status_without_repeating_request(self):
        error = urllib.error.HTTPError('https://example.invalid', 402, 'Payment Required', {}, None)
        with patch.object(classifier.urllib.request, 'urlopen', side_effect=error) as request:
            result = classifier.classify_one('https://example.invalid', {}, 'test', 'test', {'id': '1'})
        self.assertEqual(result['action'], 'error')
        self.assertEqual(result['http_status'], 402)
        self.assertEqual(request.call_count, 1)
        error.close()


if __name__ == '__main__':
    unittest.main()
