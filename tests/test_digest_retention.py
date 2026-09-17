from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import digest_retention as digests
import notifications
import retention_sweep


class DigestRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self.event_id = 'digest:2026-09-16'
        self.identifier = notifications.notification_message_id(self.event_id)
        self.record = dict(kind='digest', status='sent', subject='Tahor daily summary',
                           body='Healthy.\n\nOpen Tahor: https://mail.example.com\n')
        self.records = {self.identifier: self.record}
        self.flags = b'\\Seen category-notification retention-standard'
        self.latest_flags = None
        self.delivered = self.now - timedelta(days=4)
        self.message = EmailMessage()
        for name, value in [('From', 'owner@example.com'), ('To', 'owner@example.com'),
                            ('Message-ID', self.identifier), ('Subject', self.record['subject']),
                            ('X-Tahor-Notification', '1'), ('Auto-Submitted', 'auto-generated')]:
            self.message[name] = value
        self.message.set_content(self.record['body'])
        self.client = Mock()
        self.client.capabilities = (b'IMAP4rev1', b'UIDPLUS')
        self.client.select.return_value = ('OK', [])
        self.client.uid.side_effect = self.uid
        self.start(patch.object(digests.config, 'email_address', return_value='owner@example.com'))
        self.grace = self.start(patch.object(digests.mailbox_settings, 'get_inbox_grace_days',
                                            return_value={'read': 3, 'unread': 7}))
        self.search = self.start(patch.object(digests, 'search_uids', return_value=('OK', [b'42'])))

    def start(self, p):
        value = p.start(); self.addCleanup(p.stop); return value

    def uid(self, command, uid, *args):
        if command == 'FETCH':
            flags = self.flags if 'BODY.PEEK[]' in args[0] or self.latest_flags is None else self.latest_flags
            meta = (b'1 (UID 42 FLAGS (' + flags + b') INTERNALDATE "' +
                    self.delivered.strftime('%d-%b-%Y %H:%M:%S %z').encode() + b'")')
            return ('OK', [(meta, self.message.as_bytes())]) if 'BODY.PEEK[]' in args[0] else ('OK', [meta])
        return 'OK', []

    def sweep(self, dry_run=False):
        return digests.sweep(self.client, 'INBOX', self.records, dry_run,
                             retention_sweep.delete_uids, self.now)

    def assert_no_delete(self):
        self.assertFalse(any(c.args[0] == 'EXPUNGE' or (c.args[0] == 'STORE' and '\\Deleted' in c.args[-1])
                             for c in self.client.uid.call_args_list))

    def test_read_digest_deleted_at_three_days_by_targeted_uid(self):
        self.delivered = self.now - timedelta(days=3)
        self.assertEqual(self.sweep(), (1, 1))
        self.assertIn(('EXPUNGE', b'42'), [c.args for c in self.client.uid.call_args_list])
        self.client.expunge.assert_not_called()
        self.assertTrue(all('PEEK' in c.args[2] or c.args[2] == '(UID FLAGS INTERNALDATE)'
                            for c in self.client.uid.call_args_list if c.args[0] == 'FETCH'))

    def test_unread_digest_kept_until_seven_days(self):
        self.flags = b'category-notification retention-standard'
        self.assertEqual(self.sweep(), (0, 0)); self.assert_no_delete()
        self.delivered = self.now - timedelta(days=7)
        self.assertEqual(self.sweep(), (1, 1))

    def test_younger_read_digest_is_only_classified(self):
        self.delivered = self.now - timedelta(days=3) + timedelta(seconds=1)
        self.assertEqual(self.sweep(), (0, 0)); self.assert_no_delete()
        self.assertIn(('STORE', b'42', '+FLAGS', '(category-notification category-tahor-digest retention-standard)'),
                      [c.args for c in self.client.uid.call_args_list])

    def test_existing_settings_control_both_cutoffs(self):
        self.grace.return_value = {'read': 5, 'unread': 10}
        self.assertEqual(self.sweep(), (0, 0)); self.assert_no_delete()
        self.grace.return_value = {'read': 0, 'unread': 10}
        self.assertEqual(self.sweep(), (1, 1))

    def test_marked_unread_during_sweep_is_preserved(self):
        self.latest_flags = b'category-notification'
        self.assertEqual(self.sweep(), (0, 0)); self.assert_no_delete()

    def test_protection_added_during_sweep_is_preserved(self):
        self.latest_flags = self.flags + b' retention-forever'
        self.assertEqual(self.sweep(), (0, 0)); self.assert_no_delete()

    def test_identity_or_content_mismatch_never_deletes(self):
        for change in ('body', 'from', 'id', 'duplicate_header'):
            with self.subTest(change=change):
                original = self.message.as_bytes()
                if change == 'body':
                    self.message.set_content('Important receipt, not the generated summary.')
                elif change == 'from':
                    self.message.replace_header('From', 'outsider@example.com')
                elif change == 'id':
                    self.message.replace_header('Message-ID', '<unrelated@example.com>')
                else:
                    self.message['X-Tahor-Notification'] = '1'
                self.client.reset_mock()
                self.assertEqual(self.sweep(), (0, 0))
                self.assert_no_delete()
                import email
                self.message = email.message_from_bytes(original, policy=email.policy.default)

    def test_dry_run_never_writes(self):
        self.assertEqual(self.sweep(True), (1, 0))
        self.assertTrue(all(c.args[0] == 'FETCH' for c in self.client.uid.call_args_list))
        self.client.select.assert_called_once_with('"INBOX"', readonly=True)

    def test_missing_uidplus_refuses_permanent_deletion(self):
        self.client.capabilities = (b'IMAP4rev1',)
        with self.assertRaisesRegex(RuntimeError, 'UIDPLUS'):
            self.sweep()
        self.assert_no_delete()

    def test_brief_retention_rechecks_read_state_and_uses_targeted_deletion(self):
        self.flags += b' retention-short-lived'
        with patch.object(retention_sweep, 'search_uids', return_value=('OK', [b'42'])):
            self.assertEqual(retention_sweep.sweep_short_lived(self.client, 'Archive', now=self.now), (1, 1))
            self.client.reset_mock()
            self.latest_flags = b'retention-short-lived'
            self.assertEqual(retention_sweep.sweep_short_lived(self.client, 'Archive', now=self.now), (0, 0))
            self.assert_no_delete()

    def test_brief_retention_without_marker_or_protected_is_preserved(self):
        with patch.object(retention_sweep, 'search_uids', return_value=('OK', [b'42'])):
            for flags in (b'\\Seen retention-standard', b'\\Seen retention-short-lived retention-forever',
                          b'\\Seen retention-short-lived \\Flagged', b'\\Seen retention-short-lived needs-attention'):
                self.flags = flags
                self.assertEqual(retention_sweep.sweep_short_lived(self.client, 'INBOX', now=self.now), (0, 0))
                self.assert_no_delete()

    def test_failed_targeted_expunge_rolls_back_deleted_flag(self):
        self.flags += b' category-tahor-digest'
        def failed_expunge(command, *args):
            return ('NO', []) if command == 'EXPUNGE' else self.uid(command, *args)
        self.client.uid.side_effect = failed_expunge
        with self.assertRaisesRegex(RuntimeError, 'Targeted retention deletion failed'):
            self.sweep()
        self.assertEqual(self.client.uid.call_args.args, ('STORE', b'42', '-FLAGS', '(\\Deleted)'))
        self.client.expunge.assert_not_called()

    def test_failed_search_fails_closed(self):
        self.search.return_value = 'NO', []
        with self.assertRaisesRegex(RuntimeError, 'search failed'):
            self.sweep()
        self.client.uid.assert_not_called()

    def test_only_sent_digest_journal_entries_are_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'notifications.json'
            state = {'events': {self.event_id: self.record,
                                'health:worker:test': dict(self.record, kind='health'),
                                'digest:pending': dict(self.record, status='uncertain')}}
            path.write_text(json.dumps(state))
            with patch.object(notifications, 'state_path', return_value=path):
                self.assertEqual(digests.known_digests(), self.records)
                path.write_text('{')
                with self.assertRaises(ValueError):
                    digests.known_digests()

    def test_digest_cleanup_includes_archive_and_trash(self):
        with patch.object(retention_sweep, 'connect', return_value=self.client), \
             patch.object(retention_sweep, 'list_all_paths', return_value=['INBOX', 'Archive', 'Trash']), \
             patch.object(digests, 'known_digests', return_value=self.records), \
             patch.object(digests, 'sweep', return_value=(0, 0)) as sweep, \
             patch.object(retention_sweep.coupon_expiry, 'sweep', return_value=(0, 0)), \
             patch.object(retention_sweep, 'sweep_short_lived', return_value=(0, 0)), \
             patch.object(retention_sweep, 'sweep_mailbox', return_value=(0, 0)) as ordinary, \
             patch.object(retention_sweep.sys, 'argv', ['retention_sweep.py']):
            retention_sweep.main()
        self.assertEqual([c.args[1] for c in sweep.call_args_list], ['INBOX', 'Archive', 'Trash'])
        self.assertEqual({c.args[1] for c in ordinary.call_args_list}, {'INBOX'})


if __name__ == '__main__':
    unittest.main()
