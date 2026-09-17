"""Real DOM coverage of pending-card refresh and source-bound manual choices."""
import json
import os
import shutil
import subprocess
import unittest
import decision_bulk_ui
import decision_interactions


@unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for real browser checks')
class PendingRefreshRegressions(unittest.TestCase):
    def test_refresh_drops_stale_manual_action_and_initializes_one_toolbar(self):
        bulk=decision_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        individual=decision_interactions.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        harness=r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card=(id,revision)=>`<div data-decision-id="${id}" data-decision-kind="message_review" data-bulk-eligible="true" data-decision-revision="${revision}"><form action="/resolve/${id}"><input name="csrf_token" value="csrf"><button name="action" value="keep">Keep</button></form><fieldset data-decision-choices><input data-decision-choice name="choice-${id}" type="radio" value="" checked><input data-decision-choice name="choice-${id}" type="radio" value="trash"></fieldset><p class="decision-result"></p></div>`;
const page=(initial)=>'<main>'+BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+'<div id="decision-cards">'+(initial?card(1,'r1'):'')+card(2,initial?'r2':'changed-source')+'</div></main>';
const dom=new JSDOM(page(true),{url:'https://example.test/',runScripts:'outside-only'});const w=dom.window,calls=[];
w.scrollBy=()=>{};w.scrollTo=()=>{};w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));
w.eval(INDIVIDUAL);w.eval(BULK);const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 calls[0].resolve({ok:true,json:async()=>[]});await settle();
 const stale=w.document.querySelector('[data-decision-id="2"] input[value="trash"]');stale.checked=true;stale.dispatchEvent(new w.Event('change',{bubbles:true}));
 const form=w.document.querySelector('[data-decision-id="1"] form');form.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:form.querySelector('button')}));await settle();
 assert.equal(calls[1].url,'/resolve/1');calls[1].resolve({ok:true,url:'https://example.test/',text:async()=>page(false)});await settle();await settle();
 assert.equal(calls[2].url,'/decisions/batches');calls[2].resolve({ok:true,json:async()=>[]});await settle();
 assert.equal(w.document.querySelector('[data-decision-id="2"] input[data-decision-choice]:checked').value,'','A changed source must not inherit old Trash authorization');
 w.document.querySelector('[data-generate]').click();assert.equal(calls.length,4,'Only one toolbar handler should submit after refresh');
 calls[3].resolve({ok:true,json:async()=>({job_id:'empty',status:'complete',total:0,completed:0,recommendations:[]})});await settle();w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        code='const BULK='+json.dumps(bulk)+';const INDIVIDUAL='+json.dumps(individual)+';const BAR='+json.dumps(decision_bulk_ui.BAR)+';'+harness
        result=subprocess.run([shutil.which('node'),'-e',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
