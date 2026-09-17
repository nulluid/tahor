import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

import notifications
import email

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import notification_services


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'notifications.json'
        import ai_routing
        routing = patch.object(ai_routing, 'state_path', return_value=Path(self.temp.name)/'ai-routing.json')
        routing.start()
        self.addCleanup(routing.stop)
        self.environment = patch.dict(os.environ, {'TAHOR_NOTIFICATION_STATE': str(self.path), 'TAHOR_NOTIFY_HEALTH': '1', 'TAHOR_NOTIFY_DIGEST': '0', 'TAHOR_NOTIFY_TIMEZONE': 'UTC', 'TAHOR_NOTIFY_HOUR': '9', 'FASTMAIL_EMAIL': 'owner@example.com', 'FASTMAIL_APP_PASSWORD': 'private-app-password', 'BASE_URL': 'https://tahor.example.com'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = datetime(2026, 9, 17, 10, tzinfo=timezone.utc).timestamp()
        self.worker = patch.object(notifications.runtime_status, 'read_status', return_value={})
        self.snapshot = self.worker.start()
        self.addCleanup(self.worker.stop)
        self.connector = patch.object(notifications.provider_bridge, 'status', return_value={'enabled': False})
        self.provider = self.connector.start()
        self.addCleanup(self.connector.stop)
        self.client = Mock()
        self.client.select.return_value = ('OK', [])
        self.client.list.return_value = ('OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"'])
        self.notices = {}
        self.client.uid.side_effect = self.uid
        self.client.append.side_effect = self.append
        self.transport = patch.object(notifications.fetch_batch, 'connect', return_value=self.client)
        self.connect = self.transport.start()
        self.addCleanup(self.transport.stop)

    def uid(self, command, *args):
        if command == 'SEARCH':
            identifier = args[-1].strip('"')
            self.last_search = identifier
            return 'OK', [b'7' if identifier in self.notices else b'']
        if command == 'FETCH':
            return 'OK', [(b'1 (UID 7)', self.notices[self.last_search])]
        raise AssertionError('Unexpected IMAP command')

    def append(self, mailbox, flags, date, raw):
        message = email.message_from_bytes(raw)
        self.notices[message['Message-ID']] = raw
        return 'OK', []

    def poll_problem(self):
        for offset in (0, 900, 1800):
            notifications.run(self.now+offset)

    def test_disabled_by_default_never_contacts_mailbox(self):
        with patch.dict(os.environ, {'TAHOR_NOTIFY_HEALTH': '0', 'TAHOR_NOTIFY_DIGEST': '0'}):
            self.assertEqual(notifications.run(self.now), 0)
        self.connect.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_three_spaced_checks_deliver_once_to_self_with_sanitized_content(self):
        notifications.run(self.now)
        notifications.run(self.now+1)
        notifications.run(self.now+900)
        self.connect.assert_not_called()
        self.assertEqual(notifications.run(self.now+1800), 1)
        notifications.run(self.now+2700)
        self.client.append.assert_called_once()
        message = email.message_from_bytes(self.client.append.call_args.args[3])
        self.assertEqual(message['From'], 'owner@example.com')
        self.assertEqual(message['To'], 'owner@example.com')
        self.assertEqual(self.client.append.call_args.args[0], 'INBOX')
        self.assertNotIn('\\Seen', self.client.append.call_args.args[1])
        self.assertNotIn(b'private-app-password', self.client.append.call_args.args[3])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_healthy_idle_and_progressing_free_fallback_do_not_alert(self):
        for offset in range(0, 10800, 900):
            self.snapshot.return_value = {'state': 'idle', 'updated_at': datetime.fromtimestamp(self.now+offset, timezone.utc).isoformat(), 'last_success_at': '2000-01-01T00:00:00+00:00'}
            notifications.run(self.now+offset)
        for offset in range(10800, 21600, 900):
            instant = datetime.fromtimestamp(self.now+offset, timezone.utc).isoformat()
            self.snapshot.return_value = {'state': 'retrying', 'updated_at': instant, 'last_success_at': instant, 'error': 'SECRET should never be included'}
            notifications.run(self.now+offset)
        self.connect.assert_not_called()

    def test_stuck_active_worker_with_fresh_heartbeat_eventually_alerts(self):
        for offset in range(0, 7200, 900):
            self.snapshot.return_value = {'state': 'classifying', 'updated_at': datetime.fromtimestamp(self.now+offset, timezone.utc).isoformat()}
            notifications.run(self.now+offset)
        self.client.append.assert_called_once()
        self.assertIn(b'completed a batch', self.client.append.call_args.args[3])

    def test_connector_authentication_alert_requires_enabled_connector(self):
        self.snapshot.return_value = {'state': 'idle', 'updated_at': datetime.fromtimestamp(self.now+1800, timezone.utc).isoformat()}
        self.provider.return_value = {'enabled': True, 'state': 'authentication_required', 'label': 'SECRET'}
        self.poll_problem()
        self.client.append.assert_called_once()
        self.assertNotIn(b'SECRET', self.client.append.call_args.args[3])
        self.assertIn(b'administrator enrollment or sign-in', self.client.append.call_args.args[3])

    def test_explicit_append_rejection_retries_and_lost_response_reconciles(self):
        self.client.append.side_effect = None
        self.client.append.return_value = ('NO', [])
        self.poll_problem()
        self.client.append.assert_called_once()
        self.client.append.side_effect = self.append
        self.assertEqual(notifications.run(self.now+2700), 1)
        self.assertEqual(self.client.append.call_count, 2)
        self.path.unlink()
        self.notices.clear()
        self.client.reset_mock()
        def lost_response(*args):
            self.append(*args)
            raise OSError('Private server response must not be logged')
        self.client.append.side_effect = lost_response
        self.poll_problem()
        self.assertEqual(next(iter(json.loads(self.path.read_text())['events'].values()))['status'], 'uncertain')
        self.assertEqual(notifications.run(self.now+2700), 1)
        notifications.run(self.now+3600)
        self.client.append.assert_called_once()
        self.assertEqual(next(iter(json.loads(self.path.read_text())['events'].values()))['status'], 'sent')
        self.client.list.assert_called_once()

    def test_connection_failure_is_retryable_without_secret_logging(self):
        self.connect.side_effect = OSError('SECRET')
        with patch('builtins.print') as output:
            self.poll_problem()
        self.client.append.assert_not_called()
        self.assertNotIn('SECRET', str(output.call_args_list))
        self.connect.side_effect = None
        self.assertEqual(notifications.run(self.now+2700), 1)

    def test_failed_reconciliation_never_blindly_appends(self):
        self.client.uid.side_effect = [('NO', [])]
        self.poll_problem()
        self.client.append.assert_not_called()
        self.assertEqual(next(iter(json.loads(self.path.read_text())['events'].values()))['status'], 'pending')

    def test_append_success_requires_exact_readback_confirmation(self):
        self.client.append.side_effect = None
        self.client.append.return_value = ('OK', [])
        self.poll_problem()
        self.assertEqual(next(iter(json.loads(self.path.read_text())['events'].values()))['status'], 'uncertain')

    def test_notification_search_does_not_accept_substring_message_id(self):
        self.client.uid.side_effect = [('OK', [b'7']), ('OK', [(b'1 (UID 7)', b'Message-ID: <prefix-id@example.com>\r\n')])]
        self.assertFalse(notifications.find_notice(self.client, '<id@example.com>'))

    def test_daily_digest_respects_local_hour_and_restarts(self):
        with patch.dict(os.environ, {'TAHOR_NOTIFY_HEALTH': '0', 'TAHOR_NOTIFY_DIGEST': '1', 'TAHOR_NOTIFY_TIMEZONE': 'America/Denver', 'TAHOR_NOTIFY_HOUR': '9'}), patch.object(notifications, 'digest_counts', return_value=(4, 2, 1)):
            # September Denver is UTC-6: 14:00 UTC is too early, 15:00 UTC is due.
            self.assertEqual(notifications.run(self.now+4*3600), 0)
            self.assertEqual(notifications.run(self.now+5*3600), 1)
            self.assertEqual(notifications.run(self.now+6*3600), 0)
            self.assertEqual(notifications.run(self.now+29*3600), 1)
        self.assertEqual(self.client.append.call_count, 2)
        self.assertIn(b'Pending decisions: 4', self.client.append.call_args.args[3])

    def test_digest_links_to_tahor_and_is_tagged_for_dedicated_retention(self):
        with patch.dict(os.environ, {'TAHOR_NOTIFY_HEALTH': '0', 'TAHOR_NOTIFY_DIGEST': '1'}), patch.object(notifications, 'digest_counts', return_value=(4, 2, 1)):
            self.assertEqual(notifications.run(self.now), 1)
        mailbox, flags, internaldate, raw = self.client.append.call_args.args
        message = email.message_from_bytes(raw)
        body = message.get_payload(decode=True).decode(message.get_content_charset())
        self.assertIn('Open Tahor: https://tahor.example.com/', body)
        saved = json.loads(self.path.read_text())['events']['digest:2026-09-17']
        self.assertEqual(saved['body'], body)
        self.assertTrue(saved['body_prepared'])
        self.assertEqual(message['X-Tahor-Notification-Kind'], 'digest')
        self.assertIn('category-tahor-digest', flags)
        self.assertIn('retention-standard', flags)
        self.assertNotIn('\\Seen', flags)
        self.assertEqual(message['Message-ID'], notifications.notification_message_id('digest:2026-09-17'))

    def test_digest_retry_keeps_journal_body_and_does_not_repeat_link(self):
        self.client.append.side_effect = None
        self.client.append.return_value = ('NO', [])
        with patch.dict(os.environ, {'TAHOR_NOTIFY_HEALTH': '0', 'TAHOR_NOTIFY_DIGEST': '1'}), patch.object(notifications, 'digest_counts', return_value=(4, 2, 1)):
            notifications.run(self.now)
            saved = json.loads(self.path.read_text())['events']['digest:2026-09-17']['body']
            self.assertEqual(saved.count('Open Tahor:'), 1)
            self.client.append.side_effect = self.append
            with patch.dict(os.environ, {'BASE_URL': 'https://changed.example.com'}):
                self.assertEqual(notifications.run(self.now + 900), 1)
        message = email.message_from_bytes(self.client.append.call_args.args[3])
        self.assertEqual(message.get_payload(decode=True).decode(message.get_content_charset()), saved)

    def test_health_alert_retention_and_body_are_unchanged(self):
        self.poll_problem()
        message = email.message_from_bytes(self.client.append.call_args.args[3])
        self.assertIsNone(message['X-Tahor-Notification-Kind'])
        self.assertEqual(self.client.append.call_args.args[1], '(category-notification retention-standard)')
        self.assertNotIn(b'Open Tahor:', self.client.append.call_args.args[3])

    def test_unsafe_or_missing_website_urls_are_not_exposed_in_digest(self):
        invalid = ('', 'javascript:alert(1)', '//example.com', 'https://owner:secret@example.com',
                   'https://example.com/?token=secret', 'https://example.com/#secret',
                   'https://example.com/private/secret', 'https://example.com:99999',
                   'https://example.com\n', 'https://example.com\\@other.example',
                   'https://[broken', 'https://')
        for url in invalid:
            with self.subTest(url=url), patch.dict(os.environ, {'BASE_URL': url}):
                _, message = notifications.make_message({'kind': 'digest', 'subject': 'Summary', 'body': 'Summary'}, 'digest:test')
                self.assertEqual(message.get_content(), 'Summary\n')
                self.assertIsNone(notifications.website_url())

    def test_local_and_https_origins_are_supported_without_a_public_site(self):
        for url, expected in [('http://localhost:8420', 'http://localhost:8420/'),
                              ('https://tahor.example.com/', 'https://tahor.example.com/'),
                              ('http://[::1]:8420', 'http://[::1]:8420/')]:
            with self.subTest(url=url), patch.dict(os.environ, {'BASE_URL': url}):
                self.assertEqual(notifications.website_url(), expected)

    def test_corrupt_ledger_fails_closed(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            notifications.run(self.now)
        self.connect.assert_not_called()

    def test_database_summary_reads_counts_not_private_subjects_or_bodies(self):
        with patch.object(notifications.tahor_db, 'DB_PATH', Path(self.temp.name)/'db.sqlite'):
            notifications.tahor_db.init_db()
            database = notifications.tahor_db.get_db()
            with database:
                database.execute("INSERT INTO decisions(kind,summary,status,created_at) VALUES ('review','PRIVATE','pending','2026-09-17')")
            database.close()
            self.assertEqual(notifications.digest_counts(), (1, 0, 0))


    def test_persistent_ai_failure_alert_does_not_wait_three_more_checks(self):
        import ai_routing
        self.snapshot.return_value = {'state': 'idle', 'updated_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat()}
        with patch.object(ai_routing, 'persistent_problems', return_value=['ai_rule']):
            self.assertEqual(notifications.run(self.now), 1)
            self.assertEqual(notifications.run(self.now + 900), 0)
        self.client.append.assert_called_once()
        body = next(iter(self.notices.values())).decode()
        self.assertIn('30 minutes', body)
        with patch.object(ai_routing, 'persistent_problems', return_value=[]):
            notifications.run(self.now + 1800)
        state = json.loads(self.path.read_text())
        self.assertNotIn('ai_rule', state['problems'])


class NotificationServiceTests(unittest.TestCase):
    def test_units_preserve_unprivileged_hardening_and_use_notify(self):
        units = notification_services.render('tahor', Path('/etc/tahor/config.env'), Path('/var/lib/tahor'), Path('/usr/bin/python3'), ROOT)
        self.assertEqual(len(units), 2)
        service = units['tahor-notifications.service']
        for part in ('User=tahor', 'NoNewPrivileges=true', 'ProtectSystem=strict', ' notify\n', 'TimeoutStartSec=300'):
            self.assertIn(part, service)
        self.assertIn('OnCalendar=*:0/15', units['tahor-notifications.timer'])

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'Linux systemd parser required')
    def test_real_systemd_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            files = []
            for name, text in notification_services.render('tahor', Path('/etc/tahor/config.env'), Path('/var/lib/tahor'), Path('/usr/bin/python3'), ROOT).items():
                path = Path(folder)/name
                path.write_text(text)
                files.append(str(path))
            result = subprocess.run(['systemd-analyze', 'verify', *files], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
