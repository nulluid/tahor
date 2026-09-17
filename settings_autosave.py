"""Settings forms save serially without navigating away from the current section."""
STATUS = '<p class="autosave-status hint" role="status" aria-live="polite">Changes save automatically.</p><button type="button" class="autosave-retry" hidden>Retry</button>'

SCRIPT = r'''<script>
(() => {
  const pending = new Map();
  const states = new Map();
  let running = false;
  let uncertain = false;
  function show(form, message, error = false) {
    form.querySelector('.autosave-status').textContent = message;
    form.querySelector('.autosave-retry').hidden = !error;
  }
  async function drain() {
    if (running || uncertain) return;
    running = true;
    while (pending.size && !uncertain) {
      const [form, job] = pending.entries().next().value;
      pending.delete(form);
      const state = states.get(form);
      form.setAttribute('aria-busy', 'true');
      show(form, 'Saving…');
      try {
        const response = await fetch(form.action, {method: 'POST', body: job.data,
          credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
          throw new Error('Unconfirmed response');
        }
        const result = await response.json();
        if (!response.ok || result.saved !== true) {
          throw {confirmed: true, message: result.message || 'Check the values in this section.'};
        }
        if (state.version === job.version) {
          show(form, 'Saved');
          const enabled = form.querySelector('.ai-enabled-status');
          if (enabled && typeof result.enabled === 'boolean') enabled.textContent = result.enabled ? 'Enabled' : 'Disabled';
          if (result.rule) {
            const card = form.closest('.card');
            for (const key of ['name', 'match', 'instructions', 'signature']) {
              const display = card?.querySelector('[data-rule-display="' + key + '"]');
              if (display) display.textContent = result.rule[key] || '';
            }
          }
        }
      } catch (error) {
        if (error.confirmed) {
          if (state.version === job.version) {
            state.key = null;
            show(form, 'Not saved: ' + error.message, true);
          }
        } else {
          uncertain = true;
          for (const changed of new Set([form, ...pending.keys()])) {
            show(changed, 'Could not confirm the save. Reload to check saved settings before continuing.', true);
            changed.querySelector('.autosave-retry').textContent = 'Reload settings';
          }
        }
      } finally {
        form.removeAttribute('aria-busy');
      }
    }
    running = false;
  }
  function enqueue(form, force = false) {
    const state = states.get(form);
    clearTimeout(state.timer);
    state.timer = null;
    if (uncertain) {
      show(form, 'Could not confirm the save. Reload to check saved settings before continuing.', true);
      form.querySelector('.autosave-retry').textContent = 'Reload settings';
      return;
    }
    const data = new FormData(form);
    const key = new URLSearchParams(data).toString();
    if (!force && state.key === key) return;
    state.key = key;
    state.version += 1;
    if (!form.checkValidity()) {
      pending.delete(form);
      show(form, 'Not saved: complete the required fields with valid values.', true);
      return;
    }
    pending.set(form, {data, version: state.version});
    show(form, 'Saving…');
    drain();
  }
  document.querySelectorAll('form[data-autosave]').forEach(form => {
    const state = {version: 0, timer: null, key: new URLSearchParams(new FormData(form)).toString()};
    states.set(form, state);
    form.addEventListener('submit', event => { event.preventDefault(); enqueue(form, true); });
    form.addEventListener('change', () => {
      form.querySelectorAll('.mode-option').forEach(card => card.classList.toggle('active', card.querySelector('input').checked));
      enqueue(form);
    });
    form.addEventListener('input', event => {
      if (['radio', 'checkbox', 'hidden'].includes(event.target.type) || event.target.tagName === 'SELECT') return;
      clearTimeout(state.timer);
      state.version += 1;
      show(form, 'Unsaved changes…');
      state.timer = setTimeout(() => enqueue(form), 500);
    });
    form.addEventListener('focusout', () => enqueue(form));
    form.querySelector('.autosave-retry').addEventListener('click', () => {
      if (uncertain) window.location.reload();
      else enqueue(form, true);
    });
  });
  window.addEventListener('beforeunload', event => {
    if (running || pending.size || [...states.values()].some(state => state.timer)) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
})();
</script>'''
