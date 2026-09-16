from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_status


class StatusTests(unittest.TestCase):
    def test_state_and_last_success_survive_updates(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TAHOR_STATUS_PATH': str(Path(directory) / 'status.json')}):
            runtime_status.write_status('processed', last_success_at='a-success', last_batch_applied=50)
            runtime_status.write_status('classifying')
            self.assertEqual(runtime_status.read_status()['last_success_at'], 'a-success')
            self.assertIn('Classifying', runtime_status.describe_status())

    def test_stale_worker_is_not_reported_as_healthy(self):
        snapshot = {'state': 'processed', 'updated_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
        self.assertIn('30 minutes', runtime_status.describe_status(snapshot))


if __name__ == '__main__':
    unittest.main()
