import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class AppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.env = patch.dict(os.environ, {
            'TAHOR_DB_PATH': str(cls.root / 'decisions.db'),
            'TAHOR_SETTINGS_PATH': str(cls.root / 'settings.json'),
            'ALLOWED_EMAIL': 'owner@example.com',
            'DATA_DIR': str(cls.root),
        })
        cls.env.start()
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / 'decision-app'))
        # Other test modules may already have loaded shared modules.
        cls.saved = {key: sys.modules.pop(key, None) for key in ('config', 'mailbox_settings', 'tahor_db', 'apply_decisions', 'generate_sieve', 'runtime_status', 'reply_rules', 'vendor_inventory', 'vendor_suggestions', 'subscription_suggestions')}
        spec = importlib.util.spec_from_file_location('security_app', ROOT / 'decision-app/app.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.module.app.config['TESTING'] = True
        cls.module.init_db()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.saved.items():
            sys.modules.pop(key, None)
            if value is not None:
                sys.modules[key] = value
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session['email'] = 'owner@example.com'
        db = self.module.tahor_db.get_db()
        for table in ('decisions', 'unsubscribe_candidates', 'reply_drafts', 'sender_rules', 'reply_rule_matches'):
            db.execute('DELETE FROM ' + table)
        db.commit()
        db.close()

    def token(self):
        self.client.get('/settings')
        with self.client.session_transaction() as session:
            return session['csrf_token']


class WebSecurityTests(AppTestCase):
    def test_oauth_requires_verified_owner_and_consumes_state(self):
        for email, verified, expected in [('other@example.com', True, 403), ('owner@example.com', False, 403), ('owner@example.com', True, 302)]:
            client = self.module.app.test_client()
            with client.session_transaction() as session:
                session['oauth_state'] = 'one-use-state'
            token = Mock()
            token.json.return_value = {'access_token': 'synthetic-token'}
            user = Mock()
            user.json.return_value = {'email': email, 'email_verified': verified}
            with patch.object(self.module.requests, 'post', return_value=token), patch.object(self.module.requests, 'get', return_value=user):
                self.assertEqual(client.get('/auth/google/callback?state=one-use-state&code=test').status_code, expected)
                self.assertEqual(client.get('/auth/google/callback?state=one-use-state&code=test').status_code, 400)
            with client.session_transaction() as session:
                self.assertEqual(session.get('email'), 'owner@example.com' if expected == 302 else None)

    def test_every_mutation_requires_login_and_a_form_token(self):
        anonymous = self.module.app.test_client()
        with anonymous.session_transaction() as session:
            session['csrf_token'] = 'valid-test-token'
        for rule in self.module.app.url_map.iter_rules():
            if 'POST' not in rule.methods:
                continue
            path = re.sub(r'<[^>]+>', '1', rule.rule)
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 400)
                response = anonymous.post(path, data={'csrf_token': 'valid-test-token'})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith('/login'))

    def test_post_requires_csrf(self):
        with patch.object(self.module.mailbox_settings, 'set_classify_mode') as setter:
            self.assertEqual(self.client.post('/settings', data={'classify_mode': 'paid'}).status_code, 400)
            setter.assert_not_called()
            self.assertEqual(self.client.post('/settings', data={'classify_mode': 'paid', 'csrf_token': self.token()}).status_code, 302)
            setter.assert_called_once_with('paid')

    def test_forms_have_token_and_private_cache_headers(self):
        for path in ('/', '/settings', '/unsubscribe'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            self.assertEqual(text.count('<form '), text.count('name="csrf_token"'))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_mail_and_sieve_are_escaped(self):
        attack = '<img src=x onerror=alert(1)>'
        db = self.module.tahor_db.get_db()
        db.execute("INSERT INTO decisions(kind,summary,context,created_at) VALUES ('vendor_mapping',?,?, 'now')", (attack, attack))
        db.execute("INSERT INTO decisions(kind,summary,resolution,created_at) VALUES ('legacy','old','broken JSON','now')")
        db.commit()
        db.close()
        self.module.SIEVE_PATH.write_text(attack)
        page = self.client.get('/').get_data(as_text=True)
        self.assertNotIn(attack, page)
        self.assertIn('&lt;img', page)
        self.module.tahor_db.create_reply_draft('message', 'thread', attack, attack, attack, 'test')
        page = self.client.get('/drafts').get_data(as_text=True)
        self.assertNotIn(attack, page)
        self.assertNotIn('Draft text', page)
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', attack, attack, None, 'test@example.com', False)
        page = self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertNotIn(attack, page)

    def test_oauth_missing_state_never_contacts_provider(self):
        with patch.object(self.module.requests, 'post') as post:
            self.assertEqual(self.client.get('/auth/google/callback?code=test').status_code, 400)
            post.assert_not_called()

    def test_unknown_unsubscribe_action_cannot_resolve(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('example.com', '', '', None, None, False)
        candidate = self.module.tahor_db.get_unsubscribe_candidate('example.com')
        response = self.client.post('/unsubscribe/' + str(candidate['id']), data={'action': 'bogus', 'csrf_token': self.token()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.module.tahor_db.get_unsubscribe_candidate('example.com')['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
