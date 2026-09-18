"""Reviewable AI choices and durable batches for pending message and filing decisions."""
SCRIPT = r'''<script>
(() => {
 let previous=null;
 function initialize(){
 if(previous)previous.abort();const controller=new AbortController();previous=controller;const signal=controller.signal;
 const bar=document.querySelector('#decision-tools'); if(!bar) return;
 const cards=()=>[...document.querySelectorAll('[data-bulk-eligible="true"]')];
 const cardFor=id=>document.querySelector('[data-decision-id="'+id+'"]');
 const notice=bar.querySelector('[data-bulk-status]');
 const apply=bar.querySelector('[data-apply]'); const generate=bar.querySelector('[data-generate]');
 const activity=bar.querySelector('[data-bulk-activity]');
 const activeWork=new Set();
 function busy(key,working){if(working)activeWork.add(key);else activeWork.delete(key);if(activity)activity.hidden=activeWork.size===0;}
 const csrf=bar.querySelector('input[name=csrf_token]').value;
 const storageKey='tahor-decision-choices';
 let saved={}; try{saved=JSON.parse(sessionStorage.getItem(storageKey)||'{}');}catch(_){}
 const choices=()=>cards().filter(c=>!c.dataset.queued).map(c=>({decision_id:Number(c.dataset.decisionId),action:c.querySelector('[data-decision-choice]:checked')?.value,source_revision:c.dataset.decisionRevision,bucket:c.querySelector('[data-bulk-bucket]')?.value,vendor_name:c.querySelector('[data-bulk-vendor]')?.value})).filter(c=>c.action);
 function remember(){const state={};for(const c of cards()){const value=c.querySelector('[data-decision-choice]:checked')?.value;if(value||c.dataset.manual==='true')state[c.dataset.decisionId]={action:value,manual:c.dataset.manual==='true',attentionJob:c.dataset.attentionJob||'',instructionCleared:c.dataset.instructionCleared==='true',guidanceSaved:c.dataset.guidanceSaved==='true',source_revision:c.dataset.decisionRevision,bucket:c.querySelector('[data-bulk-bucket]')?.value,vendor_name:c.querySelector('[data-bulk-vendor]')?.value};}try{sessionStorage.setItem(storageKey,JSON.stringify(state));}catch(_){}}
 function prioritize(preserve=true){
  const list=document.querySelector('#decision-cards');if(!list||document.querySelector('dialog.email-preview[open]'))return;
  const current=[...list.querySelectorAll('[data-decision-id]')];
  const hasRecommendation=c=>Boolean(c.querySelector('[data-decision-recommendation]')?.textContent.trim());
  const rank=c=>c.dataset.queued?3:(hasRecommendation(c)?0:(c.querySelector('[data-decision-choice]:checked')?.value?1:2));
  const ordered=[...current].sort((a,b)=>rank(a)-rank(b));
  if(ordered.every((c,i)=>c===current[i]))return;
  const edge=Math.max(0,bar.getBoundingClientRect().bottom);
  const anchor=preserve?current.find(c=>{const r=c.getBoundingClientRect();return r.bottom>edge&&r.top<window.innerHeight;}):null;
  const before=anchor?.getBoundingClientRect().top;const focused=document.activeElement;
  list.style.overflowAnchor='none';for(const c of ordered)list.append(c);
  if(anchor){const delta=anchor.getBoundingClientRect().top-before;if(delta)window.scrollBy(0,delta);}
  if(focused?.isConnected&&list.contains(focused))focused.focus({preventScroll:true});
 }
 document.addEventListener('close',()=>setTimeout(()=>prioritize(),0),{capture:true,signal});
 function count(preserve=true){bar.querySelector('[data-selected-count]').textContent=String(choices().length);prioritize(preserve);}
 function choose(c,action,manual){const radio=[...c.querySelectorAll('[data-decision-choice]')].find(r=>r.value===action);if(radio&&!c.dataset.queued){radio.checked=true;if(manual)c.dataset.manual='true';}}
 for(const c of cards()){const stored=saved[c.dataset.decisionId];const old=stored?.source_revision===c.dataset.decisionRevision?stored:null;if(old?.attentionJob)c.dataset.attentionJob=old.attentionJob;if(old?.instructionCleared)c.dataset.instructionCleared='true';if(old?.guidanceSaved)c.dataset.guidanceSaved='true';if(old?.manual){choose(c,old.action,true);if(c.querySelector('[data-bulk-bucket]')&&old.bucket!==undefined)c.querySelector('[data-bulk-bucket]').value=old.bucket;if(c.querySelector('[data-bulk-vendor]')&&old.vendor_name!==undefined)c.querySelector('[data-bulk-vendor]').value=old.vendor_name;}}
 document.addEventListener('card-guidance-state',()=>remember(),{signal});
 document.addEventListener('change',event=>{const c=event.target.closest('[data-decision-id]');if(c&&event.target.matches('[data-decision-choice],[data-bulk-bucket],[data-bulk-vendor]')){if(event.isTrusted){delete c.dataset.instructionCleared;delete c.dataset.guidanceSaved;}c.dataset.manual='true';remember();count();}},{signal});
 bar.querySelector('[data-select-all]').addEventListener('click',()=>{const action=bar.querySelector('[data-bulk-choice]').value;for(const c of cards())choose(c,action,true);remember();count();});
 async function request(url,values){const options={credentials:'same-origin',headers:{Accept:'application/json'}};if(values){options.method='POST';options.body=new URLSearchParams({csrf_token:csrf,...values});}const response=await fetch(url,options);const body=await response.json();if(!response.ok){const error=new Error(body.error||'The request could not be confirmed.');error.status=response.status;error.unavailableIds=body.unavailable_ids;throw error;}return body;}
 const wait=()=>new Promise(resolve=>setTimeout(resolve,2000));
 const watched=new Set();
 async function follow(job,replace=false){if(watched.has(job.job_id))return;watched.add(job.job_id);busy(job.job_id,job.status!=='complete');for(const item of job.items){const c=cardFor(item.decision_id);if(c&&(replace===true||!c.dataset.actionJob))c.dataset.actionJob=job.job_id;}try{while(true){if(signal.aborted)return;for(const item of job.items){const c=cardFor(item.decision_id);if(!c||c.dataset.actionJob!==job.job_id)continue;c.dataset.queued='true';c.querySelectorAll('[data-decision-choice]').forEach(r=>r.disabled=true);c.querySelector('.decision-result').textContent=item.message;if(item.status==='done'||item.status==='attention'||item.status==='uncertain'){c.dataset.finished='true';}if(item.status==='done'){c.querySelector('.decision-result').textContent='Completed: '+item.message;const controls=c.querySelector('[data-decision-choices]');if(controls)controls.hidden=true;}if(item.status==='attention'&&item.retry_allowed===false){const controls=c.querySelector('[data-decision-choices]');if(controls)controls.hidden=true;}if(item.status==='attention'&&item.retry_allowed!==false){delete c.dataset.queued;c.querySelectorAll('[data-decision-choice]').forEach(r=>r.disabled=false);if(c.dataset.attentionJob!==job.job_id){choose(c,'',true);c.dataset.attentionJob=job.job_id;}}}remember();count();if(job.status==='complete')break;await wait();job=await request('/decisions/batches/'+job.job_id);} }catch(error){notice.textContent='Progress temporarily unavailable. Queued actions remain saved; progress will be available when the connection recovers.';}finally{watched.delete(job.job_id);busy(job.job_id,false);}}
 let sending=false;let pending=null;
 apply.addEventListener('click',async()=>{if(sending)return;const selected=choices();if(!pending&&selected.some(item=>item.action==='map'&&(!item.bucket?.trim()||!item.vendor_name?.trim()))){notice.textContent='Enter a folder and vendor for each selected filing action.';return;}if(!pending&&!selected.length){notice.textContent='Choose an action for at least one sender.';return;}pending=pending||{request_key:[...crypto.getRandomValues(new Uint8Array(16))].map(value=>value.toString(16).padStart(2,'0')).join(''),selections:JSON.stringify(selected)};sending=true;apply.disabled=true;apply.setAttribute('aria-busy','true');busy('submit',true);notice.textContent='Saving selected actions…';try{const job=await request('/decisions/batches',pending);pending=null;notice.textContent='Selected actions are queued. You can continue reviewing other decisions.';follow(job,true);}catch(error){if(error.status>=400&&error.status<500){pending=null;for(const id of error.unavailableIds||[]){const c=cardFor(id);if(!c)continue;c.dataset.queued='true';c.querySelectorAll('[data-decision-choice]').forEach(r=>{r.checked=false;r.disabled=true;});const controls=c.querySelector('[data-decision-choices]');if(controls)controls.hidden=true;c.querySelector('.decision-result').textContent='Already handled or queued. Excluded from this batch.';}remember();count();notice.textContent=error.message+' Your remaining choices are preserved. Apply selected actions to submit them.';}else{notice.textContent=error.message+' Retry Apply selected actions to confirm this same request.';}}finally{sending=false;apply.disabled=false;apply.removeAttribute('aria-busy');busy('submit',false);}});
 function suggestions(job){if(signal.aborted)return;for(const suggestion of job.recommendations||[]){const c=cardFor(suggestion.decision_id);if(!c||c.dataset.manual==='true'||c.dataset.queued)continue;c.dataset.decisionRevision=suggestion.source_revision;const bucket=c.querySelector('[data-bulk-bucket]');const vendor=c.querySelector('[data-bulk-vendor]');if(bucket&&suggestion.bucket)bucket.value=suggestion.bucket;if(vendor&&suggestion.vendor_name)vendor.value=suggestion.vendor_name;choose(c,suggestion.action,false);c.dataset.recommended='true';let reason=c.querySelector('[data-decision-recommendation]');if(!reason){reason=document.createElement('p');reason.dataset.decisionRecommendation='';c.querySelector('[data-decision-choices]').before(reason);}reason.textContent='AI suggestion: '+suggestion.reason;}remember();count();}
 generate.addEventListener('click',async()=>{if(generate.disabled)return;generate.disabled=true;generate.setAttribute('aria-busy','true');busy('generate',true);for(const c of cards()){if(c.dataset.guidanceSaved==='true'&&c.dataset.instructionCleared==='true'&&!c.dataset.queued){delete c.dataset.manual;delete c.dataset.guidanceSaved;delete c.dataset.instructionCleared;}}remember();notice.textContent='Generating suggestions for the next batch…';try{let job=await request('/decisions/suggestions',{exclude_ids:JSON.stringify(cards().filter(c=>c.dataset.manual==='true'||c.dataset.queued).map(c=>Number(c.dataset.decisionId)))});suggestions(job);while(job.status!=='complete'&&job.status!=='failed'){if(signal.aborted)return;notice.textContent=job.error||((job.status==='queued'?'Waiting for the background worker. ':'Generating suggestions. ')+(job.completed||0)+' of '+(job.total||0)+' processed. '+((job.recommendations||[]).length?(job.recommendations.length+' suggestions ready for review.'):'No suggestions are ready yet.'));await wait();job=await request('/decisions/suggestions/'+job.job_id);suggestions(job);}notice.textContent=job.error||((job.recommendations||[]).length?'Suggestions are selected for review. Nothing is applied until you submit.':'No new suggestions in this batch. Your current choices are unchanged.');}catch(error){notice.textContent=error.message;}finally{generate.disabled=false;generate.removeAttribute('aria-busy');busy('generate',false);}});
 count(false);request('/decisions/batches').then(jobs=>jobs.forEach(job=>follow(job))).catch(()=>{notice.textContent='Saved progress could not be loaded. Reload before submitting existing work.';});
 }
 initialize();document.addEventListener('tahor-decisions-updated',initialize);
})();
</script>'''

BAR = '''<style>
@keyframes tahor-spin { to { transform: rotate(360deg); } }
.tahor-working { display:inline-flex;align-items:center;gap:.55rem;margin:.75rem 0 0; }
.tahor-working[hidden] { display:none; }
.tahor-working::before, #decision-tools button[aria-busy="true"]::before { content:"";display:inline-block;width:.9em;height:.9em;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:tahor-spin .8s linear infinite;flex-shrink:0; }
#decision-tools button[aria-busy="true"]::before { margin-right:.5em;vertical-align:-.12em; }
@media (prefers-reduced-motion:reduce) { .tahor-working::before, #decision-tools button[aria-busy="true"]::before { animation:none; } }
</style><div id="decision-tools" style="position:sticky;top:0;z-index:20;background:var(--raised,#10302C);padding:1rem;border:1px solid var(--rule,#1C3B37);border-radius:.6rem;box-shadow:0 3px 12px #0002">
<form method="post" action="/decisions/batches" onsubmit="return false">
<div class="actions"><button type="button" data-generate>Generate AI suggestions</button><button type="button" data-apply class="primary">Apply selected actions (<span data-selected-count>0</span>)</button></div>
<details style="margin-top:.65rem"><summary>Choose one action for all shown messages</summary><label>Action <select data-bulk-choice><option value="">No action</option><option value="keep">Keep</option><option value="keep_brief">Keep briefly</option><option value="skip">Keep decision</option><option value="trash">Trash permanently</option></select></label> <button type="button" data-select-all>Select for all</button></details>
<p data-bulk-activity class="tahor-working" role="status" hidden>Working…</p>
<p data-bulk-status role="status" aria-live="polite">Choose actions below, then apply them together. Suggestions require your review. Rule proposals still require separate approval.</p>
</form></div>'''


def choices(row, revision, suggestion=None, buckets=()):
    from html import escape
    options = ([('keep', 'Keep'), ('keep_brief', 'Keep briefly'), ('trash', 'Trash permanently'), ('skip', 'Skip for now')]
               if row['kind'] == 'message_review' else [('map', 'Use this filing folder'), ('unsorted', 'Leave unsorted')])
    selected = (suggestion or {}).get('action', '')
    radios = ''.join(f'<label style="display:block"><input data-decision-choice type="radio" name="decision-choice-{row["id"]}" value="{value}"{" checked" if selected == value else ""}> {label}</label>' for value, label in [('', 'No action yet')] + options)
    fields = ''
    if row['kind'] == 'vendor_mapping':
        import json
        try:
            context = json.loads(row['context'] or '{}')
        except (ValueError, TypeError):
            context = {}
        bucket = (suggestion or {}).get('bucket') or context.get('suggested_bucket') or ''
        vendor = (suggestion or {}).get('vendor_name') or context.get('suggested_vendor') or context.get('display_name') or ''
        fields = (f'<div class="fields" style="margin-top:.75rem"><label>Folder <input type="text" data-bulk-bucket list="decision-folders-{row["id"]}" value="{escape(bucket)}"></label>'
                  f'<datalist id="decision-folders-{row["id"]}">' + ''.join(f'<option value="{escape(b)}">' for b in buckets) + '</datalist>'
                  f'<label>Sender or organization <input type="text" data-bulk-vendor value="{escape(vendor)}"></label></div>'
                  '<p class="hint">Receipts go in a Receipts subfolder; other retained mail goes in Correspondence. Inbox timing, attention protections, and deletion rules still apply. Business receipt rules can use their own year folders.</p>')
    reason = 'AI suggestion: ' + suggestion.get('reason', '') if suggestion else ''
    return f'<p data-decision-recommendation>{escape(reason)}</p><fieldset data-decision-choices><legend>Choose an action for this batch</legend>{radios}{fields}</fieldset><p class="decision-result" role="status"></p>'
