import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import notifications


class BackupHealthTests(unittest.TestCase):
    def test_verified_receiver_ack_required_and_recovers_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'status.json'
            now = 1000000
            with patch.dict(os.environ, TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS='36', TAHOR_OFFHOST_BACKUP_STATUS=str(path)), \
                    patch.object(notifications.runtime_status, 'read_status', return_value={'state': 'idle'}), \
                    patch.object(notifications.provider_bridge, 'status', return_value={'enabled': False}), \
                    patch('ai_routing.persistent_problems', return_value=[]):
                self.assertIn('backup_stale', notifications.inspect_health({}, now)[0])
                for value in (None, 'private-invalid-value', now - 37*3600, now + 1000, float('nan')):
                    path.write_text(json.dumps({'verified_at': value}))
                    self.assertIn('backup_stale', notifications.inspect_health({}, now)[0])
                path.write_text(json.dumps({'verified_at': now}))
                self.assertNotIn('backup_stale', notifications.inspect_health({}, now)[0])

    def test_monitoring_is_opt_in(self):
        with patch.dict(os.environ, TAHOR_OFFHOST_BACKUP_MAX_AGE_HOURS=''), \
                patch.object(notifications.runtime_status, 'read_status', return_value={'state': 'idle'}), \
                patch.object(notifications.provider_bridge, 'status', return_value={'enabled': False}), \
                patch('ai_routing.persistent_problems', return_value=[]):
            self.assertNotIn('backup_stale', notifications.inspect_health({}, 1000000)[0])
