"""Inert subscription choices, explicit batch submission, and local progress."""
SCRIPT = r'''<script>
(() => {
 const bar=document.querySelector('#subscription-tools'); if(!bar) return;
 const cards=()=>[...document.querySelectorAll('[data-subscription-id]')];
 const cardFor=id=>document.querySelector('[data-subscription-id="'+id+'"]');
 const notice=bar.querySelector('[data-bulk-status]');
 const apply=bar.querySelector('[data-apply]'); const generate=bar.querySelector('[data-generate]');
 const csrf=bar.querySelector('input[name=csrf_token]').value;
 const storageKey='tahor-subscription-choices';
 let saved={}; try{saved=JSON.parse(sessionStorage.getItem(storageKey)||'{}');}catch(_){}
 const choices=()=>cards().filter(c=>!c.dataset.queued).map(c=>({candidate_id:Number(c.dataset.subscriptionId),action:c.querySelector('input:checked')?.value})).filter(c=>c.action);
 function remember(){const state={};for(const c of cards()){const value=c.querySelector('input:checked')?.value;if(value||c.dataset.manual==='true')state[c.dataset.subscriptionId]={action:value,manual:c.dataset.manual==='true'};}try{sessionStorage.setItem(storageKey,JSON.stringify(state));}catch(_){}}
 function count(){bar.querySelector('[data-selected-count]').textContent=String(choices().length);}
 function choose(c,action,manual){const radio=[...c.querySelectorAll('input[type="radio"]')].find(r=>r.value===action);if(radio&&!c.dataset.queued){radio.checked=true;if(manual)c.dataset.manual='true';}}
 for(const c of cards()){const old=saved[c.dataset.subscriptionId];if(old)choose(c,old.action,old.manual);}
 document.addEventListener('change',event=>{const c=event.target.closest('[data-subscription-id]');if(c&&event.target.type==='radio'){c.dataset.manual='true';remember();count();}});
 bar.querySelector('[data-select-all]').addEventListener('click',()=>{const action=bar.querySelector('[data-bulk-choice]').value;for(const c of cards())choose(c,action,true);remember();count();});
 async function request(url,values){const options={credentials:'same-origin',headers:{Accept:'application/json'}};if(values){options.method='POST';options.body=new URLSearchParams({csrf_token:csrf,...values});}const response=await fetch(url,options);const body=await response.json();if(!response.ok)throw new Error(body.error||'The request could not be confirmed.');return body;}
 const wait=()=>new Promise(resolve=>setTimeout(resolve,2000));
 const watched=new Set();
 async function follow(job){if(watched.has(job.job_id))return;watched.add(job.job_id);try{while(true){for(const item of job.items){const c=cardFor(item.candidate_id);if(!c)continue;c.dataset.queued='true';c.querySelectorAll('input').forEach(r=>r.disabled=true);c.querySelector('.subscription-result').textContent=item.message;if(item.status==='done'||item.status==='attention'||item.status==='uncertain'){c.dataset.finished='true';}if(item.status==='attention'){delete c.dataset.queued;c.querySelectorAll('input').forEach(r=>r.disabled=false);choose(c,'',true);}}remember();count();if(job.status==='complete')break;await wait();job=await request('/unsubscribe/batches/'+job.job_id);} }catch(error){notice.textContent='Progress temporarily unavailable. Queued actions remain saved; reload to check them.';}finally{watched.delete(job.job_id);}}
 let sending=false;let pending=null;
 apply.addEventListener('click',async()=>{if(sending)return;const selected=choices();if(!pending&&!selected.length){notice.textContent='Choose an action for at least one sender.';return;}pending=pending||{request_key:[...crypto.getRandomValues(new Uint8Array(16))].map(value=>value.toString(16).padStart(2,'0')).join(''),selections:JSON.stringify(selected)};sending=true;apply.disabled=true;notice.textContent='Saving selected actions…';try{const job=await request('/unsubscribe/batches',pending);pending=null;notice.textContent='Selected actions are queued. You can continue reviewing other senders.';follow(job);}catch(error){notice.textContent=error.message+' Retry Apply selected actions to confirm this same request.';}finally{sending=false;apply.disabled=false;}});
 function suggestions(job){for(const suggestion of job.recommendations||[]){const c=cardFor(suggestion.candidate_id);if(!c||c.dataset.manual==='true'||c.dataset.queued)continue;choose(c,suggestion.action,false);c.querySelector('.subscription-result').textContent='AI suggestion: '+suggestion.reason;}remember();count();}
 generate.addEventListener('click',async()=>{if(generate.disabled)return;generate.disabled=true;notice.textContent='Generating suggestions for the next batch…';try{let job=await request('/unsubscribe/suggestions',{exclude_ids:JSON.stringify(cards().filter(c=>c.dataset.manual==='true'||c.dataset.queued).map(c=>Number(c.dataset.subscriptionId)))});suggestions(job);while(job.status!=='complete'&&job.status!=='failed'){notice.textContent=job.error||'Generating suggestions… Completed choices are ready to review.';await wait();job=await request('/unsubscribe/suggestions/'+job.job_id);suggestions(job);}notice.textContent=job.error||'Suggestions are selected for review. Nothing is applied until you submit.';}catch(error){notice.textContent=error.message;}finally{generate.disabled=false;}});
 count();request('/unsubscribe/batches').then(jobs=>jobs.forEach(follow)).catch(()=>{notice.textContent='Saved progress could not be loaded. Reload before submitting existing work.';});
})();
</script>'''

BAR = '''<div id="subscription-tools" style="position:sticky;top:0;z-index:20;background:var(--raised,#10302C);padding:1rem;border:1px solid var(--rule,#1C3B37);border-radius:.6rem;box-shadow:0 3px 12px #0002">
<form method="post" action="/unsubscribe/batches" onsubmit="return false">
<div class="actions"><button type="button" data-generate>Generate AI suggestions</button><button type="button" data-apply class="primary">Apply selected actions (<span data-selected-count>0</span>)</button></div>
<details style="margin-top:.65rem"><summary>Choose one action for all shown senders</summary><label>Action <select data-bulk-choice><option value="">No action</option><option value="unsubscribe_block_marketing">Stop marketing, keep transactions</option><option value="unsubscribe">Unsubscribe only</option><option value="dismiss">Keep subscription</option><option value="block_all">Unsubscribe and block all mail, including receipts</option></select></label> <button type="button" data-select-all>Select for all</button></details>
<p data-bulk-status role="status" aria-live="polite">Choose actions below, then apply them together. AI suggestions do not send requests.</p>
</form></div>'''
