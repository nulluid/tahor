from test_web_security import AppTestCase


class SavedSubscriptionChoiceTests(AppTestCase):
    def setUp(self):
        super().setUp()
        db=self.module.tahor_db.get_db()
        with db:db.execute("DELETE FROM unsubscribe_seen")
        db.close()

    def add(self,domain='shop.example',identifier='<first@example.com>',marketing=True,received='2026-09-17T12:00:00+00:00'):
        self.module.tahor_db.upsert_unsubscribe_candidate(domain,'news@'+domain,'Example brand',None,None,False,message_id=identifier,received_at=received,is_marketing=marketing)

    def test_saved_marketing_block_settles_existing_and_future_candidates(self):
        module=self.module.tahor_db
        self.add();module.set_sender_rule('shop.example','block_marketing')
        self.assertEqual(module.get_unsubscribe_candidate('shop.example')['status'],'resolved')
        self.add(identifier='<later@example.com>')
        self.assertEqual(module.get_unsubscribe_candidate('shop.example')['status'],'resolved')
        module.set_sender_rule('new.example','block_marketing')
        self.add('new.example')
        self.assertEqual(module.get_unsubscribe_candidate('new.example')['status'],'resolved')
        self.assertEqual(module.get_sender_rule('shop.example'),'block_marketing')

    def test_startup_repairs_pending_exact_block_but_never_similar_brands(self):
        module=self.module.tahor_db
        self.add();self.add('other.shop.example')
        module.set_sender_rule('shop.example','block_marketing')
        db=module.get_db()
        with db:db.execute("UPDATE unsubscribe_candidates SET status='pending' WHERE sender_domain='shop.example'")
        db.close();module.init_db()
        self.assertEqual(module.get_unsubscribe_candidate('shop.example')['status'],'resolved')
        self.assertEqual(module.get_unsubscribe_candidate('other.shop.example')['status'],'pending')
        page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn('This is a different sending domain: other.shop.example',page)

    def test_unsubscribe_only_still_flags_new_marketing_not_old_backlog_or_receipts(self):
        module=self.module.tahor_db;self.add()
        db=module.get_db()
        with db:db.execute("UPDATE unsubscribe_candidates SET status='unsubscribed',unsubscribed_at='2026-09-17T10:00:00+00:00'")
        db.close()
        self.add(identifier='<old@example.com>',received='2026-09-16T12:00:00+00:00')
        self.add(identifier='<receipt@example.com>',marketing=False)
        self.assertEqual(module.get_unsubscribe_candidate('shop.example')['status'],'unsubscribed')
        self.add(identifier='<new-marketing@example.com>')
        row=module.get_unsubscribe_candidate('shop.example')
        self.assertEqual(row['status'],'pending');self.assertEqual(row['non_compliant'],1)
        page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn('Mail after an unsubscribe request',page)
