import json
from unittest.mock import patch
from test_web_security import AppTestCase


class RuleClarificationUITests(AppTestCase):
    def seed(self, **extra):
        context = {'rule_clarification': {'question': 'Which exact domain? <script>unsafe</script>'}}
        context.update(extra)
        resolution = {'action': 'free_text_rule', 'text': 'Block <Brand> marketing, but keep receipts.'}
        db = self.module.tahor_db.get_db()
        with db:
            cursor = db.execute("INSERT INTO decisions(kind,summary,context,status,resolution,created_at) VALUES('free_text_rule','Rule',?,'pending',?,'2026-01-01')", (json.dumps(context), json.dumps(resolution)))
        row = db.execute('SELECT * FROM decisions WHERE id=?', (cursor.lastrowid,)).fetchone()
        db.close()
        return row

    def post(self, row, **data):
        return self.client.post('/clarify-rule/' + str(row['id']), data=dict(csrf_token=self.token(), revision=self.module.rule_revision(row), rule_text='Block marketing from alerts.example; keep receipts.', **data))

    def test_original_instruction_and_question_are_escaped_and_retry_replaced(self):
        row = self.seed()
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Block &lt;Brand&gt; marketing, but keep receipts.', page)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', page)
        self.assertIn('Resubmit instruction', page)
        self.assertNotIn('/retry-rule/' + str(row['id']), page)
        with patch.object(self.module.apply_decisions, 'apply_one') as apply:
            response = self.client.post('/retry-rule/' + str(row['id']), data={'csrf_token': self.token()})
        self.assertEqual(response.status_code, 409)
        apply.assert_not_called()

    def test_resubmit_preserves_full_instruction_and_generates_review_not_application(self):
        row = self.seed()
        result = {'kind': 'sender_rule', 'sender_rule': {'domain': 'alerts.example', 'rule': 'block_marketing', 'attempt_unsubscribe': False}}
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value=result), patch.object(self.module.apply_decisions, 'apply_sender_rule') as apply:
            self.assertEqual(self.post(row).status_code, 302)
            apply.assert_not_called()
        db = self.module.tahor_db.get_db()
        changed = db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone()
        db.close()
        self.assertEqual(changed['status'], 'pending')
        self.assertEqual(json.loads(changed['resolution'])['text'], 'Block marketing from alerts.example; keep receipts.')
        self.assertNotIn('rule_clarification', json.loads(changed['context']))
        self.assertIn('rule_proposal', json.loads(changed['context']))
        self.assertEqual(self.post(row).status_code, 409)

    def test_retry_that_discovers_missing_scope_stays_pending(self):
        row = self.seed()
        db = self.module.tahor_db.get_db()
        with db:
            db.execute("UPDATE decisions SET context='{}' WHERE id=?", (row['id'],))
        db.close()
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value={'kind': 'needs_clarification', 'explanation': 'Which domain?'}):
            response = self.client.post('/retry-rule/' + str(row['id']), data={'csrf_token': self.token()})
        self.assertEqual(response.status_code, 302)
        db = self.module.tahor_db.get_db()
        stored = db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone()
        db.close()
        self.assertEqual(stored['status'], 'pending')
        self.assertIn('rule_clarification', json.loads(stored['context']))
        self.assertEqual(json.loads(stored['resolution'])['text'], json.loads(row['resolution'])['text'])

    def test_applied_or_approved_or_proposed_cannot_be_edited(self):
        for extra in ({'applied': True}, {'rule_proposal': {'token': 'existing'}}):
            row = self.seed(**extra)
            self.assertEqual(self.post(row).status_code, 409)
        row = self.seed()
        db = self.module.tahor_db.get_db()
        with db:
            db.execute('UPDATE decisions SET resolution=? WHERE id=?', (json.dumps({'text': 'original', 'approved_proposal': 'approved'}), row['id']))
        db.close()
        self.assertEqual(self.post(row).status_code, 409)

    def test_authentication_csrf_and_blank_instruction(self):
        row = self.seed()
        url = '/clarify-rule/' + str(row['id'])
        self.assertEqual(self.client.post(url, data={'rule_text': 'new'}).status_code, 400)
        self.assertEqual(self.client.post(url, data={'rule_text': ' ', 'revision': self.module.rule_revision(row), 'csrf_token': self.token()}).status_code, 400)
        with self.client.session_transaction() as session:
            session.pop('email', None)
        self.assertIn(self.post(row).status_code, (302, 403))

    def test_interleaved_change_cannot_be_overwritten_or_generated(self):
        row = self.seed()
        token = self.token()
        real = self.module.tahor_db.get_db()
        class InterleavedConnection:
            changed = False
            def execute(self, sql, parameters=()):
                if sql.startswith('UPDATE decisions SET summary=') and not self.changed:
                    self.changed = True
                    real.execute("UPDATE decisions SET context=? WHERE id=?", (json.dumps({'applied': True}), row['id']))
                    real.commit()
                return real.execute(sql, parameters)
            def __getattr__(self, name):
                return getattr(real, name)
        with patch.object(self.module, 'get_db', return_value=InterleavedConnection()), patch.object(self.module.apply_decisions, 'apply_one') as apply:
            response = self.client.post('/clarify-rule/' + str(row['id']), data={'csrf_token': token, 'revision': self.module.rule_revision(row), 'rule_text': 'Block alerts.example'})
            self.assertEqual(response.status_code, 409)
            apply.assert_not_called()
        self.assertTrue(json.loads(real.execute('SELECT context FROM decisions WHERE id=?', (row['id'],)).fetchone()['context'])['applied'])
        real.close()

    def test_generic_resolve_cannot_overwrite_any_rule_review_state(self):
        for state in ('clarification', 'proposal', 'approved', 'applied'):
            with self.subTest(state=state):
                row = self.seed()
                context = json.loads(row['context'])
                resolution = json.loads(row['resolution'])
                if state != 'clarification':
                    context = {'rule_proposal': {'token': 'saved-proposal'}}
                if state == 'approved':
                    resolution['approved_proposal'] = 'saved-proposal'
                if state == 'applied':
                    context['applied'] = True
                db = self.module.tahor_db.get_db()
                with db:
                    db.execute('UPDATE decisions SET context=?,resolution=? WHERE id=?', (json.dumps(context), json.dumps(resolution), row['id']))
                before = dict(db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone())
                with patch.object(self.module.apply_decisions, 'apply_one') as apply:
                    for action in ('keep', 'trash', 'skip', 'map'):
                        response = self.client.post('/resolve/' + str(row['id']), data={'csrf_token': self.token(), 'action': action})
                        self.assertEqual(response.status_code, 400)
                    apply.assert_not_called()
                after = dict(db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone())
                db.close()
                self.assertEqual(before, after)

    def test_observed_domain_selection_is_explicit_escaped_and_still_requires_review(self):
        row = self.seed()
        self.module.tahor_db.upsert_unsubscribe_candidate('updates.community.example', 'person@updates.community.example', '<img src=x onerror=bad()>', None, None, False)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('value="updates.community.example"', page)
        self.assertIn('&lt;img src=x onerror=bad()&gt;', page)
        self.assertNotIn('<img src=x onerror=bad()>', page)
        result = {'kind': 'sender_rule', 'sender_rule': {'domain': 'updates.community.example', 'rule': 'block_marketing', 'attempt_unsubscribe': False}}
        with patch.object(self.module.apply_decisions, 'rule_model_call', return_value=result), patch.object(self.module.apply_decisions, 'apply_sender_rule') as apply:
            response = self.client.post('/clarify-rule/' + str(row['id']), data={'csrf_token': self.token(), 'revision': self.module.rule_revision(row), 'rule_text': 'Block community marketing; keep receipts.', 'observed_domain': 'updates.community.example'})
            self.assertEqual(response.status_code, 302)
            apply.assert_not_called()
        db = self.module.tahor_db.get_db()
        stored = db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone()
        db.close()
        self.assertEqual(json.loads(stored['resolution'])['text'], 'Block community marketing; keep receipts.\nExact sender domain: updates.community.example.')
        self.assertEqual(stored['status'], 'pending')
        self.assertEqual(json.loads(stored['context'])['rule_proposal']['result']['sender_rule']['domain'], 'updates.community.example')

    def test_forged_or_no_longer_observed_selection_cannot_authorize_a_domain(self):
        row = self.seed()
        self.module.tahor_db.upsert_unsubscribe_candidate('known.example', 'person@known.example', 'Known', None, None, False)
        with patch.object(self.module.apply_decisions, 'apply_one') as apply:
            response = self.post(row, observed_domain='guessed.example')
            self.assertEqual(response.status_code, 400)
            apply.assert_not_called()
        db = self.module.tahor_db.get_db()
        with db:
            db.execute("DELETE FROM unsubscribe_candidates WHERE sender_domain='known.example'")
        with patch.object(self.module.apply_decisions, 'apply_one') as apply:
            self.assertEqual(self.post(row, observed_domain='known.example').status_code, 400)
            apply.assert_not_called()
        stored = db.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone()
        db.close()
        self.assertEqual(dict(stored), dict(row))
