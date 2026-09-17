"""Accessible, read-only email previews without leaving the working page."""
SCRIPT = r'''<script data-email-preview>
(() => {
  const allowed = /^\/(?:message\/\d+(?:\/\d+)?|subscription-messages\/\d+|subscription-message\/\d+\/\d+|expenses\/message\/\d+)$/;
  let dialog, content, opener, controller, previousOverflow, previousScroll;
  function destination(value) {
    const url = new URL(value, window.location.href);
    return url.origin === window.location.origin && allowed.test(url.pathname) ? url : null;
  }
  function create() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.className = 'email-preview';
    dialog.setAttribute('aria-label', 'Email preview');
    dialog.innerHTML = '<div class="email-preview-bar"><strong>Email preview</strong><button type="button" aria-label="Close email preview">Close</button></div><div class="email-preview-content" aria-live="polite"></div>';
    const style = document.createElement('style');
    style.textContent = '.email-preview{width:min(850px,calc(100vw - 2rem));max-height:85vh;padding:0;border:1px solid var(--rule,#777);border-radius:12px;background:var(--ground,#fff);color:var(--ink,#111)}.email-preview::backdrop{background:rgba(0,0,0,.65)}.email-preview-bar{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem;background:var(--raised,#eee);position:sticky;top:0;z-index:1}.email-preview-content{padding:1.25rem;overflow:auto;max-height:calc(85vh - 5rem);overscroll-behavior:contain;overflow-wrap:anywhere}.email-preview-content main{max-width:none;margin:0}.email-preview-content pre{white-space:pre-wrap;overflow-wrap:anywhere}';
    document.head.append(style);
    document.body.append(dialog);
    content = dialog.querySelector('.email-preview-content');
    dialog.querySelector('button').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      controller?.abort();
      document.body.style.overflow = previousOverflow;
      window.scrollTo(0, previousScroll);
      if (opener?.isConnected) opener.focus({preventScroll:true});
    });
    dialog.addEventListener('click', event => {if(event.target === dialog) dialog.close();});
  }
  async function load(url, options = {}) {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    content.textContent = 'Loading email…';
    content.setAttribute('aria-busy','true');
    try {
      const response = await fetch(url.href, {credentials:'same-origin', ...options, signal:current.signal});
      if (current.signal.aborted) return;
      if (!destination(response.url)) throw new Error('Your session may have expired. Close this preview and reload the page.');
      if (!response.ok) {
        const reason = (await response.text()).trim();
        throw new Error(reason && reason.length <= 600 && !/[<>]/.test(reason) ? reason : 'This email could not be opened. Close this preview and try again.');
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (current.signal.aborted) return;
      const main = page.querySelector('main');
      if (!main) throw new Error('The preview could not be loaded. Close it and try again.');
      main.querySelector('header')?.remove();
      // Navigation within message examples stays in this modal. The page's own
      // navigation is redundant with Close and must not discard current work.
      main.querySelectorAll('a[href="/unsubscribe"], a[href="/"]').forEach(link => link.remove());
      content.replaceChildren(main);
      content.scrollTop = 0;
    } catch (error) {
      if (current.signal.aborted) return;
      content.textContent = error.message || 'The preview could not be loaded. Try again.';
    } finally {
      if (controller === current) content.removeAttribute('aria-busy');
    }
  }
  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (!link) return;
    const url = destination(link.getAttribute('href'));
    if (!url) return;
    event.preventDefault();
    create();
    if (!dialog.open) {
      opener = link;
      previousOverflow = document.body.style.overflow;
      previousScroll = window.scrollY;
      document.body.style.overflow = 'hidden';
      dialog.showModal();
    }
    load(url);
  });
  document.addEventListener('submit', event => {
    const form = event.target.closest('form');
    if (!dialog?.open || !form || !dialog.contains(form)) return;
    const url = destination(form.getAttribute('action'));
    if (!url || !/^\/subscription-messages\/\d+$/.test(url.pathname)) return;
    event.preventDefault();
    const data = new FormData(form);
    if(event.submitter?.name) data.set(event.submitter.name,event.submitter.value);
    load(url,{method:'POST',body:data});
  });
})();
</script>'''
