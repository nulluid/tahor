import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from data_changes import atomic_write
from provider_connector.service import read_desired, once
from provider_connector.rules import digest
from provider_connector.auth import ConnectorError
from test_web_security import AppTestCase
import provider_bridge


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('inbox', 'outbox', 'private'):
            (self.root/name).mkdir(mode=0o700)
        self.config = {'bridge': str(self.root), 'state': str(self.root/'private'),
                       'request_uid': os.getuid(), 'credentials': str(self.root/'missing'), 'installation': 'a'*32}
        self.path = self.root/'inbox/desired.json'
        self.desired = {'version': 1, 'enabled': False, 'domains': [], 'digest': digest([])}
        self.write()

    def write(self):
        atomic_write(self.path, json.dumps(self.desired))
        self.path.chmod(0o640)

    def test_disabled_does_not_load_credentials_or_call_provider(self):
        with patch('provider_connector.service.FastmailAuth') as auth:
            self.assertEqual(once(self.config)['state'], 'disabled')
            auth.assert_not_called()

    def test_rejects_wrong_owner_links_permissions_and_extra_operations(self):
        with self.assertRaises(ValueError):
            read_desired(self.path, os.getuid()+1)
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            read_desired(self.path, os.getuid())
        self.write()
        self.desired['url'] = 'https://evil.test'
        self.write()
        with patch('provider_connector.service.FastmailAuth') as auth:
            self.assertEqual(once(self.config)['state'], 'invalid_request')
            auth.assert_not_called()
        self.path.unlink()
        self.path.symlink_to(self.root/'private')
        with self.assertRaises(OSError):
            read_desired(self.path, os.getuid())

    def test_credentials_and_exception_bodies_never_enter_status(self):
        self.desired['enabled'] = True
        self.write()
        for error in (ConnectorError('secret-password response'), RuntimeError('secret-password response')):
            with patch('provider_connector.service.FastmailAuth', side_effect=error):
                result = once(self.config)
            self.assertEqual(result['state'], 'provider_unavailable')
            self.assertNotIn('secret', (self.root/'outbox/status.json').read_text())

    def test_status_rejects_unknown_labels_and_detects_stale_or_outdated_results(self):
        self.desired['enabled'] = True
        self.write()
        with patch.dict(os.environ, TAHOR_PROVIDER_BRIDGE=str(self.root)):
            for result, expected in [
                ({'state': '<script>secret</script>', 'updated_at': time.time(), 'digest': digest([])}, 'provider_unavailable'),
                ({'state': 'installed', 'updated_at': 0, 'digest': digest([])}, 'stale'),
                ({'state': 'installed', 'updated_at': time.time(), 'digest': 'wrong'}, 'pending'),
            ]:
                atomic_write(self.root/'outbox/status.json', json.dumps(result))
                self.assertEqual(provider_bridge.status()['state'], expected)


class ProviderWebTests(AppTestCase):
    def test_opt_in_requires_owner_csrf_and_real_bridge(self):
        anonymous = self.module.app.test_client()
        self.assertIn(anonymous.post('/provider-sync', data={'enabled': '1'}).status_code, (302, 400, 403))
        self.assertEqual(self.client.post('/provider-sync', data={'enabled': '1'}).status_code, 400)
        with patch.dict(os.environ, TAHOR_PROVIDER_BRIDGE=''):
            self.assertEqual(self.client.post('/provider-sync', data={'enabled': '1', 'csrf_token': self.token()}).status_code, 503)

    def test_enable_and_disable_write_only_desired_domain_rules(self):
        bridge = self.root/'bridge'
        (bridge/'inbox').mkdir(parents=True, exist_ok=True)
        (bridge/'outbox').mkdir(exist_ok=True)
        with patch.dict(os.environ, TAHOR_PROVIDER_BRIDGE=str(bridge)):
            token = self.token()
            for enabled in ('1', '0'):
                response = self.client.post('/provider-sync', data={'enabled': enabled, 'csrf_token': token})
                self.assertEqual(response.status_code, 302)
                value = json.loads((bridge/'inbox/desired.json').read_text())
                self.assertEqual(value, {'version': 1, 'enabled': enabled == '1', 'domains': [], 'digest': digest([])})
            self.assertNotIn('password', json.dumps(value))
            self.assertEqual(self.client.post('/provider-sync', data={'enabled': 'maybe', 'csrf_token': token}).status_code, 400)
