"""Recommendations stay ahead of untouched subscriptions without moving the viewport."""
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch
import subscription_bulk_ui
from test_web_security import AppTestCase

class PriorityPageTests(AppTestCase):
    def test_recommendation_precedes_untouched_noncompliant_sender(self):
        dbm=self.module.tahor_db
        first=dbm.upsert_unsubscribe_candidate('first.example','news@first.example','First',None,None,False)
        second=dbm.upsert_unsubscribe_candidate('second.example','news@second.example','Second',None,None,False)
        db=dbm.get_db()
        with db: db.execute('UPDATE unsubscribe_candidates SET non_compliant=1 WHERE id=?',(first,))
        db.close()
        with patch('subscription_suggestions.latest_recommendations', return_value=[dict(candidate_id=second,action='dismiss',reason='Wanted updates')]):
            page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertLess(page.index('id="subscription-'+str(second)+'"'),page.index('id="subscription-'+str(first)+'"'))
        self.assertIn('id="subscription-cards"',page)

class PriorityBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for DOM tests')
    def test_arriving_recommendations_sort_stably_and_preserve_visible_card(self):
        script=subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        harness=r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const card=id=>`<div data-subscription-id="${id}"><p class="subscription-result"></p><fieldset><input type="radio" name="r${id}" value="" checked><input type="radio" name="r${id}" value="dismiss"><input type="radio" name="r${id}" value="unsubscribe_block_marketing"></fieldset></div>`;
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+'<div id="subscription-cards">'+[1,2,3].map(card).join('')+'</div>',{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window;const list=w.document.querySelector('#subscription-cards');let scroll=180;
w.scrollBy=(x,y)=>{scroll+=y;};Object.defineProperty(w,'scrollY',{get:()=>scroll});
w.document.querySelector('#subscription-tools').getBoundingClientRect=()=>({bottom:100});
for(const c of list.children)c.getBoundingClientRect=()=>({top:300+[...list.children].indexOf(c)*100-scroll,bottom:400+[...list.children].indexOf(c)*100-scroll});
// Stale AI choices must not return from browser storage after feedback changes.
w.sessionStorage.setItem('tahor-subscription-choices',JSON.stringify({'1':{action:'dismiss',manual:false}}));
w.fetch=async(url,options)=>({ok:true,json:async()=>url==='/unsubscribe/batches'?[]:{job_id:'job',status:'complete',total:2,completed:2,recommendations:[{candidate_id:2,action:'dismiss',reason:'Wanted'},{candidate_id:3,action:'unsubscribe_block_marketing',reason:'Unwanted'}]}});
w.eval(SCRIPT);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 assert.equal(list.children[0].querySelector('input:checked').value,'');
 const first=list.children[0];const before=first.getBoundingClientRect().top;
 w.document.querySelector('[data-generate]').click();await settle();await settle();
 assert.deepEqual([...list.children].map(c=>c.dataset.subscriptionId),['2','3','1']);
 assert.equal(first.getBoundingClientRect().top,before);
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'2');
 const radio=first.querySelector('input[value="dismiss"]');radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.deepEqual([...list.children].map(c=>c.dataset.subscriptionId),['2','3','1']);
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'3');
 w.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
