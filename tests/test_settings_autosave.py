import json
from pathlib import Path
import shutil
import subprocess
import unittest

from test_web_security import AppTestCase
import settings_autosave


class SettingsAutosaveTests(AppTestCase):
    def post_json(self, path, **data):
        return self.client.post(path, data=dict(csrf_token=self.token(), **data), headers={'Accept': 'application/json'})

    def test_ordinary_settings_have_no_save_buttons_and_actions_stay_explicit(self):
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertNotIn('Save AI settings', page)
        self.assertNotIn('Save inbox timing', page)
        self.assertIn('data-autosave', page)
        self.assertIn('Save reply rule', page)
        self.assertIn('action="/provider-sync"', page)
        self.assertIn('aria-live="polite"', page)

    def test_ai_policy_saves_without_redirect_and_preserves_other_sections(self):
        before = self.module.mailbox_settings.get_ai_policy('reply')
        response = self.post_json('/settings', ai_task='classification', ai_policy='paid_only')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'saved': True, 'enabled': True})
        self.assertEqual(self.module.mailbox_settings.get_classify_mode(), 'paid_only')
        self.assertEqual(self.module.mailbox_settings.get_ai_policy('reply'), before)

    def test_timing_validation_reports_json_and_does_not_write_bad_values(self):
        self.assertEqual(self.post_json('/settings', inbox_grace='1', inbox_read_days='3', inbox_unread_days='7').json, {'saved': True})
        response = self.post_json('/settings', inbox_grace='1', inbox_read_days='-1', inbox_unread_days='5')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json['saved'])
        self.assertEqual(self.module.mailbox_settings.get_inbox_grace_days(), {'read': 3, 'unread': 7})

    def test_existing_rule_edits_autosave_but_new_rule_still_requires_submit(self):
        rules = self.module.reply_rules
        identifier = rules.save_rule('Updates', 'sender_email', 'writer@example.com', 'Thank them.', 'Regards')
        response = self.post_json('/reply-rules/save', rule_id=identifier, name='Personal updates',
            match_type='sender_email', match='writer@example.com', instructions='Reply briefly.', signature='Best wishes', max_sentences='3')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rule']['name'], 'Personal updates')
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('action="/reply-rules/save" data-autosave', page)
        self.assertNotIn('Save changes', page)
        self.assertIn('Save reply rule', page)

    def test_json_posts_still_require_owner_and_csrf(self):
        self.assertEqual(self.client.post('/settings', data={'inbox_grace':'1'}, headers={'Accept':'application/json'}).status_code, 400)
        self.assertEqual(self.module.app.test_client().post('/settings', data={'csrf_token': 'invalid'}, headers={'Accept':'application/json'}).status_code, 400)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the client event-loop test')
    def test_browser_queue_serializes_requests_and_coalesces_latest_intent(self):
        script = settings_autosave.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const status = {textContent: ''};
const retry = {hidden: true, addEventListener(type, fn) { this[type] = fn; }};
const form = {action:'/settings', values:{csrf_token:'secret-form-token',ai_task:'classification',ai_policy:'paid'}, listeners:{},
 querySelector(selector) { if(selector === '.autosave-status') return status; if(selector === '.autosave-retry') return retry; return null; },
 querySelectorAll() { return []; }, checkValidity() { return true; }, setAttribute() {}, removeAttribute() {},
 addEventListener(type, fn) { this.listeners[type] = fn; }};
class FormData { constructor(form) { this.data = Object.entries(form.values); } [Symbol.iterator]() { return this.data[Symbol.iterator](); } }
const requests = [];
const context = {FormData, URLSearchParams, Map, Set, setTimeout, clearTimeout,
 document:{querySelectorAll(){return [form];}}, window:{addEventListener(){},location:{reload(){}}},
 fetch(url, options){return new Promise(resolve => requests.push({options,resolve}));}};
vm.runInNewContext(SCRIPT, context);
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = {ok:true, redirected:false, headers:{get(){return 'application/json';}},async json(){return {saved:true, enabled:true};}};
(async () => {
 form.values.ai_policy='free'; form.listeners.change();
 form.values.ai_policy='paid_only'; form.listeners.change();
 form.values.ai_policy='auto'; form.listeners.change();
 assert.equal(requests.length,1);
 assert.equal(new URLSearchParams(requests[0].options.body).get('ai_policy'),'free');
 assert.equal(new URLSearchParams(requests[0].options.body).get('csrf_token'),'secret-form-token');
 requests[0].resolve(response); await settle();
 assert.equal(requests.length,2);
 assert.equal(new URLSearchParams(requests[1].options.body).get('ai_policy'),'auto');
 assert.notEqual(status.textContent,'Saved');
 requests[1].resolve(response); await settle();
 assert.equal(status.textContent,'Saved');
 assert.equal(requests.length,2);
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        subprocess.run([shutil.which('node'), '-e', 'const SCRIPT='+json.dumps(script)+';\n'+harness], check=True, timeout=15, capture_output=True, text=True)
