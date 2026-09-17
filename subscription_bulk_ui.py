"""Inert subscription choices, explicit batch submission, and local progress."""
SCRIPT = r'''<script>
(() => {
 const bar=document.querySelector('#subscription-tools'); if(!bar) return;
 const cards=()=>[...document.querySelectorAll('[data-subscription-id]')];
 const cardFor=id=>document.querySelector('[data-subscription-id="'+id+'"]');
 const notice=bar.querySelector('[data-bulk-status]');
 const apply=bar.querySelector('[data-apply]'); const generate=bar.querySelector('[data-generate]');
 const activity=bar.querySelector('[data-bulk-activity]');
 const activeWork=new Set();
 function busy(key,working){if(working)activeWork.add(key);else activeWork.delete(key);if(activity)activity.hidden=activeWork.size===0;}
 const csrf=bar.querySelector('input[name=csrf_token]').value;
 const storageKey='tahor-subscription-choices';
 let saved={}; try{saved=JSON.parse(sessionStorage.getItem(storageKey)||'{}');}catch(_){}
 const choices=()=>cards().filter(c=>!c.dataset.queued).map(c=>({candidate_id:Number(c.dataset.subscriptionId),action:c.querySelector('input:checked')?.value})).filter(c=>c.action);
 function remember(){const state={};for(const c of cards()){const value=c.querySelector('input:checked')?.value;if(value||c.dataset.manual==='true')state[c.dataset.subscriptionId]={action:value,manual:c.dataset.manual==='true',attentionJob:c.dataset.attentionJob||'',instructionCleared:c.dataset.instructionCleared==='true',guidanceSaved:c.dataset.guidanceSaved==='true'};}try{sessionStorage.setItem(storageKey,JSON.stringify(state));}catch(_){}}
 function prioritize(preserve=true){
  const list=document.querySelector('#subscription-cards');if(!list||document.querySelector('dialog.email-preview[open]'))return;
  const current=[...list.querySelectorAll('[data-subscription-id]')];
  const rank=c=>c.dataset.queued?3:(c.querySelector('input:checked')?.value?0:(c.dataset.recommended==='true'?1:2));
  const ordered=[...current].sort((a,b)=>rank(a)-rank(b));
  if(ordered.every((c,i)=>c===current[i]))return;
  const edge=Math.max(0,bar.getBoundingClientRect().bottom);
  const anchor=preserve?current.find(c=>{const r=c.getBoundingClientRect();return r.bottom>edge&&r.top<window.innerHeight;}):null;
  const before=anchor?.getBoundingClientRect().top;const focused=document.activeElement;
  list.style.overflowAnchor='none';for(const c of ordered)list.append(c);
  if(anchor){const delta=anchor.getBoundingClientRect().top-before;if(delta)window.scrollBy(0,delta);}
  if(focused?.isConnected&&list.contains(focused))focused.focus({preventScroll:true});
 }
 document.addEventListener('close',()=>setTimeout(()=>prioritize(),0),true);
 function count(preserve=true){bar.querySelector('[data-selected-count]').textContent=String(choices().length);prioritize(preserve);}
 function choose(c,action,manual){const radio=[...c.querySelectorAll('input[type="radio"]')].find(r=>r.value===action);if(radio&&!c.dataset.queued){radio.checked=true;if(manual)c.dataset.manual='true';}}
 for(const c of cards()){const old=saved[c.dataset.subscriptionId];if(old?.attentionJob)c.dataset.attentionJob=old.attentionJob;if(old?.instructionCleared)c.dataset.instructionCleared='true';if(old?.guidanceSaved)c.dataset.guidanceSaved='true';if(old?.manual)choose(c,old.action,true);}
 document.addEventListener('card-guidance-state',()=>remember());
 document.addEventListener('change',event=>{const c=event.target.closest('[data-subscription-id]');if(c&&event.target.type==='radio'){if(event.isTrusted){delete c.dataset.instructionCleared;delete c.dataset.guidanceSaved;}c.dataset.manual='true';remember();count();}});
 bar.querySelector('[data-select-all]').addEventListener('click',()=>{const action=bar.querySelector('[data-bulk-choice]').value;for(const c of cards())choose(c,action,true);remember();count();});
 async function request(url,values){const options={credentials:'same-origin',headers:{Accept:'application/json'}};if(values){options.method='POST';options.body=new URLSearchParams({csrf_token:csrf,...values});}const response=await fetch(url,options);const body=await response.json();if(!response.ok){const error=new Error(body.error||'The request could not be confirmed.');error.status=response.status;error.unavailableIds=body.unavailable_ids;throw error;}return body;}
 const wait=()=>new Promise(resolve=>setTimeout(resolve,2000));
 const watched=new Set();
 async function follow(job,replace=false){if(watched.has(job.job_id))return;watched.add(job.job_id);busy(job.job_id,job.status!=='complete');for(const item of job.items){const c=cardFor(item.candidate_id);if(c&&(replace===true||!c.dataset.actionJob))c.dataset.actionJob=job.job_id;}try{while(true){for(const item of job.items){const c=cardFor(item.candidate_id);if(!c||c.dataset.actionJob!==job.job_id)continue;c.dataset.queued='true';c.querySelectorAll('input').forEach(r=>r.disabled=true);c.querySelector('.subscription-result').textContent=item.message;if(item.status==='done'||item.status==='attention'||item.status==='uncertain'){c.dataset.finished='true';}if(item.status==='done'){c.querySelector('.subscription-result').textContent='Completed: '+item.message;const controls=c.querySelector('fieldset');if(controls)controls.hidden=true;}if(item.status==='attention'&&item.retry_allowed===false){const controls=c.querySelector('fieldset');if(controls)controls.hidden=true;}if(item.status==='attention'&&item.retry_allowed!==false){delete c.dataset.queued;c.querySelectorAll('input').forEach(r=>r.disabled=false);if(c.dataset.attentionJob!==job.job_id){choose(c,'',true);c.dataset.attentionJob=job.job_id;}}}remember();count();if(job.status==='complete')break;await wait();job=await request('/unsubscribe/batches/'+job.job_id);} }catch(error){notice.textContent='Progress temporarily unavailable. Queued actions remain saved; reload to check them.';}finally{watched.delete(job.job_id);busy(job.job_id,false);}}
 let sending=false;let pending=null;
 apply.addEventListener('click',async()=>{if(sending)return;const selected=choices();if(!pending&&!selected.length){notice.textContent='Choose an action for at least one sender.';return;}pending=pending||{request_key:[...crypto.getRandomValues(new Uint8Array(16))].map(value=>value.toString(16).padStart(2,'0')).join(''),selections:JSON.stringify(selected)};sending=true;apply.disabled=true;apply.setAttribute('aria-busy','true');busy('submit',true);notice.textContent='Saving selected actions…';try{const job=await request('/unsubscribe/batches',pending);pending=null;notice.textContent='Selected actions are queued. You can continue reviewing other senders.';follow(job,true);}catch(error){if(error.status>=400&&error.status<500){pending=null;for(const id of error.unavailableIds||[]){const c=cardFor(id);if(!c)continue;c.dataset.queued='true';c.querySelectorAll('input[type=radio]').forEach(r=>{r.checked=false;r.disabled=true;});const controls=c.querySelector('fieldset');if(controls)controls.hidden=true;c.querySelector('.subscription-result').textContent='Already handled or queued. Excluded from this batch.';}remember();count();notice.textContent=error.message+' Your remaining choices are preserved. Apply selected actions to submit them.';}else{notice.textContent=error.message+' Retry Apply selected actions to confirm this same request.';}}finally{sending=false;apply.disabled=false;apply.removeAttribute('aria-busy');busy('submit',false);}});
 function suggestions(job){for(const suggestion of job.recommendations||[]){const c=cardFor(suggestion.candidate_id);if(!c||c.dataset.manual==='true'||c.dataset.queued)continue;choose(c,suggestion.action,false);c.dataset.recommended='true';c.querySelector('.subscription-result').textContent='AI suggestion: '+suggestion.reason;}remember();count();}
 generate.addEventListener('click',async()=>{if(generate.disabled)return;generate.disabled=true;generate.setAttribute('aria-busy','true');busy('generate',true);for(const c of cards()){if(c.dataset.guidanceSaved==='true'&&c.dataset.instructionCleared==='true'&&!c.dataset.queued){delete c.dataset.manual;delete c.dataset.guidanceSaved;delete c.dataset.instructionCleared;}}remember();notice.textContent='Generating suggestions for the next batch…';try{let job=await request('/unsubscribe/suggestions',{exclude_ids:JSON.stringify(cards().filter(c=>c.dataset.manual==='true'||c.dataset.queued).map(c=>Number(c.dataset.subscriptionId)))});suggestions(job);while(job.status!=='complete'&&job.status!=='failed'){notice.textContent=job.error||((job.status==='queued'?'Waiting for the background worker. ':'Generating suggestions. ')+(job.completed||0)+' of '+(job.total||0)+' processed. '+((job.recommendations||[]).length?(job.recommendations.length+' suggestions ready for review.'):'No suggestions are ready yet.'));await wait();job=await request('/unsubscribe/suggestions/'+job.job_id);suggestions(job);}notice.textContent=job.error||((job.recommendations||[]).length?'Suggestions are selected for review. Nothing is applied until you submit.':'No new suggestions in this batch. Your current choices are unchanged.');}catch(error){notice.textContent=error.message;}finally{generate.disabled=false;generate.removeAttribute('aria-busy');busy('generate',false);}});
 count(false);request('/unsubscribe/batches').then(jobs=>jobs.forEach(job=>follow(job))).catch(()=>{notice.textContent='Saved progress could not be loaded. Reload before submitting existing work.';});
})();
</script>'''

BAR = '''<style>
@keyframes tahor-spin { to { transform: rotate(360deg); } }
.tahor-working { display:inline-flex;align-items:center;gap:.55rem;margin:.75rem 0 0; }
.tahor-working[hidden] { display:none; }
.tahor-working::before, #subscription-tools button[aria-busy="true"]::before { content:"";display:inline-block;width:.9em;height:.9em;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:tahor-spin .8s linear infinite;flex-shrink:0; }
#subscription-tools button[aria-busy="true"]::before { margin-right:.5em;vertical-align:-.12em; }
@media (prefers-reduced-motion:reduce) { .tahor-working::before, #subscription-tools button[aria-busy="true"]::before { animation:none; } }
</style><div id="subscription-tools" style="position:sticky;top:0;z-index:20;background:var(--raised,#10302C);padding:1rem;border:1px solid var(--rule,#1C3B37);border-radius:.6rem;box-shadow:0 3px 12px #0002">
<form method="post" action="/unsubscribe/batches" onsubmit="return false">
<div class="actions"><button type="button" data-generate>Generate AI suggestions</button><button type="button" data-apply class="primary">Apply selected actions (<span data-selected-count>0</span>)</button></div>
<details style="margin-top:.65rem"><summary>Choose one action for all shown senders</summary><label>Action <select data-bulk-choice><option value="">No action</option><option value="unsubscribe_block_marketing">Stop marketing, keep transactions</option><option value="unsubscribe">Unsubscribe only</option><option value="dismiss">Keep subscription</option><option value="block_all">Unsubscribe and block all mail, including receipts</option></select></label> <button type="button" data-select-all>Select for all</button></details>
<p data-bulk-activity class="tahor-working" role="status" hidden>Working…</p>
<p data-bulk-status role="status" aria-live="polite">Choose actions below, then apply them together. AI suggestions do not send requests.</p>
</form></div>'''
