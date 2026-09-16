import importlib.util
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
            self.assertEqual(sum('retention-transient' in op['add'] for op in ops), 2)
            self.assertEqual(sum('retention-forever' in op['add'] for op in ops), 1)
            with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(process_batch.tahor_db, 'get_sender_rule', return_value=None), patch.object(process_batch.tahor_db, 'has_sender_sample', return_value=True):
                process_batch.main()
            later_ops = json.loads(Path(prefix + '_ops.json').read_text())
            self.assertEqual(sum('retention-transient' in op['add'] for op in later_ops), 3)
            self.assertFalse(any('sample_sender' in op for op in later_ops))


if __name__ == '__main__':
    unittest.main()
