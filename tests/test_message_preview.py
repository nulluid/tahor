"""Modal previews preserve the user's working page and mailbox action boundaries."""
import json
import os
import shutil
import subprocess
import unittest
import message_preview
from test_web_security import AppTestCase


class PreviewInjectionTests(AppTestCase):
    def test_working_pages_include_preview_dialog_handler(self):
        for path in ('/', '/unsubscribe'):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn('data-email-preview', page)
            self.assertIn('dialog.showModal()', page)


class PreviewBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for modal DOM tests')
    def test_modal_navigation_preserves_choices_and_posts_only_readonly_search(self):
        script = message_preview.SCRIPT.removeprefix('<script data-email-preview>').removesuffix('</script>')
        harness = r'''
const assert=require('node:assert/strict');
const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const dom=new JSDOM('<main><input type="radio" name="choice" value="keep" checked><a id="open" href="/subscription-messages/1">View emails</a><textarea>unfinished rule</textarea></main>',{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window;const calls=[];const scroll=[];
w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
w.scrollTo=(x,y)=>scroll.push(y);
Object.defineProperty(w,'scrollY',{value:700});
w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));
w.eval(SCRIPT);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const response=(url,body)=>({ok:true,url,text:async()=>'<main>'+body+'</main>'});
(async()=>{
 w.document.querySelector('#open').click();
 assert.ok(w.document.querySelector('dialog').open);assert.equal(w.document.body.style.overflow,'hidden');
 assert.equal(calls[0].url,'https://example.test/subscription-messages/1');
 calls[0].resolve(response(calls[0].url,'<header>Unneeded navigation</header><a id="example" href="/subscription-message/1/2">Example</a><form action="/subscription-messages/1" method="post"><input name="csrf_token" value="csrf"><button>Find more</button></form>'));
 await settle();assert.equal(w.document.querySelector('dialog header'),null);
 const form=w.document.querySelector('dialog form');form.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:form.querySelector('button')}));
 assert.equal(calls[1].options.method,'POST');assert.equal(calls[1].options.body.get('csrf_token'),'csrf');
 calls[1].resolve(response(calls[1].url,'<a id="example" href="/subscription-message/1/2">Example</a>'));await settle();
 w.document.querySelector('#example').click();assert.equal(calls[2].url,'https://example.test/subscription-message/1/2');
 calls[2].resolve(response(calls[2].url,'<h1>Example email</h1><pre>&lt;script&gt;not executable&lt;/script&gt;</pre>'));await settle();
 assert.equal(w.document.querySelector('dialog pre').textContent,'<script>not executable</script>');
 assert.equal(w.document.querySelector('input[name="choice"]').checked,true);
 assert.equal(w.document.querySelector('textarea').value,'unfinished rule');
 w.document.querySelector('#open').click();
 calls[3].resolve({ok:false,status:409,url:calls[3].url,text:async()=> 'Searching for this moved message. Reopen to continue from the saved position.'});await settle();
 assert.match(w.document.querySelector('.email-preview-content').textContent,/saved position/);
 w.document.querySelector('dialog button').click();
 assert.equal(w.document.body.style.overflow,'');assert.equal(scroll.at(-1),700);
 assert.equal(w.document.activeElement.id,'open');assert.ok(calls[2].options.signal.aborted);
 w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', 'const SCRIPT=' + json.dumps(script) + ';\n' + harness], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
