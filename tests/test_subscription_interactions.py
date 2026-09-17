"""Real browser DOM checks for inert choices and explicit durable batches."""
import json
import os
import shutil
import subprocess
import unittest
import subscription_bulk_ui


class SubscriptionInteractionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for real DOM subscription tests')
    def test_choices_ai_and_bulk_preserve_manual_intent_without_early_execution(self):
        harness=r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card=id=>`<div data-subscription-id="${id}"><p class="subscription-result"></p><input type="radio" name="choice-${id}" value="" checked><input type="radio" name="choice-${id}" value="unsubscribe"><input type="radio" name="choice-${id}" value="dismiss"></div>`;
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+card(1)+card(2),{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window;const calls=[];w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));w.eval(SCRIPT);
const settle=()=>new Promise(resolve=>setImmediate(resolve));const respond=(call,data)=>call.resolve({ok:true,json:async()=>data});
(async()=>{
 assert.equal(calls[0].url,'/unsubscribe/batches');respond(calls[0],[]);await settle();
 const manual=w.document.querySelector('[data-subscription-id="1"] input[value="dismiss"]');manual.checked=true;manual.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.equal(calls.length,1,'radio selection must never execute requests');
 w.document.querySelector('[data-generate]').click();assert.equal(calls[1].url,'/unsubscribe/suggestions');assert.deepEqual(JSON.parse(calls[1].options.body.get('exclude_ids')),[1]);
 respond(calls[1],{job_id:'suggestions',status:'complete',recommendations:[{candidate_id:1,action:'unsubscribe',reason:'Example'},{candidate_id:2,action:'unsubscribe',reason:'Example'}]});await settle();
 assert.equal(w.document.querySelector('[data-subscription-id="1"] input:checked').value,'dismiss');
 assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'unsubscribe');
 assert.equal(calls.length,2,'AI suggestions must never execute unsubscribe');
 w.document.querySelector('[data-apply]').click();w.document.querySelector('[data-apply]').click();
 assert.equal(calls.length,3);assert.equal(calls[2].url,'/unsubscribe/batches');
 const payload=JSON.parse(calls[2].options.body.get('selections'));assert.deepEqual(payload,[{candidate_id:1,action:'dismiss'},{candidate_id:2,action:'unsubscribe'}]);assert.equal(calls[2].options.body.get('csrf_token'),'csrf');
 respond(calls[2],{job_id:'batch',status:'complete',items:[{candidate_id:1,status:'done',message:'Kept'},{candidate_id:2,status:'attention',message:'Needs attention'}]});await settle();
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'0');
 assert.match(w.document.querySelector('[data-subscription-id="2"] .subscription-result').textContent,/Needs attention/);
 assert.equal(w.location.pathname,'/unsubscribe');w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        run=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';\n'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
