import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backlog_worker
import fetch_batch
import keyword_tool


class WorkerFetchTests(unittest.TestCase):
    def test_message_without_id_uses_mailbox_uid_identity(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.response.return_value = ('UIDVALIDITY', [b'100'])
        headers = b'From: person@example.com\r\nSubject: No identifier\r\nDate: Wed, 16 Sep 2026 12:00:00 +0000\r\n\r\n'
        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [b'42']
            return 'OK', [(b'1 (UID 42 INTERNALDATE "16-Sep-2026 12:00:00 +0000" FLAGS ()', headers), (b'BODY[]', b'Hello')]
        conn.uid.side_effect = uid
        with tempfile.TemporaryDirectory() as directory, patch.object(fetch_batch, 'connect', return_value=conn), patch.object(fetch_batch.config, 'email_address', return_value='owner@example.com'):
            prefix = str(Path(directory) / 'batch')
            records = backlog_worker.fetch('INBOX', prefix)
            expected = fetch_batch.local_message_id('INBOX', '100', '42')
            self.assertEqual(records[0]['id'], expected)
            self.assertNotEqual(expected, fetch_batch.local_message_id('INBOX', '101', '42'))
            envelope = json.loads(Path(prefix + '_env.json').read_text())[0]
            self.assertEqual(envelope['uid'], '42')
            self.assertEqual(envelope['uidvalidity'], '100')
        conn.search.assert_not_called()
        conn.fetch.assert_not_called()
        conn.logout.assert_called_once()

    def test_mailbox_uid_reset_cannot_tag_another_message(self):
        conn = Mock()
        conn.select.return_value = ('OK', [])
        conn.response.return_value = ('UIDVALIDITY', [b'101'])
        with patch.object(keyword_tool, 'connect', return_value=conn):
            result = keyword_tool.apply_ops([dict(mailbox='INBOX', uid='42', uidvalidity='100', message_id='local-id', add=['retention-standard'])])
        self.assertEqual(result['failed'], {'local-id'})
        conn.uid.assert_not_called()
