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
 w.document.querySelector('[data-generate]').click();assert.equal(w.document.querySelector('[data-bulk-activity]').hidden,false);assert.equal(w.document.querySelector('[data-generate]').getAttribute('aria-busy'),'true');assert.equal(calls[1].url,'/unsubscribe/suggestions');assert.deepEqual(JSON.parse(calls[1].options.body.get('exclude_ids')),[1]);
 respond(calls[1],{job_id:'suggestions',status:'complete',recommendations:[{candidate_id:1,action:'unsubscribe',reason:'Example'},{candidate_id:2,action:'unsubscribe',reason:'Example'}]});await settle();
 assert.equal(w.document.querySelector('[data-subscription-id="1"] input:checked').value,'dismiss');
 assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'unsubscribe');
 assert.equal(w.document.querySelector('[data-bulk-activity]').hidden,true);assert.equal(w.document.querySelector('[data-generate]').hasAttribute('aria-busy'),false);
 assert.equal(calls.length,2,'AI suggestions must never execute unsubscribe');
 w.document.querySelector('[data-apply]').click();w.document.querySelector('[data-apply]').click();
 assert.equal(w.document.querySelector('[data-bulk-activity]').hidden,false);assert.equal(calls.length,3);assert.equal(calls[2].url,'/unsubscribe/batches');
 const payload=JSON.parse(calls[2].options.body.get('selections'));assert.deepEqual(payload,[{candidate_id:1,action:'dismiss'},{candidate_id:2,action:'unsubscribe'}]);assert.equal(calls[2].options.body.get('csrf_token'),'csrf');
 respond(calls[2],{job_id:'batch',status:'complete',items:[{candidate_id:1,status:'done',message:'Kept'},{candidate_id:2,status:'attention',message:'Needs attention'}]});await settle();
 assert.equal(w.document.querySelector('[data-bulk-activity]').hidden,true);assert.equal(w.document.querySelector('[data-selected-count]').textContent,'0');
 assert.match(w.document.querySelector('[data-subscription-id="2"] .subscription-result').textContent,/Needs attention/);assert.match(w.document.querySelector('[data-subscription-id="1"] .subscription-result').textContent,/Completed: Kept/);
 assert.equal(w.location.pathname,'/unsubscribe');w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        run=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';\n'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)

    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for real DOM subscription tests')
    def test_queued_generation_does_not_claim_any_choices_are_ready(self):
        harness=r'''const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form '),{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window;const calls=[];const timers=[];
w.setTimeout=callback=>timers.push(callback);w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,resolve}));w.eval(SCRIPT);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 calls[0].resolve({ok:true,json:async()=>[]});await settle();
 w.document.querySelector('[data-generate]').click();
 calls[1].resolve({ok:true,json:async()=>({job_id:'job',status:'queued',completed:0,total:50,recommendations:[]})});await settle();
 assert.equal(w.document.querySelector('[data-bulk-activity]').hidden,false);
 const text=w.document.querySelector('[data-bulk-status]').textContent;
 assert.match(text,/Waiting for the background worker/);assert.match(text,/0 of 50 processed/);assert.match(text,/No suggestions are ready yet/);
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'0');
 assert.doesNotMatch(text,/Completed choices/);w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        run=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';\n'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)

    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for real DOM subscription tests')
    def test_attention_poll_and_reload_preserve_retry_choice_and_new_jobs_take_precedence(self):
        harness=r'''const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card='<div data-subscription-id="1"><p class="subscription-result"></p><fieldset><input type="radio" name="choice-1" value="" checked><input type="radio" name="choice-1" value="unsubscribe"></fieldset></div>';
function create(saved){const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+card,{url:'https://example.test/unsubscribe',runScripts:'outside-only'});const w=dom.window,calls=[],timers=[];if(saved)w.sessionStorage.setItem('tahor-subscription-choices',saved);w.setTimeout=callback=>timers.push(callback);w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));w.eval(SCRIPT);return {dom,w,calls,timers};}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const attention=id=>({job_id:id,status:'running',items:[{candidate_id:1,status:'attention',message:'Delivery failed'}]});
const answer=(call,value)=>call.resolve({ok:true,json:async()=>value});
(async()=>{
 let env=create();answer(env.calls[0],[attention('old')]);await settle();
 const choice=env.w.document.querySelector('input[value="unsubscribe"]');choice.checked=true;choice.dispatchEvent(new env.w.Event('change',{bubbles:true}));
 env.timers.shift()();await settle();answer(env.calls[1],attention('old'));await settle();
 assert.equal(choice.checked,true,'repeated attention must not erase newly edited choice');
 const stored=env.w.sessionStorage.getItem('tahor-subscription-choices');env.w.close();
 env=create(stored);answer(env.calls[0],[attention('old')]);await settle();
 const restored=env.w.document.querySelector('input[value="unsubscribe"]');
 assert.equal(restored.checked,true,'page reload must preserve the acknowledged retry choice');
 env.w.document.querySelector('[data-apply]').click();
 answer(env.calls[1],{job_id:'new',status:'running',items:[{candidate_id:1,status:'queued',message:'New choice queued'}]});await settle();
 assert.equal(restored.disabled,true);
 env.timers.shift()();await settle();answer(env.calls[2],attention('old'));await settle();
 assert.equal(restored.disabled,true,'older job polling must not unlock a newly queued action');
 assert.match(env.w.document.querySelector('.subscription-result').textContent,/New choice queued/);
 env.timers.shift()();await settle();assert.equal(env.calls[3].url,'/unsubscribe/batches/new');
 answer(env.calls[3],{...attention('new'),status:'complete'});await settle();
 assert.equal(restored.checked,false,'a different failed job still needs fresh review');
 assert.equal(restored.disabled,false);env.w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        run=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';\n'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
