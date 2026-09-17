"""Owner review remains authoritative across AI batches and page updates."""
import json
import os
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch
from test_web_security import AppTestCase
import decision_bulk_ui


class PendingRecommendationRoutes(AppTestCase):
    def seed(self, identifier, kind='message_review'):
        db=self.module.tahor_db.get_db()
        with db:
            db.execute("INSERT INTO decisions(id,kind,summary,context,status,created_at) VALUES(?,?,?,?,'pending','2026-09-17')",(identifier,kind,'Example',json.dumps({'sender':'owner@example.org','mailbox':'INBOX','message_id':'<sample@example.org>','uid':'1','uidvalidity':'2'})))
        db.close()

    def test_recommended_card_comes_first_and_rule_approval_is_separate(self):
        self.seed(1);self.seed(2);self.seed(3,'free_text_rule')
        with patch('decision_suggestions.latest_recommendations',return_value=[{'decision_id':2,'action':'keep','reason':'Useful record','source_revision':'sample'}]):
            body=self.client.get('/').get_data(as_text=True)
        self.assertLess(body.index('data-decision-id="2"'),body.index('data-decision-id="1"'))
        self.assertEqual(len(re.findall(r'<div[^>]*data-bulk-eligible="true"', body)),2)
        self.assertIn('data-generate',body)
        self.assertIn('data-decision-choice',body)

    def test_settings_exposes_default_twenty_and_autosaves_task(self):
        page=self.client.get('/settings').get_data(as_text=True)
        self.assertIn('data-ai-task="decisions"',page)
        self.assertIn('value="20" required',page)
        with patch.object(self.module.mailbox_settings,'set_ai_task_settings') as setter:
            response=self.client.post('/settings',data={'csrf_token':self.token(),'ai_task':'decisions','ai_policy':'paid','batch_size':'25','decision_guidance':'Preserve important records'},headers={'Accept':'application/json'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(setter.call_args.kwargs['batch_size'],'25')
            self.assertEqual(setter.call_args.kwargs['guidance'],'Preserve important records')


@unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'),'Set TAHOR_JSDOM_MODULE for browser checks')
class PendingRecommendationBrowser(unittest.TestCase):
    def test_suggestions_filing_fields_manual_choices_and_consecutive_batches(self):
        rows=[{'id':1,'kind':'message_review','context':'{}'},{'id':2,'kind':'vendor_mapping','context':'{}'},{'id':3,'kind':'message_review','context':'{}'}]
        markup=''.join('<div class="card" data-bulk-eligible="true" data-decision-id="'+str(r['id'])+'" data-decision-revision="r'+str(r['id'])+'">'+decision_bulk_ui.choices(r,'r'+str(r['id']))+'</div>' for r in rows)
        script=decision_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        harness=r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+'<div id="decision-cards">'+CARDS+'</div>',{url:'https://example.test/',runScripts:'outside-only'});
const w=dom.window,calls=[];w.scrollBy=()=>{};w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));w.eval(SCRIPT);
const settle=()=>new Promise(r=>setImmediate(r));const answer=(call,value)=>call.resolve({ok:true,json:async()=>value});
const card=id=>w.document.querySelector('[data-decision-id="'+id+'"]');const select=(id,action)=>{const radio=card(id).querySelector('[data-decision-choice][value="'+action+'"]');radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));};
(async()=>{
 answer(calls[0],[]);await settle();select(1,'keep');
 w.document.querySelector('[data-generate]').click();assert.equal(calls[1].url,'/decisions/suggestions');
 answer(calls[1],{job_id:'ai',status:'complete',recommendations:[{decision_id:1,action:'trash',reason:'Wrong',source_revision:'r1'},{decision_id:2,action:'map',bucket:'Business/Software',vendor_name:'Example Vendor',reason:'Receipt',source_revision:'new2'}]});await settle();
 assert.equal(card(1).querySelector('input:checked').value,'keep');assert.equal(card(2).querySelector('[data-bulk-bucket]').value,'Business/Software');
 assert.deepEqual([...w.document.querySelector('#decision-cards').children].map(c=>c.dataset.decisionId),['1','2','3']);
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'2');
 w.document.querySelector('[data-apply]').click();const sent=JSON.parse(calls[2].options.body.get('selections'));
 assert.deepEqual(sent,[{decision_id:1,action:'keep',source_revision:'r1'},{decision_id:2,action:'map',source_revision:'new2',bucket:'Business/Software',vendor_name:'Example Vendor'}]);
 answer(calls[2],{job_id:'batch',status:'complete',items:[{decision_id:1,status:'done',message:'Kept'},{decision_id:2,status:'done',message:'Filed'}]});await settle();
 select(3,'keep_brief');w.document.querySelector('[data-apply]').click();assert.equal(JSON.parse(calls[3].options.body.get('selections')).length,1);
 answer(calls[3],{job_id:'batch2',status:'complete',items:[{decision_id:3,status:'done',message:'Kept briefly'}]});await settle();
 assert.equal(w.document.querySelector('[data-selected-count]').textContent,'0');assert.equal(w.location.pathname,'/');w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run([shutil.which('node'),'-e','const SCRIPT='+json.dumps(script)+';const BAR='+json.dumps(decision_bulk_ui.BAR)+';const CARDS='+json.dumps(markup)+';'+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
