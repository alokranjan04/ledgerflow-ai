/* LedgerFlow AI — single-page client (no build step).
   Text: /ws/chat events.  Voice: /ws/voice (PCM16 16 kHz up, PCM16 24 kHz down + JSON events). */
'use strict';

const $ = (s) => document.querySelector(s);
const state = { cfg: null, session: null, chat: null, voice: null, adminToken: new URLSearchParams(location.search).get('admin') || '' };

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function fmtDate(iso) {
  if (!iso) return '—';
  const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number);
  if (!y || !m) return esc(iso);
  return `${d} ${MONTHS[m - 1]} ${y}` + (String(iso).length > 10 ? ' ' + String(iso).slice(11, 16) : '');
}
function md(text) {
  const lines = esc(text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').split('\n');
  let html = '', list = false;
  for (const raw of lines) {
    const l = raw.trim();
    if (/^[-*•]\s+/.test(l)) {
      if (!list) { html += '<ul>'; list = true; }
      html += `<li>${l.replace(/^[-*•]\s+/, '')}</li>`;
    } else {
      if (list) { html += '</ul>'; list = false; }
      if (l) html += `<p>${l}</p>`;
    }
  }
  return html + (list ? '</ul>' : '');
}
async function api(path, opts = {}) {
  const headers = { 'content-type': 'application/json', ...(opts.headers || {}) };
  if (state.adminToken) headers['X-Admin-Token'] = state.adminToken;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && state.session && path !== '/api/login') {
    logout(); $('#login-error').textContent = 'Your session ended. Please sign in again.';
    throw new Error('session expired');
  }
  return r.json();
}
const wsUrl = (p) => (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + p;

// ------------------------------------------------------------------ boot / login
async function boot() {
  state.cfg = await api('/api/config');
  const list = $('#demo-list');
  list.innerHTML = state.cfg.demo_customers.map((c, i) =>
    `<button class="demo-item" data-i="${i}"><b>${esc(c.name)} · ${esc(c.phone)}</b><span>${esc(c.story)}</span></button>`).join('');
  list.onclick = (e) => {
    const b = e.target.closest('.demo-item'); if (!b) return;
    const c = state.cfg.demo_customers[b.dataset.i];
    $('#login-phone').value = c.phone; $('#login-dob').value = c.dob;
    $('#login-form').requestSubmit();
  };
  const saved = JSON.parse(sessionStorage.getItem('lf_session') || 'null');
  if (saved) { state.session = saved; enterApp(); } else { $('#login').classList.remove('hidden'); }
}

$('#login-form').onsubmit = async (e) => {
  e.preventDefault();
  $('#login-error').textContent = '';
  const res = await api('/api/login', { method: 'POST', body: JSON.stringify({
    phone: $('#login-phone').value, dob: $('#login-dob').value, workflow: state.cfg.default_workflow }) });
  if (!res.ok) { $('#login-error').textContent = res.error || 'Verification failed'; return; }
  // The session ID lives only in an HttpOnly cookie; the page keeps display state.
  state.session = { name: res.customer_name, workflow: res.workflow, language: res.language || 'auto' };
  sessionStorage.setItem('lf_session', JSON.stringify(state.session));
  enterApp();
};

function logout() {
  stopVoice();
  if (state.session) fetch('/api/logout', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
  sessionStorage.removeItem('lf_session');
  state.session = null;
  if (state.chat) { state.chat.onclose = null; state.chat.close(); }
  $('#app').classList.add('hidden'); $('#login').classList.remove('hidden');
  $('#messages').innerHTML = '';
}
$('#logout').onclick = logout;

function enterApp() {
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
  $('#customer-name').textContent = state.session.name;
  renderLanguagePicker(); renderWorkflowSwitch(); connectChat(); loadAccount(); loadActivity(); loadFaults(); loadMetrics();
  if (!$('#messages').children.length) {
    sys(`Signed in as ${state.session.name}. Identity verified.`);
    const first = state.session.name.split(' ')[0];
    addMsg('assistant', isHindi()
      ? `नमस्ते ${first}, मैं Aria हूँ। अपने खाते के बारे में कुछ भी पूछिए — लिखकर, या माइक दबाकर बोलकर।`
      : `Hi ${first}, I'm Aria. Ask me anything about your account — type, or tap the mic to talk. You can also write in Hindi or Hinglish.`);
  }
}

// ------------------------------------------------------------------ language
const isHindi = () => ['hi', 'hi-Latn'].includes(state.session?.language);
function renderLanguagePicker() {
  const sel = $('#lang');
  sel.innerHTML = state.cfg.languages.map((l) => `<option value="${esc(l.code)}" ${l.code === (state.session.language || 'auto') ? 'selected' : ''}>${esc(l.name)}</option>`).join('');
  sel.onchange = async () => {
    const r = await api('/api/language', { method: 'POST', body: JSON.stringify({ language: sel.value }) });
    if (!r.ok) return;
    state.session.language = r.language; sessionStorage.setItem('lf_session', JSON.stringify(state.session));
    renderWorkflowSwitch();
    sys(r.language === 'auto' ? 'Language: automatic (follows how you write).' : `Language set to ${sel.options[sel.selectedIndex].text}. Voice uses it too.`);
  };
}

// ------------------------------------------------------------------ workflows
function renderWorkflowSwitch() {
  const nav = $('#workflow-switch');
  nav.innerHTML = state.cfg.workflows.map((w) =>
    `<button data-id="${w.id}" aria-pressed="${w.id === state.session.workflow}" title="${esc(w.description)}">${esc(w.name)}</button>`).join('');
  nav.onclick = async (e) => {
    const b = e.target.closest('button'); if (!b || b.dataset.id === state.session.workflow) return;
    stopVoice();
    const r = await api('/api/workflow', { method: 'POST', body: JSON.stringify({ workflow: b.dataset.id }) });
    if (r.ok) {
      state.session.workflow = r.workflow; sessionStorage.setItem('lf_session', JSON.stringify(state.session));
      renderWorkflowSwitch(); loadAccount();
      sys(`Switched to ${b.textContent}. Same assistant platform, different workflow and tools.`);
    }
  };
  const wf = state.cfg.workflows.find((w) => w.id === state.session.workflow);
  const sugg = isHindi() && wf?.suggestions_hi?.length ? wf.suggestions_hi : (wf?.suggestions || []);
  $('#suggestions').classList.remove('hidden');
  $('#suggestions').innerHTML = sugg.map((s) => `<button>${esc(s)}</button>`).join('');
  $('#suggestions').onclick = (e) => { const b = e.target.closest('button'); if (b) send(b.textContent); };
}

// ------------------------------------------------------------------ account / activity
async function loadAccount() {
  const a = await api('/api/account');
  const wf = state.cfg.workflows.find((w) => w.id === state.session.workflow);
  const panels = wf?.panels || [];
  let html = '';
  if (a.loan && panels.includes('loan')) {
    const l = a.loan;
    html += `<div class="panel"><div class="panel-title">Personal loan · ${esc(l.loan_id)}</div>
      <div class="hero-label">Outstanding principal</div><div class="hero-num">${esc(l.outstanding_principal)}</div>
      <dl class="kv" style="margin-top:10px">
        <dt>Next EMI</dt><dd>${esc(l.next_due_amount)}</dd><dt>Due on</dt><dd>${fmtDate(l.next_due_date)}</dd>
        <dt>Interest rate</dt><dd>${esc(l.current_interest_rate_pct)}% <span class="pill neutral">${esc(l.rate_type)}</span></dd>
        <dt>EMIs paid</dt><dd>${l.emis_paid} of ${l.tenure_months}</dd><dt>Sanctioned</dt><dd>${esc(l.sanctioned_amount)}</dd>
      </dl></div>`;
  }
  if (a.card && panels.includes('card')) {
    const c = a.card;
    html += `<div class="panel"><div class="panel-title">${esc(c.card)}</div>
      <div class="hero-label">Statement balance</div><div class="hero-num">${esc(c.statement_balance)}</div>
      <dl class="kv" style="margin-top:10px">
        <dt>Minimum due</dt><dd>${esc(c.minimum_due)}</dd><dt>Due on</dt><dd>${fmtDate(c.payment_due_date)}</dd>
        <dt>Available limit</dt><dd>${esc(c.available_limit)}</dd><dt>Credit limit</dt><dd>${esc(c.credit_limit)}</dd>
      </dl></div>`;
  }
  if (!html) html = `<div class="panel muted">No ${wf?.name.toLowerCase()} product on this profile.</div>`;
  $('#account-panel').innerHTML = html;
}

async function loadActivity() {
  const a = await api('/api/activity');
  $('#activity').innerHTML = a.requests.length ? a.requests.map((r) =>
    `<div class="req"><span class="id">${esc(r.id)}</span> <span class="pill ${r.status === 'open' ? 'warn' : 'good'}">${esc(r.status)}</span>
     <div>${esc(r.type.replace(/_/g, ' '))}</div><div class="muted">${fmtDate(r.created)}</div></div>`).join('')
    : '<div class="muted">No requests yet.</div>';
}
$('#refresh-activity').onclick = loadActivity;

// ------------------------------------------------------------------ messages
function scroll() { const m = $('#messages'); m.scrollTop = m.scrollHeight; }
function addMsg(role, text, mode) {
  const d = document.createElement('div');
  d.className = `msg ${role}`;
  d.innerHTML = (mode === 'voice' ? '<span class="mode">🎙 voice</span>' : '') + (role === 'assistant' ? md(text) : `<p>${esc(text)}</p>`);
  $('#messages').appendChild(d); scroll(); return d;
}
function sys(text, cls = '') { const d = document.createElement('div'); d.className = `sys ${cls}`; d.textContent = text; $('#messages').appendChild(d); scroll(); }
const TOOL_LABELS = {
  get_loan_summary: 'Loan Management', explain_emi_change: 'Loan Management + Policy', get_payments: 'Payments',
  get_last_payment: 'Payments', simulate_prepayment: 'Prepayment calculator + Policy', check_foreclosure: 'Foreclosure rules + quote',
  get_servicing_options: 'Loans + Payments + Policy', search_policy: 'Policy Knowledge', list_service_requests: 'Service Operations',
  propose_payment_dispute: 'Preparing dispute', propose_foreclosure_statement: 'Preparing statement request',
  propose_prepayment_request: 'Preparing prepayment request', propose_service_request: 'Preparing service request',
  confirm_action: 'Submitting to Service Operations', cancel_action: 'Cancelling', verify_identity: 'Verifying identity',
  verify_otp: 'Checking your code', request_human_agent: 'Connecting you to an agent',
  get_card_summary: 'Cards', get_card_transactions: 'Card transactions', propose_card_dispute: 'Preparing card dispute',
};
const chips = {};
function toolStarted(name) {
  const d = document.createElement('div');
  d.className = 'toolchip'; d.innerHTML = `<span class="spinner"></span>${esc(TOOL_LABELS[name] || name)}…`;
  $('#messages').appendChild(d); (chips[name] = chips[name] || []).push(d); scroll();
}
function toolFinished(ev) {
  const d = (chips[ev.name] || []).shift();
  if (d) { d.className = `toolchip done ${ev.ok ? '' : 'fail'}`; d.innerHTML = `${ev.ok ? '✓' : '⚠'} ${esc(TOOL_LABELS[ev.name] || ev.name)}${ev.ok ? '' : ' — unavailable'}`; }
  if (ev.card) renderCard(ev.card);
  if (ev.name === 'confirm_action' || ev.name === 'cancel_action') loadActivity();
}

// ------------------------------------------------------------------ agent activity (how the answer was made)
const STEP_ICON = { plan: '◆', tool: '✓', check: '✓', done: '●' };
function stepHtml(st) {
  const icon = st.ok === false ? '⚠' : STEP_ICON[st.kind] || '•';
  const meta = [st.systems, st.ms != null ? `${st.ms} ms` : ''].filter(Boolean).join(' · ');
  const items = st.items?.length ? `<ul>${st.items.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
  return `<li class="step ${esc(st.kind)} ${st.ok === false ? 'warn' : ''}"><span class="step-icon" aria-hidden="true">${icon}</span>
    <div><b>${esc(st.text)}</b>${meta ? ` <span class="muted">${esc(meta)}</span>` : ''}
    ${st.result ? `<div class="step-result">${esc(st.result)}</div>` : ''}${items}</div></li>`;
}
function addSteps(steps) {
  const tools = steps.filter((s) => s.kind === 'tool').length;
  const d = document.createElement('details');
  d.className = 'agent-steps';
  d.innerHTML = `<summary>How I got this answer · ${tools ? `${tools} system check${tools > 1 ? 's' : ''}, ` : ''}${steps.length} steps</summary><ol class="steps">${steps.map(stepHtml).join('')}</ol>`;
  $('#messages').appendChild(d); scroll();
}

// ------------------------------------------------------------------ cards
function clause(id, text) { return id ? `<div class="clause"><b>${esc(id)}</b> ${esc(text || '')}</div>` : ''; }
function renderCard(card) {
  const d = card.data || {}; let h = '';
  switch (card.type) {
    case 'loan_summary':
      h = `<h4>Loan ${esc(d.loan_id)}</h4><dl class="kv"><dt>Outstanding principal</dt><dd>${esc(d.outstanding_principal)}</dd>
        <dt>Current EMI</dt><dd>${esc(d.current_emi)}</dd><dt>Next due</dt><dd>${esc(d.next_due_amount)} on ${fmtDate(d.next_due_date)}</dd>
        <dt>Rate</dt><dd>${esc(d.current_interest_rate_pct)}% ${esc(d.rate_type)}</dd><dt>EMIs left</dt><dd>${d.emis_remaining}</dd></dl>`; break;
    case 'emi_change':
      if (!d.emi_changed) { h = `<h4>EMI history</h4><p>${esc(d.explanation)}</p>`; break; }
      h = `<h4>Why your EMI ${esc(d.direction)} <span class="pill ${d.direction === 'increased' ? 'warn' : 'good'}">${esc(d.emi_difference)}</span></h4>
        <div class="cmp"><div><div class="hero-label">Before · due ${fmtDate(d.previous_emi_due_date)}</div><div class="hero-num">${esc(d.previous_emi)}</div><div class="muted">at ${esc(d.previous_rate_pct)}%</div></div>
        <div><div class="hero-label">Now · from ${fmtDate(d.new_emi_first_due_date)}</div><div class="hero-num">${esc(d.new_emi)}</div><div class="muted">at ${esc(d.new_rate_pct)}%</div></div></div>
        <p style="margin:8px 0 0">${esc(d.reset_reason)}. ${esc(d.method)}</p>
        <div class="timeline">${(d.rate_history || []).map((r) => `<span>${fmtDate(r.effective_date)} · ${esc(r.rate_pct)}%</span>`).join('')}</div>
        ${clause(d.clause, d.clause_text)}`; break;
    case 'payments':
      h = `<h4>Payments since ${fmtDate(d.from_date)} <span class="pill ${d.failed_count ? 'bad' : 'good'}">${d.failed_count} failed</span></h4>
        <table><tr><th>Due</th><th>Attempt</th><th class="num">Amount</th><th>Status</th><th>Reason / charge</th></tr>
        ${(d.payments || []).map((p) => `<tr><td>${fmtDate(p.due_date)}</td><td>${fmtDate(p.attempt_date)}</td><td class="num">${esc(p.amount)}</td>
          <td><span class="pill ${p.status === 'failed' ? 'bad' : 'good'}">${esc(p.status)}</span></td>
          <td>${p.status === 'failed' ? `${esc(p.failure_reason)}<br><span class="muted">charge ${esc(p.bounce_charge)} + GST ${esc(p.gst_on_charge)} · ${esc(p.payment_id)}</span>` : `<span class="muted">${esc(p.mode)}</span>`}</td></tr>`).join('')}
        </table>`; break;
    case 'last_payment':
      h = `<h4>Last payment <span class="pill ${d.status === 'success' ? 'good' : 'bad'}">${esc(d.status)}</span></h4>
        <dl class="kv"><dt>Amount</dt><dd>${esc(d.amount)}</dd><dt>Paid on</dt><dd>${fmtDate(d.paid_on)}</dd><dt>For EMI due</dt><dd>${fmtDate(d.due_date)}</dd><dt>Mode</dt><dd>${esc(d.mode)}</dd></dl>`; break;
    case 'prepayment':
      h = `<h4>Prepay ${esc(d.prepayment_amount)} today <span class="pill ${d.eligible ? 'good' : 'bad'}">${d.eligible ? 'eligible' : 'not eligible'}</span></h4>
        ${d.eligible ? '' : `<p class="error">${esc((d.ineligible_reasons || []).join('; '))}</p>`}
        <div class="cmp"><div><div class="hero-label">Keep EMI, reduce tenure</div><div class="big">${esc(d.reduce_tenure.interest_saved)}</div><div class="muted">interest saved · ${d.reduce_tenure.tenure_reduced_by_months} fewer EMIs (${d.reduce_tenure.emis_remaining} left)</div></div>
        <div><div class="hero-label">Keep tenure, reduce EMI</div><div class="big">${esc(d.reduce_emi.interest_saved)}</div><div class="muted">interest saved · EMI ${esc(d.reduce_emi.new_emi)} (−${esc(d.reduce_emi.emi_reduced_by)})</div></div></div>
        <p class="muted" style="margin:8px 0 0">Charge ${esc(d.prepayment_charge)} + GST ${esc(d.gst_on_charge)} · outstanding ${esc(d.outstanding_principal_now)} at ${esc(d.interest_rate_pct)}% · ${esc(d.assumption)}</p>
        ${clause(d.clause, d.clause_text)}`; break;
    case 'foreclosure':
      h = `<h4>Foreclosure <span class="pill ${d.penalty_free ? 'good' : d.eligible ? 'warn' : 'bad'}">${d.penalty_free ? 'penalty-free' : d.eligible ? `${esc(d.foreclosure_charge_pct)}% charge` : 'not eligible yet'}</span></h4>
        ${d.eligible ? '' : `<p class="error">${esc((d.ineligible_reasons || []).join('; '))}</p>`}
        <table><tr><td>Outstanding principal</td><td class="num">${esc(d.outstanding_principal)}</td></tr>
        <tr><td>Interest accrued since ${fmtDate(d.accrued_since)}</td><td class="num">${esc(d.accrued_interest)}</td></tr>
        <tr><td>Foreclosure charge (${esc(d.foreclosure_charge_pct)}%)</td><td class="num">${esc(d.foreclosure_charge)}</td></tr>
        <tr><td>GST on charge</td><td class="num">${esc(d.gst_on_charge)}</td></tr>
        ${d.eligible ? `<tr><th>Payoff as of ${fmtDate(d.quote_date)}</th><th class="num">${esc(d.total_payoff_amount)}</th></tr>` : ''}</table>
        ${d.lower_charge_from ? `<p class="muted" style="margin:6px 0 0">Charge drops to ${esc(d.lower_charge_pct)}% from ${fmtDate(d.lower_charge_from)}.</p>` : ''}
        ${clause(d.clause, d.clause_text)}`; break;
    case 'options':
      h = `<h4>Your options</h4><table>${(d.options || []).map((o) => `<tr><td><b>${esc(o.option.replace(/_/g, ' '))}</b></td>
        <td><span class="pill ${o.available ? 'good' : 'neutral'}">${o.available ? 'available' : 'not yet'}</span></td><td>${esc(o.detail)}<br><span class="muted">${esc(o.clause)}</span></td></tr>`).join('')}</table>
        ${(d.unavailable_systems || []).length ? `<p class="sys warn">Could not reach: ${esc(d.unavailable_systems.join(', '))}</p>` : ''}`; break;
    case 'card_summary':
      h = `<h4>${esc(d.card)}</h4><dl class="kv"><dt>Statement balance</dt><dd>${esc(d.statement_balance)}</dd><dt>Minimum due</dt><dd>${esc(d.minimum_due)}</dd>
        <dt>Due date</dt><dd>${fmtDate(d.payment_due_date)}</dd><dt>Available</dt><dd>${esc(d.available_limit)}</dd></dl>${clause(d.min_due_clause, d.min_due_rule)}`; break;
    case 'card_txns':
      h = `<h4>Card ${esc(d.card)} · ${d.count} transactions</h4><table><tr><th>When</th><th>Merchant</th><th class="num">Amount</th><th></th></tr>
        ${(d.transactions || []).map((t) => `<tr><td>${fmtDate(t.date_time)}</td><td>${esc(t.merchant)}<br><span class="muted">${esc(t.transaction_id)}</span></td><td class="num">${esc(t.amount)}</td>
        <td>${t.possible_duplicate_of ? `<span class="pill warn">possible duplicate</span>` : ''}</td></tr>`).join('')}</table>`; break;
    case 'confirm':
      h = `<h4>Please confirm <span class="pill neutral">ID ${esc(d.token)}</span></h4><p style="margin:0">${esc(d.summary)}</p>
        <div class="actions"><button class="btn primary" data-say="Yes, go ahead.">Yes, go ahead</button><button class="btn" data-say="No, cancel that.">No, cancel</button></div>`; break;
    case 'action_result':
      h = d.ok ? `<h4>✓ Request submitted <span class="pill good">${esc(d.request_id)}</span></h4><p style="margin:0">${esc((d.type || '').replace(/_/g, ' '))} · status ${esc(d.status)}${d.next_step ? ' · ' + esc(d.next_step) : ''}${d.delivery ? ' · ' + esc(d.delivery) : ''}${d.replayed ? ' · (already submitted — no duplicate created)' : ''}</p>`
        : `<h4>Not submitted</h4><p class="error" style="margin:0">${esc(d.error)}</p>`; break;
    case 'otp_sent':
      h = d.demo_code
        ? `<h4>Verification code sent <span class="pill neutral">to ${esc(d.sent_to)}</span></h4>
           <div class="sms"><span class="muted">Simulated SMS (demo only)</span><br>LedgerFlow code <b class="otp">${esc(d.demo_code)}</b>. Valid 5 minutes. Never share it with anyone.</div>
           <p class="muted" style="margin:6px 0 0">Type or say the code to continue.</p>`
        : d.error ? `<h4>Verification needed</h4><p class="error" style="margin:0">${esc(d.error)}</p>`
          : `<h4>Verification code sent <span class="pill neutral">to ${esc(d.sent_to)}</span></h4><p style="margin:0">Type or say the 6-digit code from your SMS.</p>`;
      break;
    case 'otp_result':
      h = d.ok ? `<h4>✓ Verified <span class="pill good">valid ${esc(d.valid_minutes)} min</span></h4>`
        : `<h4>Code not accepted</h4><p class="error" style="margin:0">${esc(d.error)}${d.attempts_left ? ` · ${esc(d.attempts_left)} attempt(s) left` : ''}</p>`;
      break;
    case 'handoff':
      h = `<h4>Connecting you to an agent <span class="pill warn">${esc(d.request_id)}</span></h4>
        <p style="margin:0">${esc(d.next_step)} Queue position: ${esc(d.queue_position)}.</p>`;
      break;
    case 'action_cancelled':
      h = `<h4>Request cancelled</h4>`; break;
    case 'requests':
      h = `<h4>Service requests</h4>${(d || []).length ? `<table>${d.map((r) => `<tr><td>${esc(r.id)}</td><td>${esc(r.type)}</td><td>${esc(r.status)}</td><td>${fmtDate(r.created)}</td></tr>`).join('')}</table>` : '<p class="muted">None.</p>'}`; break;
    default: return;
  }
  const el = document.createElement('div');
  el.className = `card ${card.type === 'confirm' ? 'confirm' : ''}`; el.innerHTML = h;
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-say]'); if (!b) return;
    el.querySelectorAll('button').forEach((x) => (x.disabled = true));
    if (state.voice) state.voice.ws.send(JSON.stringify({ type: 'text', text: b.dataset.say }));
    else send(b.dataset.say);
  });
  $('#messages').appendChild(el); scroll();
}

// ------------------------------------------------------------------ text chat
let typingEl = null;
function connectChat(delay = 0) {
  setTimeout(() => {
    if (!state.session) return;
    const ws = new WebSocket(wsUrl('/ws/chat'));
    let opened = false;
    state.chat = ws;
    ws.onopen = () => { opened = true; };
    ws.onmessage = (m) => onEvent(JSON.parse(m.data), 'text');
    ws.onclose = async () => {
      if (!state.session) return;
      // Refused before opening: often the session ended (server restart, timeout). Ask the API,
      // which returns 401 and sends the customer back to sign-in instead of retrying forever.
      if (!opened) { try { await api('/api/account'); } catch (e) { return; } }
      if (state.session) connectChat(Math.min(5000, (delay || 500) * 2));
    };
  }, delay);
}
function send(text) {
  text = (text || '').trim(); if (!text) return;
  if (state.voice) { state.voice.ws.send(JSON.stringify({ type: 'text', text })); addMsg('user', text, 'voice'); return; }
  if (!state.chat || state.chat.readyState !== 1) { sys('Reconnecting… try again in a moment.', 'warn'); return; }
  addMsg('user', text);
  $('#suggestions').classList.add('hidden');      // give the conversation the room once it starts
  state.chat.send(JSON.stringify({ type: 'user', text }));
  $('#input').value = '';
}
$('#composer').onsubmit = (e) => { e.preventDefault(); send($('#input').value); };

function onEvent(ev, mode) {
  switch (ev.type) {
    case 'thinking':
      typingEl = document.createElement('div'); typingEl.className = 'typing steps-live';
      typingEl.innerHTML = '<div class="steps-head"><span class="spinner"></span> Aria is working on it…</div><ol class="steps"></ol>';
      $('#messages').appendChild(typingEl); scroll(); break;
    case 'agent_step':
      if (typingEl) { typingEl.querySelector('.steps').insertAdjacentHTML('beforeend', stepHtml(ev.step)); scroll(); }
      break;
    case 'tool_started': toolStarted(ev.name); if (state.voice) setVoiceState('thinking', `Checking ${TOOL_LABELS[ev.name] || ev.name}…`); break;
    case 'tool_finished': toolFinished(ev); break;
    case 'assistant':
      if (typingEl) { typingEl.remove(); typingEl = null; }
      addMsg('assistant', ev.text);
      if (ev.steps?.length) addSteps(ev.steps);
      break;
    case 'trace': renderTrace(ev); loadMetrics(); break;
    case 'error': sys(ev.text, 'warn'); break;
    default: if (mode === 'voice') onVoiceEvent(ev);
  }
}

// ------------------------------------------------------------------ voice
const live = { user: null, assistant: null };
function setVoiceState(kind, text) {
  const bar = $('#voice-bar'); bar.classList.remove('speaking', 'thinking');
  if (kind) bar.classList.add(kind); $('#voice-state').textContent = text;
}
function onVoiceEvent(ev) {
  if (ev.type === 'voice_ready') setVoiceState('', 'Listening…');
  else if (ev.type === 'transcript') {
    let el = live[ev.role];
    if (!el) { el = live[ev.role] = addMsg(ev.role, '', 'voice'); el.classList.add('interim'); }
    el.innerHTML = '<span class="mode">🎙 voice</span>' + (ev.role === 'assistant' ? md(ev.text) : `<p>${esc(ev.text)}</p>`);
    if (ev.final) { el.classList.remove('interim'); live[ev.role] = null; if (ev.role === 'assistant') live.steps = null; }
    scroll();
  } else if (ev.type === 'agent_step') {
    // voice: one "How I got this answer" block per spoken turn, filled as tools run
    if (!live.steps) { live.steps = document.createElement('details'); live.steps.className = 'agent-steps'; live.steps.open = true;
      live.steps.innerHTML = '<summary>How I got this answer (voice)</summary><ol class="steps"></ol>'; $('#messages').appendChild(live.steps); }
    live.steps.querySelector('.steps').insertAdjacentHTML('beforeend', stepHtml(ev.step)); scroll();
  } else if (ev.type === 'filler') { setVoiceState('', 'Checking your account…'); }
  else if (ev.type === 'interrupted') { stopPlayback(); setVoiceState('', 'Listening… (you interrupted)'); }
  else if (ev.type === 'grounding_warning') sys(`Grounding monitor: unverified figure(s) spoken — ${ev.figures.join(', ')}`, 'warn');
}
async function startVoice() {
  if (!state.cfg.voice_enabled) { sys('Voice needs GEMINI_API_KEY on the server.', 'warn'); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    const inCtx = new AudioContext();
    await inCtx.audioWorklet.addModule('/static/audio-capture.js');
    const outCtx = new AudioContext({ sampleRate: 24000 });
    const ws = new WebSocket(wsUrl('/ws/voice'));
    ws.binaryType = 'arraybuffer';
    const v = state.voice = { ws, stream, inCtx, outCtx, sources: [], nextTime: 0 };
    const src = inCtx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(inCtx, 'capture', { processorOptions: { targetRate: 16000 } });
    const mute = inCtx.createGain(); mute.gain.value = 0;
    src.connect(node); node.connect(mute); mute.connect(inCtx.destination);
    node.port.onmessage = (e) => { if (ws.readyState === 1) ws.send(e.data); };
    ws.onmessage = (m) => {
      if (typeof m.data === 'string') { onEvent(JSON.parse(m.data), 'voice'); return; }
      playChunk(v, m.data);
    };
    ws.onclose = () => { if (state.voice === v) stopVoice(); };
    $('#mic').classList.add('live'); $('#mic').setAttribute('aria-label', 'Stop live voice');
    $('#voice-bar').classList.remove('hidden'); setVoiceState('', 'Connecting to voice…');
    sys('Live voice started — same session, same context as your chat.');
  } catch (err) {
    sys(`Could not start the microphone: ${err.message}`, 'warn');
    stopVoice();
  }
}
function playChunk(v, buf) {
  const pcm = new Int16Array(buf);
  const f = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
  const ab = v.outCtx.createBuffer(1, f.length, 24000); ab.copyToChannel(f, 0);
  const s = v.outCtx.createBufferSource(); s.buffer = ab; s.connect(v.outCtx.destination);
  const t = Math.max(v.nextTime, v.outCtx.currentTime + 0.03);
  s.start(t); v.nextTime = t + ab.duration; v.sources.push(s);
  s.onended = () => { v.sources = v.sources.filter((x) => x !== s); if (!v.sources.length) setVoiceState('', 'Listening…'); };
  setVoiceState('speaking', 'Aria is speaking…');
}
function stopPlayback() {
  const v = state.voice; if (!v) return;
  v.sources.forEach((s) => { try { s.stop(); } catch (_) { /* already stopped */ } });
  v.sources = []; v.nextTime = 0;
}
function stopVoice() {
  const v = state.voice; if (!v) return;
  state.voice = null;
  try { v.ws.readyState === 1 && v.ws.send(JSON.stringify({ type: 'stop' })); v.ws.close(); } catch (_) { /* closing */ }
  v.stream.getTracks().forEach((t) => t.stop());
  v.inCtx.close(); v.outCtx.close();
  $('#mic').classList.remove('live'); $('#mic').setAttribute('aria-label', 'Start live voice');
  $('#voice-bar').classList.add('hidden');
  live.user = live.assistant = null;
  sys('Voice ended. You can keep typing — the conversation continues.');
  loadActivity();
}
$('#mic').onclick = () => (state.voice ? stopVoice() : startVoice());
$('#voice-stop').onclick = stopVoice;

// ------------------------------------------------------------------ ops panel
function renderTrace(ev) {
  const spans = ev.spans || [];
  if (!spans.length) return;
  const end = Math.max(...spans.map((s) => s.offset_ms + s.ms), ev.total_ms || 0, 1);
  $('#trace').classList.remove('muted');
  $('#trace').innerHTML = `<div class="trace-total">${ev.mode === 'text' ? 'Text turn' : 'Voice turn'}${ev.total_ms ? ` · ${Math.round(ev.total_ms)} ms total` : ''}</div>` +
    spans.map((s) => `<div class="trace-row" title="${esc(s.detail || '')}"><span class="name">${esc(s.name)}</span>
      <span class="bar-wrap"><span class="bar ${s.ok ? '' : 'fail'} ${s.name.startsWith('llm') ? 'llm' : ''}" style="left:${(s.offset_ms / end) * 100}%;width:${Math.max(0.5, (s.ms / end) * 100)}%"></span></span>
      <span class="ms">${Math.round(s.ms)}</span></div>`).join('');
}
async function loadMetrics() {
  const m = await api('/api/metrics');
  if (m.error) { $('#metrics').innerHTML = '<div class="muted">Metrics need the admin token: open the app with ?admin=&lt;token&gt;.</div>'; return; }
  const rows = Object.entries(m.latency_ms).filter(([k]) => /^(turn|llm|voice|connector|tool)\./.test(k))
    .sort(([a], [b]) => a.localeCompare(b));
  $('#metrics').innerHTML = (rows.length ? rows.map(([k, v]) => `<div class="metric-row"><span title="${esc(k)}">${esc(k)} <span class="muted">n=${v.n}</span></span><span>${v.p50}</span><span>${v.p95}</span></div>`).join('')
    : '<div class="muted">No samples yet.</div>') +
    `<div class="metric-row muted" style="margin-top:6px"><span>grounding checks / fallbacks</span><span>${m.counters['grounding.checked'] || 0}</span><span>${m.counters['grounding.fallback'] || 0}</span></div>`;
}
$('#refresh-metrics').onclick = loadMetrics;

const FAULT_MODES = {
  normal: null,
  slow: (p) => ({ p50: p.p50 * 6, p95: p.p95 * 4, error_rate: 0, down: false }),
  flaky: () => ({ error_rate: 0.35, down: false }),
  down: () => ({ down: true }),
};
const BASE = {};
async function loadFaults() {
  const f = await api('/api/faults');
  for (const [k, p] of Object.entries(f.profiles)) if (!BASE[k]) BASE[k] = { ...p };
  $('#faults').innerHTML = Object.entries(f.profiles).map(([sys, p]) => {
    const mode = p.down ? 'down' : p.error_rate >= 0.3 ? 'flaky' : p.p50 > BASE[sys].p50 ? 'slow' : 'normal';
    const br = f.breakers[sys];
    return `<div class="fault"><b>${esc(sys)}</b><span class="muted">p50 ${p.p50}ms · p95 ${p.p95}ms · err ${Math.round(p.error_rate * 100)}%</span>
      <select data-sys="${esc(sys)}" aria-label="${esc(sys)} mode">${Object.keys(FAULT_MODES).map((m) => `<option ${m === mode ? 'selected' : ''}>${m}</option>`).join('')}</select>
      <span></span><span class="state">breaker <span class="pill ${br === 'closed' ? 'good' : br === 'open' ? 'bad' : 'warn'}">${esc(br)}</span></span></div>`;
  }).join('');
}
$('#faults').onchange = async (e) => {
  const sel = e.target.closest('select'); if (!sel) return;
  const sys = sel.dataset.sys, fn = FAULT_MODES[sel.value];
  const body = { [sys]: fn ? { ...BASE[sys], ...fn(BASE[sys]) } : { ...BASE[sys] } };
  const r = await api('/api/faults', { method: 'POST', body: JSON.stringify(body) });
  if (r.error) { sys('Fault injection needs the admin token: open the app with ?admin=<token>.', 'warn'); loadFaults(); return; }
  loadFaults();
  sysMsg(`${sys} set to ${sel.value}`);
};
function sysMsg(t) { sys(`⚙ ${t}`); }
$('#reset-faults').onclick = async () => { await api('/api/faults', { method: 'POST', body: JSON.stringify({ reset: true }) }); loadFaults(); };
$('#ops-toggle').onclick = () => {
  const on = $('#ops-toggle').getAttribute('aria-pressed') !== 'true';
  $('#ops-toggle').setAttribute('aria-pressed', on); document.querySelector('.layout').classList.toggle('no-ops', !on);
};
setInterval(() => { if (state.session && !document.hidden) loadFaults(); }, 5000);

boot();
