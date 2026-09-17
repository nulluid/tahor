"""Contextual owner guidance without leaving a review card."""
from html import escape


def render(kind, identifier, text=''):
    return f'''<details class="card-instructions"><summary>Tell Tahor what you want</summary>
<form method="post" action="/card-instructions/{kind}/{int(identifier)}" data-card-instructions>
<label>Your instructions<textarea name="instructions" rows="3" maxlength="4000" required placeholder="Explain who this sender is and how you want their emails handled.">{escape(text)}</textarea></label>
<p class="hint">Save guidance to improve AI recommendations for this sender. Propose a rule to request a mailbox policy change; review the proposal on Pending decisions before it applies. Neither button sends a reply or unsubscribes you.</p>
<div class="actions"><button name="submit_action" value="guidance">Save AI guidance</button><button name="submit_action" value="propose_rule">Propose a rule</button></div>
<p data-instruction-status role="status" aria-live="polite"></p>
</form></details>'''

SCRIPT = r'''<script>
(() => {
 document.addEventListener('input', event => {
  const form=event.target.closest('[data-card-instructions]');if(!form)return;
  const card=form.closest('[data-subscription-id]');if(card){delete card.dataset.guidanceSaved;card.dispatchEvent(new Event('card-guidance-state',{bubbles:true}));}
  if(card&&!card.dataset.queued&&card.dataset.manual!=='true'){
   const none=card.querySelector('input[type="radio"][value=""]');
   if(none){card.dataset.instructionCleared='true';none.checked=true;none.dispatchEvent(new Event('change',{bubbles:true}));}
  }
 });
 document.addEventListener('submit', async event => {
  const form=event.target.closest('[data-card-instructions]');if(!form)return;
  event.preventDefault();if(form.dataset.busy)return;
  const data=new FormData(form);data.set('submit_action',event.submitter?.value||'guidance');
  const status=form.querySelector('[data-instruction-status]');
  form.dataset.busy='true';form.querySelectorAll('button').forEach(b=>b.disabled=true);
  status.textContent='Saving your instructions…';
  try{
   const response=await fetch(form.getAttribute('action'),{method:'POST',body:data,credentials:'same-origin',headers:{Accept:'application/json'}});
   const result=await response.json();if(!response.ok)throw new Error(result.error||'Instructions could not be saved.');
   const unchanged=form.querySelector('textarea').value===data.get('instructions');
   status.textContent=unchanged?(result.message||'Guidance saved. Generate fresh suggestions to use it.'):'Earlier text saved; save your latest edits before generating.';
   const card=form.closest('[data-subscription-id]');if(unchanged&&card?.dataset.instructionCleared){card.dataset.guidanceSaved='true';card.dispatchEvent(new Event('card-guidance-state',{bubbles:true}));}
   if(result.decision_id){const link=document.createElement('a');link.href='/';link.textContent=' Review rule proposals';status.append(link);}
  }catch(error){status.textContent=error.message||'Could not confirm the save. Your text is still here; retry when ready.';}
  finally{delete form.dataset.busy;form.querySelectorAll('button').forEach(b=>b.disabled=false);}
 });
})();
</script>'''
