"""Mailbox discovery and scheduling use synthetic IMAP responses only."""
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backlog_worker as worker


class MailboxTests(unittest.TestCase):
    def test_discovers_nested_special_large_and_escaped_names(self):
        conn = Mock()
        conn.untagged_responses = {'UIDNEXT': [b'10000'], 'EXISTS': [b'100']}
        conn.list.return_value = ('OK', [
            b'(\\Noselect) "/" "Parent"',
            b'(\\HasNoChildren) "/" "INBOX"',
            b'() "/" "Imported/INBOX"',
            b'(\\Trash) "/" "Trash"',
            b'(\\Drafts) "/" "Drafts"',
            b'() NIL Sent',
            b'() "/" "A \\"quote\\" and \\\\ slash"',
            b'() "/" "&ZeVnLIqe-"',
            (b'() "/" {11}', b'Folder name'), b'',
            b'() "/" inbox',
        ])
        with patch.object(worker.fetch_batch, 'connect', return_value=conn):
            names = worker.discover_mailboxes()
        self.assertEqual(names[0], 'INBOX')
        self.assertEqual(set(names), {'INBOX', 'Imported/INBOX', 'Trash', 'Drafts', 'Sent', 'A "quote" and \\ slash', '&ZeVnLIqe-', 'Folder name'})
        conn.list.assert_called_once_with('""', '"*"')
        conn.select.assert_not_called()
        conn.logout.assert_called_once()
        self.assertEqual(worker.quote_mailbox('A "quote" and \\ slash'), '"A \\"quote\\" and \\\\ slash"')

    def test_discovery_failure_is_not_an_empty_backlog(self):
        for response in [('NO', []), ('OK', [b'invalid']), ('OK', [])]:
            conn = Mock()
            conn.list.return_value = response
            with self.subTest(response=response), patch.object(worker.fetch_batch, 'connect', return_value=conn):
                with self.assertRaises(RuntimeError):
                    worker.discover_mailboxes()
                conn.logout.assert_called_once()

    def test_fair_schedule_checks_inbox_between_other_folders(self):
        self.assertEqual(list(worker.scheduled_mailboxes(['INBOX', 'Archive', 'Imported', 'Trash'])),
                         ['INBOX', 'Archive', 'INBOX', 'Imported', 'INBOX', 'Trash', 'INBOX'])

    def test_folders_over_one_thousand_are_drained_without_a_ceiling(self):
        for size in (1205, 6248):
            pending = {str(uid).encode() for uid in range(1, size + 1)}
            cursor = 0
            processed = set()
            while pending:
                batch, cursor = worker.choose_uids(pending, cursor, 50)
                self.assertGreater(len(batch), 0)
                self.assertLessEqual(len(batch), 50)
                self.assertFalse(processed.intersection(batch))
                pending.difference_update(batch)
                processed.update(batch)
            self.assertEqual(len(processed), size)

    def test_persistent_failures_and_new_arrivals_do_not_starve_old_mail(self):
        pending = {str(uid).encode() for uid in range(1, 1206)}
        failures = {str(uid).encode() for uid in range(1156, 1206)}
        cursor = 0
        seen = set()
        for step in range(70):
            arrival = str(1206 + step).encode()
            pending.add(arrival)
            batch, cursor = worker.choose_uids(pending, cursor, 50)
            self.assertIn(arrival, batch)
            seen.update(batch)
            pending.difference_update(set(batch) - failures)
        self.assertTrue({str(uid).encode() for uid in range(1, 1206)} <= seen)
        self.assertEqual(pending, failures)

    def test_failed_fetch_advances_durable_cursor_and_closes_connection(self):
        conn = Mock()
        conn.untagged_responses = {'UIDNEXT': [b'10000'], 'EXISTS': [b'100']}
        conn.select.return_value = ('OK', [])
        conn.response.return_value = ('UIDVALIDITY', [b'100'])
        def uid(command, *args):
            return ('OK', [b' '.join(str(i).encode() for i in range(1, 121))]) if command == 'SEARCH' else ('NO', [])
        conn.uid.side_effect = uid
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'STATE_DIR', Path(directory)), patch.object(worker.fetch_batch, 'connect', return_value=conn):
            selections = []
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    worker.fetch('Imported/INBOX', str(Path(directory) / 'batch'))
                selections.append(conn.uid.call_args.args[1])
            self.assertNotEqual(selections[0], selections[1])
            saved = json.loads((Path(directory) / 'fetch_cursors.json').read_text())
            self.assertEqual(saved['Imported/INBOX']['uidvalidity'], '100')
            conn.response.return_value = ('UIDVALIDITY', [b'101'])
            with self.assertRaises(RuntimeError):
                worker.fetch('Imported/INBOX', str(Path(directory) / 'batch'))
            self.assertEqual(conn.uid.call_args.args[1], selections[0])
        self.assertEqual(conn.logout.call_count, 3)

    def test_recounts_all_discovered_mailboxes_for_auto_mode(self):
        with patch.object(worker.mailbox_settings, 'get_cached_backlog', return_value=(0, False)), patch.object(worker.mailbox_settings, 'set_backlog_estimate') as save, patch.object(worker, 'discover_mailboxes', return_value=['INBOX', 'Imported']), patch.object(worker, 'full_backlog_count', return_value=7453) as count, patch.object(worker, 'log'):
            self.assertEqual(worker.get_backlog_estimate(), 7453)
            count.assert_called_once_with(['INBOX', 'Imported'])
            save.assert_called_once_with(7453)

    def test_new_folders_join_next_pass_and_only_complete_empty_pass_idles(self):
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'PROCESSED_IDS_PATH', Path(directory) / 'processed'), patch.object(worker.process_batch.tahor_db, 'init_db'), patch.object(worker, 'discover_mailboxes', side_effect=[['INBOX'], ['INBOX', 'New folder']]), patch.object(worker, 'process_one_batch', side_effect=['processed', 'empty', 'empty', 'empty']) as process, patch.object(worker, 'log'), patch.object(worker.runtime_status, 'write_status') as status, patch.object(worker.time, 'sleep', side_effect=[None, StopLoop]) as sleep:
            with self.assertRaises(StopLoop):
                worker.main()
            self.assertEqual([call.args[0] for call in process.call_args_list], ['INBOX', 'INBOX', 'New folder', 'INBOX'])
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [worker.SLEEP_BETWEEN_BATCHES, worker.SLEEP_WHEN_IDLE])
            self.assertEqual(status.call_args.args, ('idle',))

    def test_one_folder_error_does_not_stop_other_folders(self):
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'PROCESSED_IDS_PATH', Path(directory) / 'processed'), patch.object(worker.process_batch.tahor_db, 'init_db'), patch.object(worker, 'discover_mailboxes', side_effect=[['INBOX', 'Archive'], StopLoop]), patch.object(worker, 'process_one_batch', side_effect=[RuntimeError('offline'), 'processed', 'empty']) as process, patch.object(worker, 'log'), patch.object(worker.runtime_status, 'write_status'), patch.object(worker.time, 'sleep', side_effect=[None, None, StopLoop]):
            with self.assertRaises(StopLoop):
                worker.main()
            self.assertEqual([call.args[0] for call in process.call_args_list], ['INBOX', 'Archive', 'INBOX'])

    def test_paid_success_skips_free_tier_pause_but_outage_still_backs_off(self):
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'PROCESSED_IDS_PATH', Path(directory) / 'processed'), patch.object(worker.process_batch.tahor_db, 'init_db'), patch.object(worker, 'discover_mailboxes', return_value=['INBOX', 'Archive']), patch.object(worker, 'process_one_batch', side_effect=['processed_paid', 'backend_unavailable', 'empty']), patch.object(worker, 'PAID_BATCH_DELAY', 0), patch.object(worker, 'log'), patch.object(worker.runtime_status, 'write_status'), patch.object(worker.time, 'sleep', side_effect=[None, StopLoop]) as sleep:
            with self.assertRaises(StopLoop):
                worker.main()
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0, worker.BACKEND_RETRY_SECONDS])
