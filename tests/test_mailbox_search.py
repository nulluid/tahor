import unittest
from unittest.mock import Mock, patch

from mailbox_search import search_uids, UID_WINDOW, MAX_UID


class Mailbox:
    def __init__(self, uids):
        self.uids = set(uids)
        self.refresh()
        self.searches = []
        self.fail_at = None

    def refresh(self):
        self.untagged_responses = {'EXISTS': [str(len(self.uids)).encode()],
                                  'UIDNEXT': [str(max(self.uids, default=0)+1).encode()]}

    def uid(self, command, charset, *args):
        assert command == 'SEARCH' and charset is None
        assert args[0] == 'UID'
        first, last = map(int, args[1].split(':'))
        self.searches.append(args)
        if len(self.searches) == self.fail_at:
            return 'NO', [b'private server detail']
        values = sorted(uid for uid in self.uids if first <= uid <= last)
        assert len(values) <= UID_WINDOW
        return 'OK', [b' '.join(str(uid).encode() for uid in values)]


class SearchTests(unittest.TestCase):
    def test_more_than_one_megabyte_total_without_omitting_boundary_uids(self):
        conn = Mailbox(range(1, 180001))
        status, rows = search_uids(conn, 'ALL', 'UNKEYWORD', 'retention-forever')
        self.assertEqual(status, 'OK')
        self.assertGreater(len(rows[0]), 1000000)
        self.assertEqual([int(uid) for uid in rows[0].split()], list(range(1,180001)))
        self.assertEqual(len(conn.searches), 18)
        self.assertTrue(all(row[2:] == ('ALL','UNKEYWORD','retention-forever') for row in conn.searches))

    def test_small_sparse_folder_uses_one_bounded_snapshot_request(self):
        conn = Mailbox([1, MAX_UID-1])
        self.assertEqual(search_uids(conn)[1], [b'1 4294967294'])
        self.assertEqual(len(conn.searches),1)

    def test_new_arrival_excluded_by_snapshot_then_included_next_select(self):
        conn = Mailbox([1, 1001])
        conn.uids.add(1002)
        self.assertEqual(search_uids(conn)[1], [b'1 1001'])
        conn.refresh()
        self.assertEqual(search_uids(conn)[1], [b'1 1001 1002'])

    def test_expunge_does_not_shift_uid_windows(self):
        conn = Mailbox(range(1, 20002))
        conn.uids.difference_update(range(1,9000))
        self.assertEqual([int(uid) for uid in search_uids(conn)[1][0].split()], list(range(9000,20002)))

    def test_failed_later_range_never_returns_partial_success(self):
        conn = Mailbox(range(1, 20002));conn.fail_at=2
        with self.assertRaisesRegex(RuntimeError,'partial results discarded'):
            search_uids(conn)
        self.assertEqual(len(conn.searches),2)

    def test_empty_mailbox_does_not_issue_star_or_search(self):
        conn = Mailbox([])
        self.assertEqual(search_uids(conn), ('OK',[b'']))
        self.assertFalse(conn.searches)

    def test_empty_matches_are_success_but_out_of_range_is_error(self):
        conn = Mock(untagged_responses={'UIDNEXT':[b'20001'],'EXISTS':[b'20000']})
        conn.uid.return_value=('OK',[None])
        self.assertEqual(search_uids(conn), ('OK',[b'']))
        conn.uid.return_value=('OK',[b'10001'])
        with self.assertRaisesRegex(RuntimeError,'out-of-range'):search_uids(conn)

    def test_missing_uidnext_uses_one_bounded_uid_fetch(self):
        conn = Mock(untagged_responses={'EXISTS':[b'1']})
        conn.uid.side_effect=[('OK',[b'1 (UID 42)']),('OK',[b'42'])]
        self.assertEqual(search_uids(conn),('OK',[b'42']))
        self.assertEqual(conn.uid.call_args_list[0].args,('FETCH','*','(UID)'))

    def test_invalid_boundary_or_failed_fetch_cannot_mean_empty(self):
        conn=Mock(untagged_responses={'UIDNEXT':[b'not-a-number']})
        with self.assertRaises(RuntimeError):search_uids(conn)
        conn.uid.assert_not_called()
        conn.untagged_responses={};conn.uid.return_value=('NO',[])
        with self.assertRaises(RuntimeError):search_uids(conn)

    def test_existing_uid_filter_is_preserved(self):
        conn=Mailbox([1])
        search_uids(conn,'UID','1:100','OR','SEEN','UNKEYWORD','reply-protected')
        self.assertEqual(conn.searches[0][2:],('UID','1:100','OR','SEEN','UNKEYWORD','reply-protected'))

    def test_worker_full_count_uses_bounded_search_and_closes_connection(self):
        import backlog_worker
        conn=Mailbox(range(1,180001))
        conn.select=Mock(return_value=('OK',[b'180000']))
        conn.logout=Mock()
        with patch.object(backlog_worker.fetch_batch,'connect',return_value=conn):
            self.assertEqual(backlog_worker.full_backlog_count(['Trash']),180000)
        self.assertEqual(len(conn.searches),18)
        conn.select.assert_called_once_with('"Trash"',readonly=True)
        conn.logout.assert_called_once()

    def test_retention_never_deletes_partial_range_results(self):
        import retention_sweep
        conn=Mailbox(range(1,20002));conn.fail_at=2
        conn.select=Mock(return_value=('OK',[b'20001']))
        with patch.object(retention_sweep,'delete_uids') as delete:
            with self.assertRaises(RuntimeError):
                retention_sweep.sweep_trash(conn,'Trash')
        delete.assert_not_called()

    def test_multiple_response_rows_cannot_exceed_window(self):
        conn=Mock(untagged_responses={'UIDNEXT':[b'10001'],'EXISTS':[b'10000']})
        conn.uid.return_value=('OK',[b'1 '*6000,b'2 '*6000])
        with self.assertRaisesRegex(RuntimeError,'exceeded'):search_uids(conn)
