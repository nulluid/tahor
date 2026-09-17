"""Multiple subscription batches remain usable without a page reload."""
import json
import os
import shutil
import subprocess
import unittest

import subscription_bulk_ui


@unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for sequential batch DOM tests')
class SequentialSubscriptionBatches(unittest.TestCase):
    def run_browser(self, scenario):
        bootstrap=r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card=id=>`<div data-subscription-id="${id}"><p class="subscription-result"></p><fieldset>${['','unsubscribe_block_marketing','unsubscribe','dismiss'].map(action=>`<label><input type="radio" name="choice-${id}" value="${action}" ${action?'':'checked'}>${action||'No action'}</label>`).join('')}</fieldset></div>`;
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+'<div id="subscription-cards">'+[1,2,3,4].map(card).join('')+'</div>',{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window,calls=[],scrolls=[];Object.defineProperty(w,'scrollY',{value:620});w.scrollBy=(...args)=>scrolls.push(args);w.scrollTo=(...args)=>scrolls.push(args);
w.fetch=(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject}));w.eval(SCRIPT);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const answer=(call,body,status=200)=>call.resolve({ok:status<400,status,json:async()=>body});
const select=(id,action)=>{const radio=w.document.querySelector(`[data-subscription-id="${id}"] input[value="${action}"]`);radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));};
const apply=()=>w.document.querySelector('[data-apply]').click();
const last=()=>calls.at(-1);const payload=call=>JSON.parse(call.options.body.get('selections'));
const key=call=>call.options.body.get('request_key');
const noJump=()=>{assert.equal(w.location.pathname,'/unsubscribe');assert.equal(w.scrollY,620);assert.equal(scrolls.length,0);};
(async()=>{answer(calls[0],[]);await settle();
'''
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        code='const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';\n'+bootstrap+scenario+"\nnoJump();w.close();})().catch(error=>{console.error(error);process.exitCode=1;});"
        result=subprocess.run([shutil.which('node'),'-e',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_resolved_attention_cannot_reenter_select_all_or_second_ai_batch(self):
        self.run_browser(r'''
select(1,'unsubscribe_block_marketing');select(2,'dismiss');apply();const first=last();
assert.deepEqual(payload(first).map(item=>item.candidate_id),[1,2]);
answer(first,{job_id:'first',status:'complete',items:[{candidate_id:1,status:'attention',retry_allowed:false,message:'External request failed; marketing block saved.'},{candidate_id:2,status:'done',retry_allowed:false,message:'Subscription kept.'}]});await settle();
assert.equal(w.document.querySelector('[data-subscription-id="1"] fieldset').hidden,true);
assert.equal(w.document.querySelector('[data-subscription-id="1"] input').disabled,true);
w.document.querySelector('[data-bulk-choice]').value='unsubscribe_block_marketing';w.document.querySelector('[data-select-all]').click();
assert.equal(w.document.querySelector('[data-selected-count]').textContent,'2');
w.document.querySelector('[data-generate]').click();answer(last(),{job_id:'suggestions',status:'complete',recommendations:[{candidate_id:1,action:'unsubscribe',reason:'Stale suggestion'},{candidate_id:3,action:'dismiss',reason:'Suggestion'}]});await settle();
apply();const second=last();assert.notEqual(key(second),key(first));
assert.deepEqual(payload(second),[{candidate_id:3,action:'unsubscribe_block_marketing'},{candidate_id:4,action:'unsubscribe_block_marketing'}]);
answer(second,{job_id:'second',status:'complete',items:[{candidate_id:3,status:'done',message:'Done'},{candidate_id:4,status:'done',message:'Done'}]});await settle();
assert.equal(w.document.querySelector('[data-selected-count]').textContent,'0');
''')

    def test_conflict_quarantines_only_unavailable_ids_and_retries_with_new_key(self):
        self.run_browser(r'''
select(1,'unsubscribe');select(2,'dismiss');apply();const first=last();
answer(first,{error:'One sender already handled',unavailable_ids:[1]},409);await settle();
assert.equal(w.document.querySelector('[data-subscription-id="1"] input').disabled,true);
assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'dismiss');
assert.equal(w.document.querySelector('[data-selected-count]').textContent,'1');
apply();const retry=last();assert.notEqual(key(retry),key(first));assert.deepEqual(payload(retry),[{candidate_id:2,action:'dismiss'}]);
answer(retry,{job_id:'retry',status:'complete',items:[{candidate_id:2,status:'done',message:'Kept'}]});await settle();
''')

    def test_lost_response_reuses_original_key_and_payload_before_new_batch(self):
        self.run_browser(r'''
select(1,'unsubscribe');apply();const first=last();first.reject(new TypeError('Network response lost'));await settle();
select(1,'dismiss');select(2,'unsubscribe');apply();const retry=last();
assert.equal(key(retry),key(first));assert.deepEqual(payload(retry),payload(first));
assert.equal(retry.options.body.get('csrf_token'),'csrf');
answer(retry,{job_id:'accepted-original',status:'complete',items:[{candidate_id:1,status:'done',message:'Unsubscribe requested'}]});await settle();
assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'unsubscribe');
apply();const next=last();assert.notEqual(key(next),key(first));assert.deepEqual(payload(next),[{candidate_id:2,action:'unsubscribe'}]);
answer(next,{job_id:'new-choice',status:'complete',items:[{candidate_id:2,status:'done',message:'Done'}]});await settle();
''')
