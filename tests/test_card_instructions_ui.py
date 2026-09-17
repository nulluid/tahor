"""Card-scoped instructions remain private, explicit, and separate from mail actions."""
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

import card_instructions_ui
import subscription_bulk_ui
from test_web_security import AppTestCase


class CardInstructionBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for card-instruction DOM tests')
    def test_editing_and_async_save_preserve_manual_choices_and_require_latest_text(self):
        form=card_instructions_ui.render('subscription',1).replace('data-card-instructions>', 'data-card-instructions><input name="csrf_token" value="note-csrf">')
        harness=r'''const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
const markup=id=>`<div data-subscription-id="${id}" ${id===2?'data-manual="true"':''}><p class="subscription-result"></p><fieldset><input type="radio" name="choice-${id}" value=""><input type="radio" name="choice-${id}" value="unsubscribe" ${id===1?'checked':''}><input type="radio" name="choice-${id}" value="dismiss" ${id===2?'checked':''}></fieldset>${FORM.replace('/subscription/1','/subscription/'+id)}</div>`;
const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="bulk-csrf"><form ')+'<div id="subscription-cards">'+markup(1)+markup(2)+'</div>',{url:'https://example.test/unsubscribe',runScripts:'outside-only'});
const w=dom.window,calls=[],scrolls=[];w.scrollBy=(...args)=>scrolls.push(args);w.scrollTo=(...args)=>scrolls.push(args);w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));
w.eval(BULK);w.eval(SCRIPT);const settle=()=>new Promise(resolve=>setImmediate(resolve));const answer=(call,result)=>call.resolve({ok:true,json:async()=>result});
const form=id=>w.document.querySelector(`[data-subscription-id="${id}"] form`);
const input=(id,text)=>{const field=form(id).querySelector('textarea');field.value=text;field.dispatchEvent(new w.Event('input',{bubbles:true}));};
const submit=id=>form(id).dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:form(id).querySelector('button')}));
(async()=>{
 answer(calls[0],[]);await settle();
 input(1,'Earlier instructions');assert.equal(w.document.querySelector('[data-subscription-id="1"] input:checked').value,'');
 input(2,'My manually selected choice must remain');assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'dismiss');
 submit(1);submit(1);assert.equal(calls.length,2,'duplicate saves must not create duplicate requests');
 assert.equal(calls[1].url,'/card-instructions/subscription/1');assert.equal(calls[1].options.body.get('csrf_token'),'note-csrf');assert.equal(calls[1].options.body.get('instructions'),'Earlier instructions');
 input(1,'Latest instructions');answer(calls[1],{message:'Guidance saved'});await settle();
 assert.equal(form(1).querySelector('textarea').value,'Latest instructions');
 assert.match(form(1).querySelector('[data-instruction-status]').textContent,/latest/i);
 assert.notEqual(form(1).closest('[data-subscription-id]').dataset.guidanceSaved,'true');
 submit(1);answer(calls[2],{message:'Guidance saved'});await settle();
 assert.equal(form(1).closest('[data-subscription-id]').dataset.guidanceSaved,'true');
 w.document.querySelector('[data-generate]').click();assert.equal(calls[3].url,'/unsubscribe/suggestions');
 assert.deepEqual(JSON.parse(calls[3].options.body.get('exclude_ids')),[2]);
 answer(calls[3],{job_id:'fresh',status:'complete',recommendations:[{candidate_id:1,action:'dismiss',reason:'Matches your instructions'}]});await settle();
 assert.equal(w.document.querySelector('[data-subscription-id="1"] input:checked').value,'dismiss');
 assert.equal(w.document.querySelector('[data-subscription-id="2"] input:checked').value,'dismiss');
 assert.equal(calls.filter(call=>call.options.method==='POST'&&call.url==='/unsubscribe/batches').length,0);
 assert.equal(w.location.pathname,'/unsubscribe');assert.equal(scrolls.length,0);w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        prefix='const FORM='+json.dumps(form)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';const BULK='+json.dumps(subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>'))+';const SCRIPT='+json.dumps(card_instructions_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>'))+';\n'
        run=subprocess.run([shutil.which('node'),'-e',prefix+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)

    @unittest.skipUnless(shutil.which('node') and os.environ.get('TAHOR_JSDOM_MODULE'), 'Set TAHOR_JSDOM_MODULE for card-instruction DOM tests')
    def test_saved_guidance_can_get_fresh_suggestions_after_reload(self):
        form=card_instructions_ui.render('subscription',1).replace('data-card-instructions>', 'data-card-instructions><input name="csrf_token" value="csrf">')
        harness=r'''const assert=require('node:assert/strict');const {JSDOM}=require(process.env.TAHOR_JSDOM_MODULE);
function create(saved){const markup='<div data-subscription-id="1"><p class="subscription-result"></p><input type="radio" name="choice" value=""><input type="radio" name="choice" value="unsubscribe" checked><input type="radio" name="choice" value="dismiss">'+FORM+'</div>';const dom=new JSDOM(BAR.replace('<form ','<input name="csrf_token" value="csrf"><form ')+markup,{url:'https://example.test/unsubscribe',runScripts:'outside-only'});const w=dom.window,calls=[];if(saved)w.sessionStorage.setItem('tahor-subscription-choices',saved);w.fetch=(url,options)=>new Promise(resolve=>calls.push({url,options,resolve}));w.eval(BULK);w.eval(SCRIPT);return {w,calls};}
const settle=()=>new Promise(resolve=>setImmediate(resolve));const answer=(call,value)=>call.resolve({ok:true,json:async()=>value});
(async()=>{
 let env=create();answer(env.calls[0],[]);await settle();
 const f=env.w.document.querySelector('[data-card-instructions]');const text=f.querySelector('textarea');text.value='Preserve these updates';text.dispatchEvent(new env.w.Event('input',{bubbles:true}));
 f.dispatchEvent(new env.w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:f.querySelector('button')}));answer(env.calls[1],{message:'Saved'});await settle();
 const saved=env.w.sessionStorage.getItem('tahor-subscription-choices');assert.equal(JSON.parse(saved)['1'].guidanceSaved,true);env.w.close();
 env=create(saved);answer(env.calls[0],[]);await settle();env.w.document.querySelector('[data-generate]').click();
 assert.deepEqual(JSON.parse(env.calls[1].options.body.get('exclude_ids')),[]);
 answer(env.calls[1],{job_id:'fresh',status:'complete',recommendations:[{candidate_id:1,action:'dismiss',reason:'Matches owner guidance'}]});await settle();
 assert.equal(env.w.document.querySelector('[data-subscription-id] input:checked').value,'dismiss');env.w.close();
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        prefix='const FORM='+json.dumps(form)+';const BAR='+json.dumps(subscription_bulk_ui.BAR)+';const BULK='+json.dumps(subscription_bulk_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>'))+';const SCRIPT='+json.dumps(card_instructions_ui.SCRIPT.removeprefix('<script>').removesuffix('</script>'))+';\n'
        run=subprocess.run([shutil.which('node'),'-e',prefix+harness],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)

    def test_render_escapes_stored_instruction_text_and_scopes_route(self):
        rendered=card_instructions_ui.render('subscription',17,'</textarea><script>alert(1)</script>')
        self.assertIn('action="/card-instructions/subscription/17"',rendered)
        self.assertIn('&lt;/textarea&gt;',rendered)
        self.assertNotIn('<script>alert',rendered)


class CardInstructionRouteTests(AppTestCase):
    def candidate(self):
        self.module.tahor_db.upsert_unsubscribe_candidate('shop.example','news@shop.example','Example',None,None,False)
        return self.module.tahor_db.get_unsubscribe_candidate('shop.example')['id']

    def post(self,kind,identifier,text='Keep these updates',action='guidance'):
        return self.client.post(f'/card-instructions/{kind}/{identifier}',data={'csrf_token':self.token(),'instructions':text,'submit_action':action})

    def test_owner_csrf_and_scope_are_required(self):
        identifier=self.candidate()
        route=f'/card-instructions/subscription/{identifier}'
        with patch.object(self.module.card_instructions,'save_card_instructions') as save:
            self.assertEqual(self.client.post(route,data={'instructions':'Example'}).status_code,400)
            other=self.module.app.test_client()
            with other.session_transaction() as session:session['csrf_token']='valid-other-session'
            response=other.post(route,data={'csrf_token':'valid-other-session','instructions':'Example'})
            self.assertNotEqual(response.status_code,200)
            save.assert_not_called()
        self.assertEqual(self.post('invalid',identifier).status_code,400)
        self.assertEqual(self.post('subscription',identifier+9999).status_code,404)
        self.assertEqual(self.post('subscription',identifier,action='apply_now').status_code,400)
        self.assertEqual(self.client.get(route).status_code,405)

    def test_saved_guidance_is_scoped_and_escaped_on_its_card(self):
        identifier=self.candidate();text='Keep updates </textarea><script>alert(1)</script>'
        response=self.post('subscription',identifier,text)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.module.card_instructions.get_card_instructions('subscription',identifier),text)
        self.assertEqual(self.module.card_instructions.get_card_instructions('decision',identifier),'')
        page=self.client.get('/unsubscribe').get_data(as_text=True)
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;alert(1)',page)
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertIn(f'action="/card-instructions/subscription/{identifier}"',page)

    def test_rule_request_is_deduplicated_and_never_applies_mailbox_actions(self):
        identifier=self.candidate()
        with patch.object(self.module.apply_decisions,'apply_one') as execute,patch.object(self.module.tahor_db,'execute_unsubscribe') as send:
            first=self.post('subscription',identifier,action='propose_rule')
            second=self.post('subscription',identifier,action='propose_rule')
            self.assertEqual(first.status_code,200);self.assertEqual(first.json['decision_id'],second.json['decision_id'])
            execute.assert_not_called();send.assert_not_called()
        db=self.module.tahor_db.get_db()
        row=db.execute('SELECT * FROM decisions WHERE id=?',(first.json['decision_id'],)).fetchone()
        self.assertEqual(row['kind'],'free_text_rule')
        self.assertEqual(json.loads(row['context'])['card_instruction_exact_sender'],'news@shop.example')
        self.assertNotIn('rule_proposal',json.loads(row['context']))
        self.assertIn('exact sender address news@shop.example',json.loads(row['resolution'])['text'])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sender_rules').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT status FROM unsubscribe_candidates WHERE id=?',(identifier,)).fetchone()[0],'pending')
        db.close()
