import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keyword_tool
import process_batch


class PipelineTests(unittest.TestCase):
    def test_classifier_accepts_explicit_brief_retention(self):
        import classify
        answer = dict(action='keep', category='marketing', retention='brief',
                      expense_type='n/a', needs_attention=False, reason='Routine notice')
        response = json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode()
        backend = classify.BACKENDS['openrouter-paid']
        with patch.object(classify, 'wait_for_model_request'), \
             patch('reply_rules.get_rules', return_value=[]), \
             patch.object(classify.urllib.request, 'urlopen', return_value=io.BytesIO(response)):
            result = classify.classify_one(backend['url'], {}, backend['default_model'],
                                           'Use brief only when instructed.', {'id': 'notice'}, retries=0)
        self.assertEqual(result['action'], 'keep')
        self.assertEqual(result['retention'], 'brief')

    def test_brief_keep_is_classified_without_immediate_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'batch')
            inputs = [dict(id='notice', subject='Routine update', **{'from': 'alerts@example.com'}, date='2026-01-01T00:00:00+00:00')]
            outputs = [dict(id='notice', action='keep', category='marketing', retention='brief', needs_attention=False)]
            envelopes = [dict(message_id='notice', uid='10', subject='Routine update', from_email='alerts@example.com', internaldate='01-Jan-2026 00:00:00 +0000')]
            for suffix, rows in [('in', inputs), ('out', outputs), ('env', envelopes)]:
                Path(prefix + '_' + suffix + '.json').write_text(json.dumps(rows))
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), \
                 patch.object(process_batch.reply_rules, 'get_rules', return_value=[]), \
                 patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None), \
                 patch.object(process_batch.tahor_db, 'has_sender_sample', return_value=False):
                process_batch.main()
                ops = json.loads(Path(prefix + '_ops.json').read_text())
                self.assertEqual(ops[0]['add'], ['category-marketing', 'retention-standard', 'retention-short-lived'])
                self.assertFalse(ops[0].get('delete'))
                self.assertNotIn('retention-short-lived', ops[0]['remove'])
                outputs[0]['retention'] = 'forever'
                Path(prefix + '_out.json').write_text(json.dumps(outputs))
                process_batch.main()
                ops = json.loads(Path(prefix + '_ops.json').read_text())
                self.assertIn('retention-forever', ops[0]['add'])
                self.assertIn('retention-short-lived', ops[0]['remove'])

    def test_keyword_failures_are_reported_without_expunge(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.uid.side_effect = [('OK', [b'1 (UID 1)']), ('NO', []), ('OK', [None])]
        ops = [dict(mailbox='INBOX', uid=str(i), message_id=str(i), add=['retention-standard']) for i in (1, 2)]
        with patch.object(keyword_tool, 'connect', return_value=conn):
            result = keyword_tool.apply_ops(ops)
        self.assertEqual(result['failed'], {'1'})
        self.assertEqual(result['missing'], {'2'})
        self.assertFalse(result['applied'])
        conn.close.assert_not_called()
        conn.expunge.assert_not_called()
        conn.logout.assert_called_once()

    def test_trash_is_tagged_then_deleted_by_uid_without_global_expunge(self):
        conn = Mock()
        conn.capabilities = (b'UIDPLUS',)
        conn.select.return_value = ('OK', [])
        conn.uid.side_effect = [('OK', [b'1 (UID 1)']), ('OK', []), ('OK', []), ('OK', [])]
        op = dict(mailbox='INBOX', uid='1', message_id='trash', add=['category-marketing', 'delete-pending'], delete=True)
        with patch.object(keyword_tool, 'connect', return_value=conn):
            result = keyword_tool.apply_ops([op])
        self.assertEqual(result['applied'], {'trash'})
        self.assertEqual([call.args[0] for call in conn.uid.call_args_list], ['FETCH', 'STORE', 'STORE', 'EXPUNGE'])
        self.assertIn('delete-pending', conn.uid.call_args_list[1].args[-1])
        self.assertEqual(conn.uid.call_args_list[-1].args, ('EXPUNGE', '1'))
        conn.expunge.assert_not_called()
        conn.close.assert_not_called()

    def test_failed_trash_deletion_leaves_retry_tag_and_reports_failure(self):
        conn = Mock()
        conn.capabilities = (b'UIDPLUS',)
        conn.select.return_value = ('OK', [])
        conn.uid.side_effect = [('OK', [b'1 (UID 1)']), ('OK', []), ('OK', []), ('NO', []), ('OK', [])]
        op = dict(mailbox='INBOX', uid='1', message_id='trash', add=['delete-pending'], delete=True)
        with patch.object(keyword_tool, 'connect', return_value=conn):
            result = keyword_tool.apply_ops([op])
        self.assertEqual(result['failed'], {'trash'})
        self.assertFalse(result['applied'])
        self.assertEqual(conn.uid.call_args_list[-1].args, ('STORE', '1', '-FLAGS', '(\\Deleted)'))
        self.assertFalse(any('delete-pending' in call.args[-1] and call.args[2] == '-FLAGS' for call in conn.uid.call_args_list if call.args[0] == 'STORE'))

    def test_invalid_keyword_cannot_inject_imap_flags(self):
        conn = Mock()
        with self.assertRaises(ValueError):
            keyword_tool.store_flags(conn, '1', ['retention-transient \\Deleted'], '+')
        conn.uid.assert_not_called()

    def test_trash_has_retention_and_exact_message_id_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'batch')
            inputs = [dict(id=str(i), subject='Same subject', **{'from': 'sender@example.com'}, date='2026-01-01T00:00:00+00:00') for i in range(3)]
            outputs = [dict(id=str(i), action='trash', category='marketing', retention='transient') for i in range(3)]
            envelopes = [dict(message_id=str(i), uid=str(10+i), subject='Same subject', from_email='sender@example.com', internaldate='01-Jan-2026 00:00:00 +0000') for i in range(3)]
            for suffix, rows in [('in', inputs), ('out', outputs), ('env', envelopes)]:
                Path(prefix + '_' + suffix + '.json').write_text(json.dumps(rows))
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None), patch.object(process_batch.tahor_db, 'has_sender_sample', return_value=False):
                mapping = process_batch.main()
            ops = json.loads(Path(prefix + '_ops.json').read_text())
            self.assertEqual(len(ops), 3)
            self.assertEqual(mapping, {'0': '0', '1': '1', '2': '2'})
            self.assertEqual({op['uid'] for op in ops}, {'10', '11', '12'})
            self.assertEqual(sum('retention-transient' in op['add'] for op in ops), 3)
            self.assertTrue(all(op['delete'] and 'delete-pending' in op['add'] for op in ops))
            self.assertFalse(any('retention-forever' in op['add'] for op in ops))
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None), patch.object(process_batch.tahor_db, 'has_sender_sample', return_value=True):
                process_batch.main()
            later_ops = json.loads(Path(prefix + '_ops.json').read_text())
            self.assertEqual(sum('retention-transient' in op['add'] for op in later_ops), 3)
            self.assertFalse(any('sample_sender' in op for op in later_ops))


if __name__ == '__main__':
    unittest.main()
