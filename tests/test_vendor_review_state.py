import json
from unittest.mock import patch
from test_web_security import AppTestCase
import vendor_review_state


class VendorCompletionTests(AppTestCase):
    def prepare(self, *, action='trash', applied=True, uid='7', mailbox='INBOX', second=False, provenance=False):
        module = self.module.tahor_db
        module.queue_vendor_mapping('shop.example', metadata={'sender_email':'orders@shop.example','mailbox':'INBOX','message_id':'<one@example.com>','uid':'7','uidvalidity':'42'})
        db = module.get_db()
        vendor = db.execute("SELECT * FROM decisions WHERE kind='vendor_mapping'").fetchone()
        context = json.loads(vendor['context'])
        sample = dict(context['samples'][0])
        if second:
            context['samples'].append(dict(sample, message_id='<two@example.com>', uid='8'))
        identity = dict(sample, uid=uid, mailbox=mailbox, applied=applied)
        if provenance:
            identity['vendor_source_identity'] = {key:sample.get(key) for key in ('mailbox','message_id','uid','uidvalidity')}
        with db:
            db.execute('UPDATE decisions SET context=? WHERE id=?',(json.dumps(context),vendor['id']))
            review = db.execute("INSERT INTO decisions(kind,summary,context,status,resolution,created_at) VALUES('message_review','Example',?,?,?,'2026-09-01')",(json.dumps(identity),'resolved' if action else 'pending',json.dumps({'action':action}) if action else None)).lastrowid
        return db, vendor['id'], review, sample

    def test_completed_legacy_sample_disappears_and_repeated_action_is_idempotent(self):
        db, vendor, review, _ = self.prepare()
        with patch.object(self.module.apply_decisions,'apply_one') as apply:
            response=self.client.post(f'/vendor-message/{vendor}/0', data={'action':'trash','csrf_token':self.token()})
            self.assertEqual(response.status_code,302)
            apply.assert_not_called()
        self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?',(vendor,)).fetchone()[0],'resolved')
        self.assertNotIn(f'data-decision-id="{vendor}"',self.client.get('/').get_data(as_text=True))
        self.assertEqual(db.execute("SELECT count(*) FROM decisions WHERE kind='message_review'").fetchone()[0],1)
        db.close()

    def test_completed_other_action_is_preserved_and_remaining_indices_are_stable(self):
        db,vendor,review,_=self.prepare(action='keep_brief',second=True)
        with patch.object(self.module.apply_decisions,'apply_one') as apply:
            self.client.post(f'/vendor-message/{vendor}/0',data={'action':'trash','csrf_token':self.token()})
            apply.assert_not_called()
        page=self.client.get('/').get_data(as_text=True)
        self.assertNotIn(f'action="/vendor-message/{vendor}/0"',page)
        self.assertIn(f'action="/vendor-message/{vendor}/1"',page)
        self.assertEqual(json.loads(db.execute('SELECT resolution FROM decisions WHERE id=?',(review,)).fetchone()[0])['action'],'keep_brief')
        self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?',(vendor,)).fetchone()[0],'pending')
        db.close()

    def test_same_message_id_different_known_uid_is_never_used(self):
        for action in (None,'trash'):
            with self.subTest(action=action):
                db,vendor,review,sample=self.prepare(action=action,uid='99')
                self.assertIsNone(vendor_review_state.sample_review(db,sample)[0])
                with patch.object(self.module.apply_decisions,'apply_one') as apply:
                    response=self.client.post(f'/vendor-message/{vendor}/0',data={'action':'trash','csrf_token':self.token()})
                    self.assertEqual(response.status_code,409)
                    apply.assert_not_called()
                self.assertEqual(db.execute('SELECT status FROM decisions WHERE id=?',(vendor,)).fetchone()[0],'pending')
                with db: db.execute('DELETE FROM decisions')
                db.close()

    def test_cross_folder_copy_requires_source_provenance(self):
        db,vendor,review,sample=self.prepare(mailbox='Trash')
        self.assertIsNone(vendor_review_state.completed_action(db,sample))
        identity=json.loads(db.execute('SELECT context FROM decisions WHERE id=?',(review,)).fetchone()[0])
        identity['vendor_source_identity']={key:sample.get(key) for key in ('mailbox','message_id','uid','uidvalidity')}
        with db: db.execute('UPDATE decisions SET context=? WHERE id=?',(json.dumps(identity),review))
        self.assertEqual(vendor_review_state.completed_action(db,sample),'trash')
        db.close()

    def test_background_success_only_hides_after_confirmed_apply(self):
        db,vendor,review,sample=self.prepare(applied=False)
        page=self.client.get('/').get_data(as_text=True)
        self.assertIn(f'data-decision-id="{vendor}"',page)
        identity=json.loads(db.execute('SELECT context FROM decisions WHERE id=?',(review,)).fetchone()[0]);identity['applied']=True
        with db: db.execute('UPDATE decisions SET context=? WHERE id=?',(json.dumps(identity),review))
        self.assertNotIn(f'data-decision-id="{vendor}"',self.client.get('/').get_data(as_text=True))
        db.close()

    def test_retry_pending_saved_choice_is_not_replaced(self):
        db,vendor,review,_=self.prepare(action='keep_brief',applied=False)
        with db: db.execute("UPDATE decisions SET status='pending' WHERE id=?",(review,))
        with patch.object(self.module.apply_decisions,'apply_one') as apply:
            response=self.client.post(f'/vendor-message/{vendor}/0',data={'action':'trash','csrf_token':self.token()})
            self.assertEqual(response.status_code,302)
            apply.assert_not_called()
        self.assertEqual(json.loads(db.execute('SELECT resolution FROM decisions WHERE id=?',(review,)).fetchone()[0])['action'],'keep_brief')
        db.close()
