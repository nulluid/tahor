"""Subscription previews retain bounded real identities without storing bodies."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import tahor_db
import process_batch


class SubscriptionSampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = patch.object(tahor_db, 'DB_PATH', self.root / 'decisions.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        tahor_db.init_db()
        self.candidate = tahor_db.upsert_unsubscribe_candidate('example.com', 'news@example.com', 'News', 'https://example.com/u', None, True)

    def sample(self, identifier, day, **updates):
        value = dict(mailbox='INBOX', message_id=identifier, uid=str(day), uidvalidity='42',
                     sender_email='news@example.com', display_name='News', subject='Update ' + str(day),
                     date=f'2026-09-{day:02d}T12:00:00+00:00', received_at=f'2026-09-{day:02d}T13:00:00+00:00')
        value.update(updates)
        return value

    def test_latest_three_use_delivery_time_not_backlog_scan_order(self):
        for day in (9, 7, 11, 1, 10):
            tahor_db.record_subscription_sample(self.candidate, self.sample(f'<{day}@example.com>', day))
        samples = tahor_db.get_subscription_samples(self.candidate)
        self.assertEqual([row['message_id'] for row in samples], ['<11@example.com>', '<10@example.com>', '<9@example.com>'])
        self.assertEqual(samples[0]['date'], '2026-09-11T12:00:00+00:00')
        self.assertEqual(samples[0]['uidvalidity'], '42')

    def test_timezone_normalization_orders_by_instant(self):
        tahor_db.record_subscription_sample(self.candidate, self.sample('<early@example.com>', 1, received_at='2026-09-01T09:00:00+00:00'))
        tahor_db.record_subscription_sample(self.candidate, self.sample('<later@example.com>', 1, received_at='2026-09-01T08:00:00-06:00'))
        samples = tahor_db.get_subscription_samples(self.candidate)
        self.assertEqual(samples[0]['message_id'], '<later@example.com>')
        self.assertEqual(samples[0]['received_at'], '2026-09-01T14:00:00+00:00')

    def test_repeated_observation_refreshes_identity_without_incrementing_count(self):
        metadata = self.sample('<observed@example.com>', 2)
        for mailbox, uid in [('INBOX', '2'), ('Filed/Updates', '90')]:
            metadata.update(mailbox=mailbox, uid=uid)
            tahor_db.upsert_unsubscribe_candidate('example.com', 'news@example.com', 'News', None, 'leave@example.com', False,
                                                  metadata['message_id'], metadata['received_at'], True, metadata=metadata)
        self.assertEqual(tahor_db.get_unsubscribe_candidate('example.com')['message_count'], 2)
        rows = tahor_db.get_subscription_samples(self.candidate)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['mailbox'], rows[0]['uid']), ('Filed/Updates', '90'))

    def test_no_body_or_unsubscribe_token_is_copied_into_sample(self):
        metadata = self.sample('<minimal@example.com>', 1, body='PRIVATE BODY', snippet='PRIVATE EXCERPT', unsubscribe_url='https://example.com/PRIVATE_TOKEN')
        tahor_db.record_subscription_sample(self.candidate, metadata)
        serialized = json.dumps(tahor_db.get_subscription_samples(self.candidate))
        for secret in ('PRIVATE BODY', 'PRIVATE EXCERPT', 'PRIVATE_TOKEN'):
            self.assertNotIn(secret, serialized)
        db = tahor_db.get_db()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(subscription_message_samples)')}
        db.close()
        self.assertFalse(columns.intersection({'body', 'snippet', 'unsubscribe_url'}))

    def test_wrong_candidate_domain_or_invalid_identity_is_not_saved(self):
        for updates in ({'sender_email': 'news@example.com.attacker.test'}, {'message_id': '<bad\r\n@example.com>'}, {'mailbox': ''}):
            self.assertIsNone(tahor_db.record_subscription_sample(self.candidate, self.sample('<x@example.com>', 1, **updates)))
        self.assertEqual(tahor_db.get_subscription_samples(self.candidate), [])
        self.assertIsNone(tahor_db.record_subscription_sample(self.candidate + 999, self.sample('<x@example.com>', 1)))

    def test_invalid_uid_shortcuts_fall_back_to_message_identity(self):
        for uid in ('²', '9' * 5000, '4294967296', '0'):
            tahor_db.record_subscription_sample(self.candidate, self.sample('<identity@example.com>', 1, uid=uid))
            saved = tahor_db.get_subscription_samples(self.candidate)[0]
            self.assertIsNone(saved['uid'])
            self.assertIsNone(saved['uidvalidity'])
            self.assertEqual(saved['message_id'], '<identity@example.com>')

    def test_shared_domain_preserves_each_actual_sender_without_merchant_guessing(self):
        candidate = tahor_db.upsert_unsubscribe_candidate('shared.example', 'store-a@shared.example', 'Store A', None, 'leave@shared.example', False)
        for day, sender in [(1, 'store-a@shared.example'), (2, 'store-b@shared.example')]:
            tahor_db.record_subscription_sample(candidate, self.sample(f'<{day}@shared.example>', day, sender_email=sender))
        samples = tahor_db.get_subscription_samples(candidate)
        self.assertEqual({row['sender_email'] for row in samples}, {'store-a@shared.example', 'store-b@shared.example'})
        self.assertEqual(tahor_db.get_unsubscribe_candidate('shared.example')['sender_email'], 'store-a@shared.example')

    def test_pipeline_captures_real_envelope_for_viewing_without_extra_model_or_imap(self):
        prefix = str(self.root / 'batch')
        identifier = '<pipeline@example.com>'
        records = [dict(id=identifier, subject='A real update', **{'from': 'news@example.com'}, date='2026-09-01T12:00:00+00:00')]
        outputs = [dict(id=identifier, action='keep', category='marketing', retention='standard', needs_attention=False)]
        envelopes = [dict(message_id=identifier, uid='17', uidvalidity='42', subject='A real update', from_email='news@example.com', display_name='News', internaldate='01-Sep-2026 13:00:00 +0000', unsubscribe_url='https://example.com/u', one_click=True)]
        for suffix, values in [('in', records), ('out', outputs), ('env', envelopes)]:
            Path(prefix + '_' + suffix + '.json').write_text(json.dumps(values))
        with patch.object(sys, 'argv', ['process_batch.py', prefix, 'INBOX']), patch.object(process_batch, 'tahor_db', tahor_db), patch.object(process_batch.reply_rules, 'get_rules', return_value=[]), patch.object(process_batch.coupon_expiry, 'policies', return_value={}):
            process_batch.main()
        sample = tahor_db.get_subscription_samples(self.candidate)[0]
        self.assertEqual((sample['mailbox'], sample['message_id'], sample['uid'], sample['uidvalidity']), ('INBOX', identifier, '17', '42'))
        self.assertEqual(sample['sender_email'], 'news@example.com')
        self.assertEqual(sample['subject'], 'A real update')
        self.assertEqual(sample['received_at'], '2026-09-01T13:00:00+00:00')

    def test_sqlite_backup_retains_samples_for_private_recovery(self):
        identifier = tahor_db.record_subscription_sample(self.candidate, self.sample('<backup@example.com>', 1))
        source = tahor_db.get_db()
        destination = sqlite3.connect(self.root / 'restored.db')
        try:
            source.backup(destination)
            self.assertEqual(destination.execute('SELECT message_id FROM subscription_message_samples WHERE id=?', (identifier,)).fetchone()[0], '<backup@example.com>')
        finally:
            source.close()
            destination.close()
