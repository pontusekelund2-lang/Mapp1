// Lägger automatiskt till CSRF-skydd i alla formulär.
(function () {
  const token = document.querySelector('meta[name="csrf"]').content;
  document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(f => {
    if (!f.querySelector('input[name="csrf"]')) {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = 'csrf'; i.value = token;
      f.appendChild(i);
    }
  });
  // Bekräftelse för knappar med data-confirm
  document.querySelectorAll('[data-confirm]').forEach(b => {
    b.addEventListener('click', e => { if (!confirm(b.dataset.confirm)) e.preventDefault(); });
  });
  // Förhindra dubbelklick på knappar som startar jobb
  document.querySelectorAll('form').forEach(f => {
    f.addEventListener('submit', e => {
      const b = e.submitter;
      if (b && b.dataset.busy) { setTimeout(() => { b.disabled = true; b.textContent = b.dataset.busy; }, 0); }
    });
  });
  window.dropagentCsrf = token;
})();

async function testConnection(what, el) {
  const out = document.getElementById('test-' + what);
  out.className = 'test-result'; out.textContent = 'Testar…';
  el.disabled = true;
  try {
    const r = await fetch('/api/test/' + what, { method: 'POST', headers: { 'X-CSRF': window.dropagentCsrf } });
    const d = await r.json();
    out.className = 'test-result ' + (d.ok ? 'ok' : 'err');
    out.textContent = (d.ok ? '✓ ' : '✗ ') + d.message;
  } catch (e) {
    out.className = 'test-result err'; out.textContent = '✗ ' + e;
  }
  el.disabled = false;
}
