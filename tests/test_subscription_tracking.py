from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tahor_db


class SubscriptionTrackingTests(unittest.TestCase):
    def test_schema_initialization_can_run_from_multiple_services(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: tahor_db.init_db(), range(12)))
        self.assertEqual(tahor_db.get_unsubscribe_candidate('example.com')['status'], 'unsubscribed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(tahor_db, 'DB_PATH', Path(self.temp.name) / 'db.sqlite')
        patcher.start()
        self.addCleanup(patcher.stop)
        tahor_db.init_db()
        self.observe('initial', '2026-01-01T00:00:00+00:00')
        conn = tahor_db.get_db()
        with conn:
            conn.execute("UPDATE unsubscribe_candidates SET status='unsubscribed',unsubscribed_at='2026-02-01T00:00:00+00:00'")
        conn.close()

    def observe(self, message_id, received_at, marketing=True):
        tahor_db.upsert_unsubscribe_candidate('example.com','news@example.com','Example','https://example.com/unsubscribe',None,False,message_id,received_at,marketing)

    def test_old_backlog_and_receipts_do_not_resurface_unsubscribes(self):
        self.observe('old', '2026-01-15T00:00:00+00:00')
        self.observe('receipt', '2026-02-15T00:00:00+00:00', marketing=False)
        row = tahor_db.get_unsubscribe_candidate('example.com')
        self.assertEqual(row['status'], 'unsubscribed')
        self.assertEqual(row['non_compliant'], 0)

    def test_new_marketing_after_request_resurfaces_once(self):
        self.observe('new', '2026-02-15T00:00:00+00:00')
        self.observe('new', '2026-02-15T00:00:00+00:00')
        row = tahor_db.get_unsubscribe_candidate('example.com')
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['non_compliant'], 1)
        self.assertEqual(row['message_count'], 2)

    def test_missing_delivery_date_is_not_evidence_of_new_mail(self):
        self.observe('unknown', None)
        self.assertEqual(tahor_db.get_unsubscribe_candidate('example.com')['status'], 'unsubscribed')
