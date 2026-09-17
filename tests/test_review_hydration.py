import json
from unittest.mock import patch, Mock
from test_web_security import AppTestCase
import message_reviews


class ReviewHydrationTests(AppTestCase):
    def review(self, identifier, **metadata):
        db=self.module.tahor_db.get_db()
        with db:
            row=db.execute("INSERT INTO decisions(kind,summary,context,status,created_at) VALUES('message_review','',?,'pending','2026-09-01')",(json.dumps(dict(mailbox='INBOX',message_id=identifier,**metadata)),))
        db.close()
        return row.lastrowid

    def test_missing_first_message_does_not_starve_later_cards(self):
        first=self.review('<missing@example.com>');second=self.review('<available@example.com>')
        db=self.module.tahor_db.get_db()
        def locate(context,**kwargs):
            self.assertTrue(kwargs['bounded']);self.assertLessEqual(kwargs['budget_seconds'],10)
            if context['message_id']=='<missing@example.com>':raise RuntimeError('Missing')
            return {'subject':'Example','sender':'Writer <writer@example.com>','received_at':'2026-09-01T12:00:00+00:00','uid':'7','uidvalidity':'42'}
        with patch.object(message_reviews,'locate',side_effect=locate) as lookup:
            self.assertEqual(message_reviews.hydrate_pending(db,limit=1),{'attempted':1,'updated':0})
            self.assertEqual(message_reviews.hydrate_pending(db,limit=1),{'attempted':1,'updated':1})
            self.assertEqual(lookup.call_count,2)
        context=json.loads(db.execute('SELECT context FROM decisions WHERE id=?',(second,)).fetchone()[0])
        self.assertEqual(context['sender'],'Writer <writer@example.com>');self.assertTrue(context['details_loaded'])
        db.close()

    def test_moved_message_search_cursor_is_durable_and_never_resolves_card(self):
        identifier=self.review('<moved@example.com>');db=self.module.tahor_db.get_db()
        def searching(context,**kwargs):
            context['review_search']={'next_index':3,'folders':'hash','matches':[]}
            raise message_reviews.LookupPending('Continue later')
        with patch.object(message_reviews,'locate',side_effect=searching):
            self.assertEqual(message_reviews.hydrate_pending(db),{'attempted':1,'updated':0})
        row=db.execute('SELECT * FROM decisions WHERE id=?',(identifier,)).fetchone()
        self.assertEqual(json.loads(row['context'])['review_search']['next_index'],3)
        self.assertEqual(row['status'],'pending');self.assertIsNone(row['resolution']);db.close()

    def test_loaded_cards_and_invalid_context_do_not_use_network(self):
        self.review('<done@example.com>',details_loaded=True)
        db=self.module.tahor_db.get_db()
        with patch.object(message_reviews,'locate') as lookup:
            self.assertEqual(message_reviews.hydrate_pending(db),{'attempted':0,'updated':0});lookup.assert_not_called()
        db.close()

    def test_deadline_wrapper_bounds_commands_and_stops_at_deadline(self):
        client=Mock();wrapper=message_reviews._DeadlineMailbox(client,100)
        with patch.object(message_reviews.time,'monotonic',return_value=98):
            wrapper.uid('SEARCH',None,'ALL')
        client.sock.settimeout.assert_called_with(2)
        with patch.object(message_reviews.time,'monotonic',return_value=101),self.assertRaises(message_reviews.LookupPending):
            wrapper.uid('FETCH','7','BODY.PEEK[]')
        self.assertEqual(client.uid.call_count,1)

    def test_body_preview_uses_one_bounded_budget_and_does_not_connect_after_expiry(self):
        details={'mailbox':'INBOX','uid':'7','uidvalidity':'42'}
        with patch.object(message_reviews,'locate',return_value=details) as locate,patch.object(message_reviews.fetch_batch,'connect') as connect,patch.object(message_reviews.time,'monotonic',side_effect=[0,13]):
            with self.assertRaises(message_reviews.LookupPending):message_reviews.read_message({'mailbox':'INBOX','message_id':'<one@example.com>'})
            locate.assert_called_once_with({'mailbox':'INBOX','message_id':'<one@example.com>'},budget_seconds=8,bounded=True)
            connect.assert_not_called()
