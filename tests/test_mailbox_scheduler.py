"""Scheduling simulations and discovery use no real mailbox or model calls."""
import imaplib
import unittest
from unittest.mock import Mock, patch

import mailbox_scheduler as module


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.scheduler = module.ProductiveMailboxes(clock=lambda: self.now)

    def finish(self, status='processed_paid'):
        mailbox = self.scheduler.next_mailbox()
        self.assertIsNotNone(mailbox)
        self.scheduler.completed(mailbox, status)
        self.now += 150  # representative slow classification batch
        return mailbox

    def test_large_inbox_has_majority_and_other_productive_folders_are_fair(self):
        names = ['INBOX', 'Sent', 'Missions', 'Trash'] + ['cold'+str(i) for i in range(676)]
        self.scheduler.discovered(names)
        for name in names:
            self.scheduler.observe(name, name in names[:4])
        sequence = [self.finish() for _ in range(80)]
        self.assertEqual(sequence.count('INBOX'), 60)
        self.assertEqual(sequence.count('Sent'), 10)
        self.assertEqual(sequence.count('Missions'), 10)
        self.assertNotIn('Trash', sequence)
        self.assertFalse(any(name.startswith('cold') for name in sequence))
        self.assertLessEqual(len(self.scheduler.queue), 3)

    def test_trash_waits_for_confirmed_empty_inbox_then_new_arrival_preempts(self):
        self.scheduler.discovered(['INBOX','Trash'])
        self.scheduler.observe('Trash', True)
        self.assertEqual(self.finish('empty'), 'INBOX')
        # Periodic inbox poll is due after simulated elapsed time; no loop of
        # empty visits occurs while the clock remains unchanged.
        self.assertEqual(self.finish('empty'), 'INBOX')
        self.scheduler.inbox_at=self.now+30
        self.assertEqual(self.finish(), 'Trash')
        self.scheduler.observe('INBOX', True)
        self.assertEqual(self.finish(), 'INBOX')
        self.assertNotEqual(self.scheduler.next_mailbox(), 'Trash')

    def test_empty_inbox_is_polled_without_two_logins_per_cold_folder(self):
        self.scheduler.discovered(['INBOX']+['cold'+str(i) for i in range(679)])
        self.assertEqual(self.scheduler.next_mailbox(),'INBOX')
        self.scheduler.completed('INBOX','empty')
        for _ in range(679):
            self.assertIsNone(self.scheduler.next_mailbox())
        self.now+=30
        self.assertEqual(self.scheduler.next_mailbox(),'INBOX')

    def test_backend_failure_cools_globally_but_discovery_still_updates(self):
        self.scheduler.discovered(['INBOX','Sent'])
        self.scheduler.observe('Sent',True)
        self.assertEqual(self.scheduler.next_mailbox(),'INBOX')
        self.scheduler.completed('INBOX','backend_unavailable')
        self.scheduler.observe('Sent',True)
        self.now+=299
        self.assertIsNone(self.scheduler.next_mailbox())
        self.now+=1
        self.assertEqual(self.scheduler.next_mailbox(),'INBOX')

    def test_folder_error_does_not_block_other_folders_or_reset_by_scan(self):
        self.scheduler.discovered(['INBOX','Sent','Other'])
        self.scheduler.observe('Sent',True);self.scheduler.observe('Other',True)
        self.scheduler.completed('INBOX','empty')
        self.assertEqual(self.scheduler.next_mailbox(),'Sent')
        self.scheduler.completed('Sent','error')
        self.scheduler.observe('Sent',True)
        self.assertEqual(self.scheduler.next_mailbox(),'Other')

    def test_special_use_trash_and_removed_folders(self):
        self.scheduler.discovered(['INBOX','Bin','Other'], {'Bin'})
        self.scheduler.observe('Bin',True);self.scheduler.observe('Other',True)
        self.scheduler.discovered(['INBOX','Bin'], {'Bin'})
        for _ in range(8):self.assertEqual(self.finish(),'INBOX')

    def test_empty_observation_cannot_cancel_running_productive_batch(self):
        self.scheduler.discovered(['INBOX','Other'])
        self.scheduler.completed('INBOX','empty');self.scheduler.observe('Other',True)
        self.assertEqual(self.scheduler.next_mailbox(),'Other')
        self.scheduler.observe('Other',False)
        self.scheduler.completed('Other','processed_paid')
        self.assertEqual(self.scheduler.next_mailbox(),'Other')

    def test_idle_680_folder_simulation_bounds_sessions_independent_of_folder_count(self):
        names=['INBOX']+['cold'+str(i) for i in range(679)]
        self.scheduler.discovered(names)
        for name in names:self.scheduler.observe(name,False)
        visits=[]
        start=self.now
        for second in range(300):
            self.now=start+second
            mailbox=self.scheduler.next_mailbox()
            if mailbox is not None:
                visits.append(mailbox)
                self.scheduler.completed(mailbox,'empty')
        self.assertEqual(visits,['INBOX']*10)
        # One shared discovery login plus existing cleanup/fetch connections
        # per inbox poll: 21, rather than 2719 for an old 680-folder empty pass.
        self.assertEqual(1+2*len(visits),21)
        self.assertEqual(1+2*(2*len(names)-1),2719)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.scheduler=module.ProductiveMailboxes()
        self.conn=Mock()
        self.connect=Mock(return_value=self.conn)
        self.discovery=module.Discovery(self.scheduler,self.connect,Mock(),folder_pause=0)

    def test_680_folders_share_one_connection_and_new_folder_gets_work(self):
        names=[('INBOX',set())]+[('folder'+str(i),set()) for i in range(679)]
        with patch.object(module,'list_mailboxes',return_value=names), patch.object(module,'has_work',side_effect=lambda conn,name:name=='folder678') as probe:
            self.assertTrue(self.discovery.scan_once())
        self.connect.assert_called_once();self.conn.logout.assert_called_once()
        self.assertEqual(probe.call_count,680)
        self.assertEqual(self.scheduler.active,{'folder678'})
        with patch.object(module,'list_mailboxes',return_value=names+[('New',set())]),patch.object(module,'has_work',side_effect=lambda conn,name:name=='New'):
            self.discovery.scan_once()
        self.assertEqual(self.scheduler.active,{'New'})

    def test_connection_failure_resumes_after_bad_folder_without_starvation(self):
        names=[('INBOX',set()),('Broken',set()),('Later',set())]
        calls=[]
        def probe(conn,name):
            calls.append(name)
            if name=='Broken':raise imaplib.IMAP4.abort('private error')
            return False
        with patch.object(module,'list_mailboxes',return_value=names),patch.object(module,'has_work',side_effect=probe):
            with self.assertRaises(imaplib.IMAP4.abort):self.discovery.scan_once()
            self.assertEqual(calls,['INBOX','Broken'])
            calls.clear()
            with self.assertRaises(imaplib.IMAP4.abort):self.discovery.scan_once()
        self.assertEqual(calls[0],'Later')
        self.assertEqual(self.conn.logout.call_count,2)

    def test_stopping_closes_connection_and_does_not_scan(self):
        self.discovery.stop_event.set()
        with patch.object(module,'list_mailboxes',return_value=[('INBOX',set())]),patch.object(module,'has_work') as probe:
            self.assertFalse(self.discovery.scan_once())
        probe.assert_not_called();self.conn.logout.assert_called_once()

    def test_pending_trash_discovery_preserves_deletion_protections(self):
        self.conn.select.return_value=('OK',[])
        with patch.object(module,'search_uids',return_value=('OK',[b'42'])) as search:
            self.assertTrue(module.has_work(self.conn,'Trash'))
        self.conn.select.assert_called_once_with('"Trash"',readonly=True)
        query=' '.join(search.call_args.args[1:])
        for term in ('delete-pending','retention-forever','retention-pending-review','needs-attention','OR SEEN UNKEYWORD reply-protected'):
            self.assertIn(term,query)

    def test_cold_folder_select_failure_is_retryable_not_empty(self):
        self.conn.select.return_value=('NO',[])
        with self.assertRaises(RuntimeError):module.has_work(self.conn,'Other')
