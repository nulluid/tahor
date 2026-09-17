import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_instructions as notes


class CardInstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbmodule = importlib.import_module('tahor_db')
        self.path = Path(self.tmp.name) / 'decisions.db'
        setting = patch.object(self.dbmodule, 'DB_PATH', self.path)
        setting.start(); self.addCleanup(setting.stop)
        self.dbmodule.init_db()
        self.identifier = self.dbmodule.upsert_unsubscribe_candidate('example.com', 'updates@example.com', 'Untrusted title', 'https://example.com/remove?PRIVATE_TOKEN', None, True)

    def decision(self, kind='vendor_mapping', context=None):
        conn = self.dbmodule.get_db()
        with conn:
            identifier = conn.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES(?,?,?,'pending','2026-01-01')", (kind,'Untrusted subject',json.dumps(context or {'sender_email':'updates@example.com','suggestion_status':'ready','suggestion_version':2}))).lastrowid
        conn.close()
        return identifier

    def test_save_guidance_is_idempotent_exact_sender_private_context(self):
        self.assertEqual(notes.get_card_instructions('subscription', 9999), '')
        first = notes.save_card_instructions('subscription', self.identifier, 'Keep these community updates.')
        conn = self.dbmodule.get_db()
        before = notes.revision(conn)
        again = notes.save_card_instructions('subscription', self.identifier, first['note'])
        self.assertEqual(notes.revision(conn), before)
        self.assertIsNone(again['decision_id'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM decisions').fetchone()[0], 0)
        self.assertEqual(notes.for_senders(conn, ['other@example.com']), [])
        self.assertEqual(notes.for_senders(conn, ['updates@example.com'])[0]['instructions'], first['note'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(notes.for_senders(conn, ['updates@example.com'])))
        self.assertEqual(notes.get_all_card_instructions('subscription'), {self.identifier:first['note']})
        conn.close()

    def test_proposal_is_queued_once_without_applying_or_copying_email_content(self):
        first = notes.save_card_instructions('subscription', self.identifier, 'Keep these updates.', propose_rule=True)
        again = notes.save_card_instructions('subscription', self.identifier, 'Keep these updates.', propose_rule=True)
        self.assertEqual(first['decision_id'], again['decision_id'])
        conn = self.dbmodule.get_db()
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (first['decision_id'],)).fetchone()
        self.assertEqual(row['kind'], 'free_text_rule')
        context = json.loads(row['context']); resolution = json.loads(row['resolution'])
        self.assertNotIn('approved_proposal', resolution)
        self.assertNotIn('PRIVATE_TOKEN', row['resolution'])
        self.assertNotIn('Untrusted title', row['resolution'])
        self.assertIn('updates@example.com', resolution['text'])
        self.assertEqual(context['card_instruction_exact_sender'], 'updates@example.com')
        with self.assertRaises(ValueError):
            notes.validate_proposal(context, {'kind':'sender_rule'})
        notes.validate_proposal(context, {'kind':'file_edit'})
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sender_rules').fetchone()[0], 0)
        conn.close()

    def test_missing_handled_or_changed_source_cannot_silently_change_instructions(self):
        with self.assertRaises(LookupError):
            notes.save_card_instructions('subscription', 99999, 'Keep')
        notes.save_card_instructions('subscription', self.identifier, 'Keep')
        conn = self.dbmodule.get_db()
        with conn:conn.execute("UPDATE unsubscribe_candidates SET sender_email='other@example.com' WHERE id=?", (self.identifier,))
        with self.assertRaises(ValueError):
            notes.save_card_instructions('subscription', self.identifier, 'Different')
        with conn:conn.execute("UPDATE unsubscribe_candidates SET status='resolved' WHERE id=?", (self.identifier,))
        with self.assertRaises(ValueError):
            notes.save_card_instructions('subscription', self.identifier, 'Different')
        conn.close()
        self.assertEqual(notes.get_card_instructions('subscription', self.identifier), 'Keep')

    def test_vendor_guidance_invalidates_old_model_context_without_automatic_action(self):
        identifier = self.decision()
        notes.save_card_instructions('decision', identifier, 'This sender is a community organization.')
        conn = self.dbmodule.get_db()
        row = conn.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
        context = json.loads(row['context'])
        self.assertEqual(context['suggestion_status'], 'pending')
        self.assertNotIn('suggestion_version', context)
        self.assertIn('card_instruction_revision', context)
        self.assertIsNone(row['resolution'])
        self.assertEqual(notes.for_senders(conn, ['updates@example.com'])[0]['instructions'], 'This sender is a community organization.')
        conn.close()

    def test_latest_sender_instruction_wins_across_card_types(self):
        notes.save_card_instructions('subscription', self.identifier, 'First guidance')
        identifier = self.decision()
        notes.save_card_instructions('decision', identifier, 'Latest guidance')
        conn = self.dbmodule.get_db()
        self.assertEqual(notes.for_senders(conn, ['updates@example.com']), [{'sender_email':'updates@example.com','instructions':'Latest guidance'}])
        notes.save_card_instructions('subscription', self.identifier, 'First guidance')
        self.assertEqual(notes.for_senders(conn, ['updates@example.com'])[0]['instructions'], 'Latest guidance')
        conn.close()

    def test_superseded_instruction_cannot_apply_old_reviewed_proposal(self):
        result = notes.save_card_instructions('subscription', self.identifier, 'Keep updates.', propose_rule=True)
        conn = self.dbmodule.get_db()
        context = json.loads(conn.execute('SELECT context FROM decisions WHERE id=?', (result['decision_id'],)).fetchone()[0])
        conn.close()
        notes.validate_proposal(context, {'kind':'file_edit'})
        notes.save_card_instructions('subscription', self.identifier, 'Keep only receipts.')
        with self.assertRaisesRegex(ValueError, 'instructions changed'):
            notes.validate_proposal(context, {'kind':'file_edit'})

    def test_subscription_context_consumes_exact_guidance_and_invalidates_cache(self):
        import subscription_suggestions as suggestions
        conn = self.dbmodule.get_db()
        self.addCleanup(conn.close)
        with patch.object(suggestions.mailbox_settings, 'load_settings', return_value={}):
            before = suggestions._context_key(conn)
            notes.save_card_instructions('subscription', self.identifier, 'Keep these requested updates.')
            self.assertNotEqual(suggestions._context_key(conn), before)
            rows = conn.execute('SELECT * FROM unsubscribe_candidates').fetchall()
            context = suggestions.build_context(conn, rows, include_excerpts=False)
        self.assertEqual(context['trusted_owner_preferences']['exact_sender_guidance'],
                         [{'sender_email':'updates@example.com','instructions':'Keep these requested updates.'}])

    def test_empty_oversized_and_control_instructions_rejected(self):
        for text in ('', 'x'*4001, 'hello\0secret'):
            with self.assertRaises(ValueError):
                notes.save_card_instructions('subscription', self.identifier, text)
