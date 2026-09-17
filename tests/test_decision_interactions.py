import json
import os
import shutil
import subprocess
import unittest
import decision_interactions


class DecisionBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE to an installed jsdom module for browser DOM tests')
    def test_real_forms_keep_position_serialize_and_preserve_other_edits(self):
        script=decision_interactions.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        harness=r'''
const assert=require('node:assert/strict');
const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card=id=>`<div class="card" data-decision-id="${id}" data-decision-kind="message_review"><form action="/resolve/${id}" method="post"><input name="csrf_token" value="csrf"><input name="vendor_name" value="original"><button name="action" value="trash">Trash</button></form></div>`;
const page=cards=>`<main><h1><span class="count">${cards.length}</span></h1>${cards.map(card).join('')}</main>`;
const dom=new JSDOM(page(['1','2','3']),{url:'https://example.test/',runScripts:'outside-only'});
const w=dom.window;const requests=[];const scrolls=[];
w.scrollBy=(x,y)=>scrolls.push(y);w.scrollTo=(x,y)=>scrolls.push(y);
w.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
w.HTMLElement.prototype.getBoundingClientRect=function(){return {top:100+Number(this.dataset.decisionId||0)*20,bottom:180};};
w.eval(SCRIPT);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const click=id=>{let f=w.document.querySelector(`[data-decision-id="${id}"] form`);f.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:f.querySelector('button')}));};
(async()=>{
 // A real named action control must never be used as the request URL.
 const f=w.document.querySelector('form');Object.defineProperty(f,'action',{value:f.querySelector('button')});
 click('1');click('1');click('2');await settle();
 assert.equal(requests.length,1);assert.equal(requests[0].url,'/resolve/1');
 assert.equal(requests[0].options.body.get('action'),'trash');assert.equal(requests[0].options.body.get('csrf_token'),'csrf');
 w.document.querySelector('[data-decision-id="3"] input[name="vendor_name"]').value='typing while saving';
 requests[0].resolve({ok:true,url:'https://example.test/',text:async()=>page(['2','3'])});await settle();await settle();
 assert.equal(requests.length,2);assert.equal(requests[1].url,'/resolve/2');
 assert.equal(w.document.querySelector('[data-decision-id="3"] input[name="vendor_name"]').value,'typing while saving');
 assert.ok(w.document.querySelector('[data-decision-id="2"] button').disabled);assert.equal(scrolls[0],20);
 requests[1].resolve({ok:true,url:'https://example.test/',text:async()=>page(['3'])});await settle();await settle();
 assert.equal(requests.length,2);assert.ok(w.document.querySelector('[data-decision-id="3"]'));
 click('3');await settle();requests[2].resolve({ok:false,status:409,url:'https://example.test/resolve/3'});await settle();
 assert.match(w.document.querySelector('.decision-result').textContent,/decision changed/);
 assert.equal(w.document.querySelector('button').disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';\n'+harness],check=True,timeout=15,capture_output=True,text=True)
