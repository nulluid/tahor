"""Keep decision actions on the current page and preserve the visible card position."""
SCRIPT = r'''<script>
(() => {
  const busy = new Set();
  const skipped = new Set();
  let chain = Promise.resolve();
  function message(card, text) {
    let output = card.querySelector('.decision-result');
    if (!output) {
      output = document.createElement('p');
      output.className = 'decision-result';
      output.setAttribute('role', 'status');
      output.setAttribute('aria-live', 'polite');
      card.append(output);
    }
    output.textContent = text;
  }
  function cardFor(id) { return document.querySelector('[data-decision-id="' + id + '"]'); }
  async function submit(job) {
    let card = cardFor(job.id);
    if (card) message(card, 'Working…');
    try {
      const response = await fetch(job.url, {method:'POST', body:job.data, credentials:'same-origin'});
      if (!response.ok || new URL(response.url, window.location.href).pathname !== '/') {
        throw new Error(response.status === 409 ? 'This decision changed. Reload its current state before trying again.' : 'The action could not be confirmed. Reload to check its status before retrying.');
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = page.querySelector('main');
      if (!replacement) throw new Error('The result could not be loaded. Reload to check its status.');
      const current = document.querySelector('main');
      const visible = [...current.querySelectorAll('[data-decision-id]')];
      card = cardFor(job.id);
      let anchor = card;
      if (!anchor || anchor.getBoundingClientRect().bottom < 0 || anchor.getBoundingClientRect().top > window.innerHeight) {
        anchor = visible.find(item => item.getBoundingClientRect().bottom > 0);
      }
      const index = visible.indexOf(anchor);
      const nextId = visible[index + 1]?.dataset.decisionId;
      const anchorId = anchor?.dataset.decisionId;
      const top = anchor?.getBoundingClientRect().top;
      const scroll = window.scrollY;
      if (job.skip) skipped.add(job.id);
      let hidden = 0;
      for (const id of skipped) {
        const item = replacement.querySelector('[data-decision-id="' + id + '"]');
        if (item) { item.remove(); hidden += 1; }
      }
      const count = replacement.querySelector('h1 .count');
      if (count) count.textContent = String(Math.max(0, Number(count.textContent) - hidden));
      // Keep edits in other cards while this request was in flight.
      for (const other of visible) {
        if (other.dataset.decisionId === job.id) continue;
        const fresh = replacement.querySelector('[data-decision-id="' + other.dataset.decisionId + '"]');
        if (!fresh) continue;
        const oldFields = [...other.querySelectorAll('input,select,textarea')];
        const newFields = [...fresh.querySelectorAll('input,select,textarea')];
        oldFields.forEach((field, index) => {
          const target = newFields[index];
          if (!target || target.name !== field.name || field.type === 'hidden') return;
          const bulkField=field.matches('[data-decision-choice],[data-bulk-bucket],[data-bulk-folder],[data-bulk-custom-folder],[data-bulk-vendor]');
          if(bulkField&&other.dataset.decisionRevision!==fresh.dataset.decisionRevision)return;
          if (field.type === 'checkbox' || field.type === 'radio') { if(target.value===field.value)target.checked=field.checked; }
          else target.value = field.value;
          if(bulkField&&other.dataset.manual==='true')fresh.dataset.manual='true';
        });
      }
      current.replaceWith(replacement);
      document.dispatchEvent(new Event('tahor-decisions-updated'));
      const destination = (anchorId && cardFor(anchorId)) || (nextId && cardFor(nextId));
      if (destination && Number.isFinite(top)) window.scrollBy(0, destination.getBoundingClientRect().top - top);
      else window.scrollTo(0, scroll);
      for (const id of busy) {
        const waiting = cardFor(id);
        if (waiting) { waiting.querySelectorAll('button').forEach(button => button.disabled = true); message(waiting, 'Saving your choice…'); }
      }
    } catch (error) {
      const current = cardFor(job.id);
      if (current) message(current, error.message || 'The result could not be confirmed. Reload to check it.');
    } finally {
      busy.delete(job.id);
      const current = cardFor(job.id);
      if (current) current.querySelectorAll('button').forEach(button => button.disabled = false);
    }
  }
  document.addEventListener('submit', event => {
    const form = event.target.closest('form');
    const card = form?.closest('[data-decision-id]');
    if (!card || !/^\/(resolve|vendor-message|review-rule|retry-rule|clarify-rule|message-details|dismiss-sieve)\//.test(new URL(form.getAttribute('action'), window.location.href).pathname)) return;
    event.preventDefault();
    const id = card.dataset.decisionId;
    if (busy.has(id)) return;
    const data = new FormData(form);
    if (event.submitter?.name) data.set(event.submitter.name, event.submitter.value);
    const skip = data.get('action') === 'skip' && card.dataset.decisionKind === 'message_review';
    busy.add(id);
    card.querySelectorAll('button').forEach(button => button.disabled = true);
    message(card, 'Saving your choice…');
    chain = chain.then(() => submit({id, url:form.getAttribute('action'), data, skip}));
  });
})();
</script>'''
