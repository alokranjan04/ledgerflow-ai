/* Agent desk: the hand-off queue with context. Admin token from ?admin=. */
'use strict';
const $ = (s) => document.querySelector(s);
const token = new URLSearchParams(location.search).get('admin') || '';
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function call(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { 'content-type': 'application/json', ...(token ? { 'X-Admin-Token': token } : {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function load() {
  $('#error').textContent = '';
  try {
    const q = await call('/api/agent/queue');
    $('#queue').innerHTML = q.items.length ? q.items.map(render).join('') : '<p class="muted">No hand-offs waiting.</p>';
  } catch (e) {
    $('#error').textContent = `Could not load the queue: ${e.message}`;
  }
}

function render(t) {
  const p = t.payload || {};
  const turns = (p.transcript || []).map((x) =>
    `<div class="turn"><b>${x.role === 'user' ? 'Customer' : 'Assistant'} (${esc(x.mode)}):</b> ${esc(x.text)}</div>`).join('');
  return `<div class="ticket">
    <div class="meta"><b>${esc(t.id)}</b>
      <span class="pill ${p.urgency === 'high' ? 'bad' : 'neutral'}">${esc(p.urgency || 'normal')}</span>
      <span class="pill ${t.status === 'open' ? 'warn' : 'good'}">${esc(t.status)}</span>
      <span class="pill ${p.verified ? 'good' : 'bad'}">${p.verified ? 'identity verified' : 'NOT verified'}</span>
      <span class="muted">${esc(p.customer_name || t.customer_id)} · ${esc(p.phone_masked)} · ${esc(p.channel)} · ${esc(p.workflow)} · ${esc(t.created)}</span>
      ${t.status === 'open' ? `<button class="btn primary small" data-id="${esc(t.id)}" style="margin-left:auto">Accept</button>` : ''}
    </div>
    <p style="margin:8px 0 0"><b>Reason:</b> ${esc(t.summary)}</p>
    <div class="turns">${turns || '<span class="muted">No conversation captured.</span>'}</div>
  </div>`;
}

$('#queue').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-id]'); if (!b) return;
  b.disabled = true;
  try {
    await call('/api/agent/accept', { method: 'POST', body: JSON.stringify({ id: b.dataset.id, agent: $('#agent-name').value }) });
    load();
  } catch (err) {
    $('#error').textContent = `Could not accept: ${err.message}`;
    b.disabled = false;
  }
});
$('#refresh').onclick = load;
setInterval(() => { if (!document.hidden) load(); }, 10000);
load();
