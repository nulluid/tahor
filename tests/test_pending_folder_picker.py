"""Pending folder controls retain all choices and submit the visible selection."""
import json
import os
import shutil
import subprocess
import unittest

import decision_bulk_ui


class PendingFolderPickerTests(unittest.TestCase):
    def card(self, number, suggestion=None):
        row = {'id': number, 'kind': 'vendor_mapping', 'context': '{}'}
        fields = decision_bulk_ui.choices(row, 'r1', suggestion, ['Vehicles', 'Shopping/Retail', 'Finance'])
        return '<div data-decision-id="%s" data-bulk-eligible="true" data-decision-revision="r1">%s</div>' % (number, fields)

    def test_menu_contains_every_folder_when_ai_preselects_one(self):
        rendered = self.card(1, {'action': 'map', 'bucket': 'Vehicles', 'vendor_name': 'Vendor'})
        self.assertIn('<option value="Vehicles" selected>Vehicles</option>', rendered)
        self.assertIn('<option value="Finance">Finance</option>', rendered)
        self.assertIn('data-bulk-custom-folder', rendered)
        self.assertNotIn('<datalist', rendered)

    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for DOM checks')
    def test_known_custom_restored_and_ai_folders_and_recommendation_sections(self):
        page = '<main>' + decision_bulk_ui.BAR.replace('<form ', '<input name="csrf_token" value="csrf"><form ')
        page += '<div id="decision-cards">' + self.card(2) + self.card(1, {'action': 'map', 'bucket': 'Vehicles', 'vendor_name': 'Vendor', 'reason': 'Receipt'}) + '</div></main>'
        harness = r'''
const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const dom=new JSDOM(PAGE,{url:'https://example.test/',runScripts:'outside-only'});const w=dom.window,calls=[];
const scrolls=[];w.scrollBy=(x,y)=>scrolls.push(y);w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));
w.eval(SCRIPT);const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 calls[0].resolve({ok:true,json:async()=>[]});await settle();
 const one=w.document.querySelector('[data-decision-id="1"]'), two=w.document.querySelector('[data-decision-id="2"]');
 assert.deepEqual([...w.document.querySelector('#decision-cards').children].map(e=>e.dataset.decisionId||e.textContent),['AI recommendations','1','Other pending decisions','2']);
 const select=one.querySelector('[data-bulk-folder]');assert.equal(select.options.length,4);assert.equal(select.value,'Vehicles');
 select.value='Finance';select.dispatchEvent(new w.Event('change',{bubbles:true}));assert.equal(one.querySelector('[data-bulk-bucket]').value,'Finance');
 const custom=one.querySelector('[data-bulk-custom-folder]');custom.value='Health/Fitness';custom.dispatchEvent(new w.Event('input',{bubbles:true}));assert.equal(select.value,'');assert.equal(one.querySelector('[data-bulk-bucket]').value,'Health/Fitness');
 assert.equal(JSON.parse(w.sessionStorage.getItem('tahor-decision-choices'))['1'].bucket,'Health/Fitness');
 custom.value='';one.querySelector('[data-bulk-bucket]').value='';w.document.dispatchEvent(new w.Event('tahor-decisions-updated'));await settle();
 // Reinitialization must restore both the canonical value and visible custom control.
 assert.equal(one.querySelector('[data-bulk-custom-folder]').value,'Health/Fitness');
 // Resolve any refresh status request before generating suggestions.
 for(const call of calls.slice(1))call.resolve({ok:true,json:async()=>[]});await settle();
 const before=calls.length;w.document.querySelector('[data-generate]').click();
 calls[before].resolve({ok:true,json:async()=>({job_id:'j',status:'complete',total:1,completed:1,recommendations:[{decision_id:2,source_revision:'r1',action:'map',bucket:'New category',vendor_name:'Another vendor',reason:'Classified archive'}]})});await settle();
 assert.ok(scrolls.includes(-12),'Finished generation reveals the recommended group');assert.equal(two.querySelector('[data-bulk-bucket]').value,'New category');assert.equal(two.querySelector('[data-bulk-custom-folder]').value,'New category');assert.equal(two.querySelector('[data-bulk-folder]').value,'');
 assert.deepEqual([...w.document.querySelectorAll('[data-decision-section]')].map(h=>h.textContent),['AI recommendations']);
 // An older 'leave unsorted' recommendation must follow a concrete filing recommendation.
 const none=one.querySelector('[data-decision-choice][value="unsorted"]');none.checked=true;none.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.deepEqual([...w.document.querySelectorAll('#decision-cards > [data-decision-id]')].map(c=>c.dataset.decisionId),['2','1']);
 assert.deepEqual([...w.document.querySelectorAll('[data-decision-section]')].map(h=>h.textContent),['AI recommendations','AI recommends dismissal or skipping']);
 const noAction=one.querySelector('[data-decision-choice][value=""]');noAction.checked=true;noAction.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.deepEqual([...w.document.querySelectorAll('[data-decision-section]')].map(h=>h.textContent),['AI recommendations','Other pending decisions']);
 const map=one.querySelector('[data-decision-choice][value="map"]');map.checked=true;map.dispatchEvent(new w.Event('change',{bubbles:true}));
 const next=calls.length;w.document.querySelector('[data-generate]').click();calls[next].resolve({ok:true,json:async()=>({job_id:'j2',status:'complete',total:1,completed:1,recommendations:[{decision_id:2,source_revision:'r1',action:'map',bucket:'Finance',vendor_name:'Another vendor',reason:'Updated classification'}]})});await settle();assert.equal(two.querySelector('[data-bulk-folder]').value,'Finance');assert.equal(two.querySelector('[data-bulk-custom-folder]').value,'');
 const deferred=calls.length;w.document.querySelector('[data-generate]').click();calls[deferred].resolve({ok:true,json:async()=>({job_id:'deferred',status:'complete',total:1,completed:1,recommendations:[{decision_id:2,source_revision:'r1',action:'defer',bucket:'',vendor_name:'',reason:'No example email'}]})});await settle();
 assert.equal(two.querySelector('[data-decision-choice]:checked').value,'');
 assert.ok(two.querySelector('[data-decision-recommendation]').textContent.includes('Keep this request pending'));
 assert.deepEqual([...w.document.querySelectorAll('[data-decision-section]')].map(h=>h.textContent),['AI recommendations','AI needs more information']);
 w.document.querySelector('[data-apply]').click();const submitted=JSON.parse(calls.at(-1).options.body.get('selections'));
 assert.equal(submitted.find(x=>x.decision_id===1).bucket,'Health/Fitness');assert.equal(submitted.some(x=>x.decision_id===2),false,'Uncertain AI advice must not submit a dismissal');w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        script = decision_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>')
        result = subprocess.run([shutil.which('node'), '-e', 'const PAGE='+json.dumps(page)+';const SCRIPT='+json.dumps(script)+';'+harness], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
