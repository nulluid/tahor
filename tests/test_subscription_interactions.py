"""Exercise independent in-flight subscription actions without a browser dependency."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest


class SubscriptionInteractionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute browser interaction logic')
    def test_parallel_senders_keep_their_own_progress_and_results(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'decision-app/app.py').read_text())
        assignment = next(n for n in source.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SUBSCRIPTION_SCRIPT' for t in n.targets))
        script = ast.literal_eval(assignment.value).removeprefix('<script>').removesuffix('</script>')
        harness = r'''
const assert = require('node:assert/strict');
const calls = [];
function makeForm(id) {
  const result = {textContent: ''};
  const card = {dataset: {}, querySelector: () => result};
  return {action: {shadowedByNamedButtons:true}, getAttribute: name => name === 'action' ? '/unsubscribe/'+id : null, dataset: {}, buttons: [{disabled:false}], result, card,
    addEventListener(name, callback) {this.submit = callback;},
    closest() {return card;}, querySelectorAll() {return this.buttons;},
    setAttribute() {}, removeAttribute() {}};
}
const forms = [makeForm(1), makeForm(2)];
global.document = {querySelectorAll: () => forms};
global.FormData = class {constructor(form) {this.form = form;} set(k,v) {this[k]=v;}};
global.fetch = (url, options) => new Promise(resolve => calls.push({url,options,resolve}));
'''
        checks = r'''
(async () => {
  const event = {preventDefault() {}, submitter: {value:'unsubscribe'}};
  const first = forms[0].submit(event);
  assert.match(forms[0].result.textContent, /Working/);
  assert.equal(forms[0].buttons[0].disabled, true);
  assert.equal(forms[1].buttons[0].disabled, false);
  await forms[0].submit(event);
  assert.equal(calls.length, 1, 'double clicks must not repeat requests');
  const second = forms[1].submit(event);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/unsubscribe/1');
  assert.equal(calls[1].url, '/unsubscribe/2');
  calls[1].resolve({ok:true,headers:{get:()=> 'application/json'},json:async()=>({message:'Second succeeded',pending:false})});
  await second;
  assert.equal(forms[1].result.textContent, 'Second succeeded');
  assert.equal(forms[1].hidden, true);
  assert.match(forms[0].result.textContent, /Working/);
  calls[0].resolve({ok:true,headers:{get:()=> 'application/json'},json:async()=>({message:'First rejected',pending:true})});
  await first;
  assert.equal(forms[0].result.textContent, 'First rejected');
  assert.equal(forms[0].buttons[0].disabled, false);
  assert.equal(forms[0].hidden, undefined);
  assert.equal(calls[0].options.body.action, 'unsubscribe');
  const retry = forms[0].submit(event);
  calls[2].resolve({ok:true,headers:{get:()=> 'text/html'}});
  await retry;
  assert.match(forms[0].result.textContent, /Reload/);
  assert.equal(forms[1].result.textContent, 'Second succeeded');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([shutil.which('node'), '-e', harness + script + checks], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
