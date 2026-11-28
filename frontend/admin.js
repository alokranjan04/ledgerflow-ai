// LedgerFlow AI — admin panel: products & policies, customers, change log.
// Every value is escaped before it reaches the page; no inline handlers (CSP: script-src 'self').
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const st = { token: sessionStorage.getItem('lf_admin_token') || '', user: sessionStorage.getItem('lf_admin_user') || '',
  schema: null, products: [], product: null, isNew: false };

// A token in the URL (?admin=...) is taken once and removed, so it does not stay in history.
const urlToken = new URLSearchParams(location.search).get('admin');
if (urlToken) { st.token = urlToken; sessionStorage.setItem('lf_admin_token', urlToken); history.replaceState(null, '', '/admin'); }

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { 'content-type': 'application/json', 'X-Admin-Token': st.token,
    'X-Admin-User': st.user, ...(opts.headers || {}) } });
  if (r.status === 403) { showGate('The admin token was not accepted.'); throw new Error('forbidden'); }
  let body = {};
  try { body = await r.json(); } catch (e) { body = {}; }
  return { ok: r.ok, status: r.status, body };
}
const post = (path, data) => api(path, { method: 'POST', body: JSON.stringify(data) });

function toast(text, bad = false) {
  const t = $('#toast'); t.textContent = text; t.className = `toast ${bad ? 'bad' : ''}`;
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), 4000);
}
const errorList = (errors) => `<ul class="errors" role="alert">${(errors || []).map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`;

// ------------------------------------------------------------------ gate & tabs
function showGate(msg = '') {
  $('#main').classList.add('hidden'); $('#gate').classList.remove('hidden');
  $('#gate-error').textContent = msg; $('#gate-user').value = st.user;
}
$('#gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  st.token = $('#gate-token').value; st.user = $('#gate-user').value.trim();
  sessionStorage.setItem('lf_admin_token', st.token); sessionStorage.setItem('lf_admin_user', st.user);
  await start();
});
$('#lock').addEventListener('click', () => { sessionStorage.removeItem('lf_admin_token'); st.token = ''; showGate(); });
$$('[data-tab]').forEach((b) => b.addEventListener('click', () => {
  $$('[data-tab]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  ['products', 'customers', 'log'].forEach((t) => $(`#tab-${t}`).classList.toggle('hidden', t !== b.dataset.tab));
  if (b.dataset.tab === 'customers') loadCustomers();
  if (b.dataset.tab === 'log') loadLog();
}));

async function start() {
  try {
    const s = await api('/api/admin/schema');
    if (!s.ok) throw new Error('schema');
    st.schema = s.body;
  } catch (e) { return; }
  if (!st.user) { showGate('Please add your name for the change log.'); return; }
  $('#gate').classList.add('hidden'); $('#main').classList.remove('hidden');
  $('#who').textContent = `Signed in as ${st.user}`;
  await loadProducts();
}

// ------------------------------------------------------------------ products
async function loadProducts(select) {
  const r = await api('/api/admin/products');
  st.products = r.body.products || [];
  $('#p-table tbody').innerHTML = st.products.map((p) => `<tr data-code="${esc(p.code)}" tabindex="0">
    <td><b>${esc(p.code)}</b></td><td>${esc(p.name)}<div class="muted">${esc(p.kind)}${p.kind === 'loan' ? ' · ' + esc(p.rate_type) : ''}</div></td>
    <td>${esc(p.version)}<div class="muted">from ${esc(p.effective)}</div></td>
    <td><span class="pill ${p.source === 'file' ? 'neutral' : p.source === 'admin' ? 'good' : 'warn'}">${esc(p.source)}</span></td>
    <td>${p.holders}</td></tr>`).join('');
  $$('#p-table tbody tr').forEach((tr) => {
    const open = () => openProduct(tr.dataset.code);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
  });
  if (select) openProduct(select);
}

async function openProduct(code) {
  const r = await api(`/api/admin/products/${encodeURIComponent(code)}`);
  if (!r.ok) return toast('Product not found', true);
  st.product = r.body; st.isNew = false;
  renderProduct(structuredClone(r.body.spec), r.body);
}

$('#p-new').addEventListener('click', () => {
  const base = st.products.find((p) => p.kind === 'loan');
  newFrom(base ? base.code : null);
});
async function newFrom(code) {
  let spec = { name: '', kind: 'loan', rate_type: 'fixed', policy: { version: '1.0', effective: new Date().toISOString().slice(0, 10), rules: {}, clauses: {} } };
  if (code) spec = structuredClone((await api(`/api/admin/products/${encodeURIComponent(code)}`)).body.spec);
  spec.name = code ? `${spec.name} (copy)` : '';
  st.product = { code: '', source: 'new', holders: 0 }; st.isNew = true;
  renderProduct(spec, st.product);
}

function ruleFields(kind, rateType, ruleName, rule) {
  const sch = st.schema;
  if (ruleName === 'foreclosure') return sch.foreclosure['lock_in_emis' in (rule || {}) ? 'lock_in' : 'open'];
  return sch.rules[kind][ruleName];
}

function rulesFor(kind, rateType) {
  const names = Object.keys(st.schema.rules[kind]);
  if (kind === 'loan') names.unshift('foreclosure');
  return names.filter((n) => n !== 'rate_reset' || rateType === 'floating');
}

function fieldInput(rule, f, type, val, clauses) {
  const id = `f-${rule}-${f}`;
  const label = f.replace(/_/g, ' ');
  if (type === 'bool') return `<label class="check"><input type="checkbox" id="${id}" data-rule="${rule}" data-field="${f}" data-type="bool" ${val ? 'checked' : ''}> ${esc(label)}</label>`;
  if (type === 'clause') return `<label>${esc(label)}<select id="${id}" data-rule="${rule}" data-field="${f}" data-type="clause">${
    Object.keys(clauses).map((c) => `<option ${c === val ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></label>`;
  const hint = { money: '₹', pct: '%', int: 'number', days: 'e.g. 5, 10, 15', text: '' }[type];
  const v = type === 'days' ? (val || []).join(', ') : (val ?? '');
  const attrs = type === 'int' ? 'type="number" step="1" min="0"' : type === 'money' ? 'type="number" step="0.01" min="0"'
    : type === 'pct' ? 'type="number" step="0.01" min="0" max="100"' : 'type="text"';
  return `<label>${esc(label)} <span class="muted">${esc(hint)}</span><input id="${id}" ${attrs} data-rule="${rule}" data-field="${f}" data-type="${type}" value="${esc(v)}"></label>`;
}

function renderProduct(spec, meta) {
  const kind = spec.kind, rt = spec.rate_type;
  const clauses = spec.policy.clauses || {};
  const req = new Set([...(st.schema.required[kind] || []), ...(rt === 'floating' ? st.schema.floating_requires : [])]);
  const held = meta.holders > 0;
  const rulesHtml = rulesFor(kind, rt).map((name) => {
    const rule = spec.policy.rules[name] || {};
    const fields = ruleFields(kind, rt, name, rule);
    const shape = name === 'foreclosure' ? `<label>Shape<select id="fc-shape">
        <option value="open" ${'lock_in_emis' in rule ? '' : 'selected'}>Allowed after N EMIs, one charge</option>
        <option value="lock_in" ${'lock_in_emis' in rule ? 'selected' : ''}>Lock-in, then a charge by loan age</option></select></label>` : '';
    return `<fieldset class="rule"><legend>${esc(name.replace(/_/g, ' '))}${req.has(name) ? '' : ' <span class="muted">(optional)</span>'}</legend>
      ${shape}<div class="form-grid">${Object.entries(fields).map(([f, t]) => fieldInput(name, f, t, rule[f], clauses)).join('')}</div></fieldset>`;
  }).join('');
  const clauseRows = Object.entries(clauses).map(([id, text]) => clauseRow(id, text)).join('');
  $('#p-editor').innerHTML = `
    <div class="admin-bar"><h2>${st.isNew ? 'New product' : esc(meta.code)}</h2>
      ${meta.source ? `<span class="pill neutral">${esc(meta.source)}</span>` : ''}
      ${held ? `<span class="muted">held by ${meta.holders} account(s)</span>` : ''}</div>
    <form id="p-form" autocomplete="off">
      <div class="form-grid">
        <label>Code<input id="p-code" value="${esc(meta.code || '')}" ${st.isNew ? '' : 'readonly'} required placeholder="PL-FLOAT-PLUS" pattern="[A-Z0-9]+(-[A-Z0-9]+)*"></label>
        <label>Name<input id="p-name" value="${esc(spec.name)}" required></label>
        <label>Kind<select id="p-kind" ${st.isNew ? '' : 'disabled'}><option ${kind === 'loan' ? 'selected' : ''}>loan</option><option ${kind === 'card' ? 'selected' : ''}>card</option></select></label>
        ${kind === 'loan' ? `<label>Rate type<select id="p-rt" ${held ? 'disabled' : ''}><option ${rt === 'floating' ? 'selected' : ''}>floating</option><option ${rt === 'fixed' ? 'selected' : ''}>fixed</option></select></label>`
          : `<label>Short name<input id="p-short" value="${esc(spec.short_name || '')}" placeholder="Gold card"></label>`}
        <label>Policy version<input id="p-version" value="${esc(spec.policy.version)}" required></label>
        <label>Effective from<input id="p-effective" type="date" value="${esc(spec.policy.effective)}" required></label>
      </div>
      <h3>Rules <span class="muted">— the numbers the calculations use</span></h3>
      ${rulesHtml}
      <h3>Clauses <span class="muted">— the text the assistant quotes and cites</span></h3>
      <div id="p-clauses">${clauseRows}</div>
      <button type="button" id="p-add-clause" class="btn small">Add clause</button>
      <div id="p-errors"></div>
      <div class="actions">
        <button class="btn primary" type="submit">${st.isNew ? 'Create product' : 'Save changes'}</button>
        ${!st.isNew ? '<button type="button" id="p-copy" class="btn">Copy as new product</button>' : ''}
        ${meta.source === 'file, edited' ? '<button type="button" id="p-revert" class="btn">Revert to file version</button>' : ''}
        ${meta.source === 'admin' && !held ? '<button type="button" id="p-delete" class="btn danger">Delete</button>' : ''}
      </div>
      ${!st.isNew ? '<p class="muted">Changing rules or clauses needs a new policy version. The assistant uses saved terms at once.</p>' : ''}
    </form>`;
  const rerender = () => { const s = collectProduct(); renderProduct(s, meta); };
  $('#p-kind')?.addEventListener('change', () => {
    const s = collectProduct(); s.kind = $('#p-kind').value; s.rate_type = s.kind === 'loan' ? 'fixed' : 'n/a';
    s.policy.rules = {}; renderProduct(s, meta);
  });
  $('#p-rt')?.addEventListener('change', rerender);
  $('#fc-shape')?.addEventListener('change', () => {
    const s = collectProduct();
    s.policy.rules.foreclosure = $('#fc-shape').value === 'lock_in' ? { lock_in_emis: 12 } : { min_emis_paid: 1 };
    renderProduct(s, meta);
  });
  $('#p-add-clause').addEventListener('click', () => {
    $('#p-clauses').insertAdjacentHTML('beforeend', clauseRow('', '')); wireClauses();
  });
  wireClauses();
  $('#p-copy')?.addEventListener('click', () => newFrom(meta.code));
  $('#p-revert')?.addEventListener('click', async () => {
    const r = await post(`/api/admin/products/${encodeURIComponent(meta.code)}/revert`, {});
    if (!r.ok) { $('#p-errors').innerHTML = errorList(r.body.errors); return; }
    toast(`${meta.code} reverted to the file version`); loadProducts(meta.code);
  });
  $('#p-delete')?.addEventListener('click', async () => {
    if (!confirm(`Delete product ${meta.code}? This cannot be undone.`)) return;
    const r = await post(`/api/admin/products/${encodeURIComponent(meta.code)}/delete`, {});
    if (!r.ok) { $('#p-errors').innerHTML = errorList(r.body.errors); return; }
    toast(`${meta.code} deleted`); $('#p-editor').innerHTML = '<p class="muted">Product deleted.</p>'; loadProducts();
  });
  $('#p-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('#p-code').value.trim().toUpperCase();
    if (!code) return;
    const r = await post(`/api/admin/products/${encodeURIComponent(code)}`, { spec: collectProduct() });
    if (!r.ok) { $('#p-errors').innerHTML = errorList(r.body.errors); $('#p-errors').scrollIntoView({ block: 'center' }); return; }
    toast(r.body.unchanged ? 'No changes to save' : `${code} saved · ${(r.body.changes || []).length} change(s)`);
    loadProducts(code);
  });
}

function clauseRow(id, text) {
  return `<div class="clause-row"><input class="c-id" value="${esc(id)}" placeholder="PL-XX-1.1" aria-label="Clause ID">
    <textarea class="c-text" rows="2" aria-label="Clause text">${esc(text)}</textarea>
    <button type="button" class="btn small danger c-del" aria-label="Remove clause">Remove</button></div>`;
}
function wireClauses() {
  $$('.c-del').forEach((b) => { b.onclick = () => { b.closest('.clause-row').remove(); refreshClauseSelects(); }; });
  $$('.c-id').forEach((i) => { i.onchange = refreshClauseSelects; });
}
function refreshClauseSelects() {
  const ids = $$('.c-id').map((i) => i.value.trim()).filter(Boolean);
  $$('select[data-type="clause"]').forEach((s) => {
    const cur = s.value;
    s.innerHTML = ids.map((c) => `<option ${c === cur ? 'selected' : ''}>${esc(c)}</option>`).join('');
  });
}

function collectProduct() {
  const kind = $('#p-kind').value;
  const spec = { name: $('#p-name').value.trim(), kind, policy: { version: $('#p-version').value.trim(),
    effective: $('#p-effective').value, rules: {}, clauses: {} } };
  if (kind === 'loan') spec.rate_type = $('#p-rt') ? $('#p-rt').value : 'fixed';
  else spec.short_name = $('#p-short') ? $('#p-short').value.trim() : '';
  $$('[data-rule]').forEach((el) => {
    const rule = (spec.policy.rules[el.dataset.rule] ||= {});
    const t = el.dataset.type, f = el.dataset.field;
    if (t === 'bool') rule[f] = el.checked;
    else if (t === 'int') rule[f] = el.value === '' ? '' : parseInt(el.value, 10);
    else if (t === 'days') rule[f] = el.value.split(/[,\s]+/).filter(Boolean).map((x) => parseInt(x, 10));
    else rule[f] = el.value.trim();
  });
  $$('.clause-row').forEach((row) => {
    const id = $('.c-id', row).value.trim();
    if (id) spec.policy.clauses[id] = $('.c-text', row).value.trim();
  });
  return spec;
}

// ------------------------------------------------------------------ customers
let searchTimer;
$('#c-q').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadCustomers, 250); });
$('#c-scope').addEventListener('change', loadCustomers);

async function loadCustomers(select) {
  const q = new URLSearchParams({ q: $('#c-q').value.trim(), scope: $('#c-scope').value, limit: '100' });
  const r = await api(`/api/admin/customers?${q}`);
  const rows = r.body.customers || [];
  $('#c-table tbody').innerHTML = rows.map((c) => `<tr data-id="${esc(c.id)}" tabindex="0">
    <td><b>${esc(c.id)}</b></td><td>${esc(c.name)}${c.demo ? ' <span class="pill good">sign-in list</span>' : ''}<div class="muted">${esc(c.city || '')}</div></td>
    <td>${esc(c.phone)}</td>
    <td>${[c.loan_product, c.card_product].filter(Boolean).map(esc).join(' · ') || '—'}</td>
    <td><span class="pill ${c.status === 'active' ? 'good' : 'bad'}">${esc(c.status)}</span></td>
    <td>${esc(c.source)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No customers match.</td></tr>';
  $$('#c-table tbody tr[data-id]').forEach((tr) => {
    const open = () => openCustomer(tr.dataset.id);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
  });
  if (typeof select === 'string') openCustomer(select);
}

const SEGMENT_OPTS = (cur) => (st.schema.segments || []).map((s) => `<option ${s === cur ? 'selected' : ''}>${esc(s)}</option>`).join('');
const profileFields = (c) => `
  <label>Full name<input id="cf-name" value="${esc(c.name)}" required></label>
  <label>Mobile<input id="cf-phone" value="${esc(c.phone)}" inputmode="numeric" required></label>
  <label>Date of birth<input id="cf-dob" type="date" value="${esc(c.dob)}" required></label>
  <label>City<input id="cf-city" value="${esc(c.city || '')}"></label>
  <label>Employment<select id="cf-segment">${SEGMENT_OPTS(c.segment || 'salaried')}</select></label>
  <label>Email<input id="cf-email" type="email" value="${esc(c.email || '')}"></label>
  <label class="check wide"><input id="cf-demo" type="checkbox" ${c.demo ? 'checked' : ''}> Show on the sign-in screen (demo list — shows mobile and date of birth)</label>
  <label class="wide">One-line story for the sign-in list<input id="cf-story" value="${esc(c.story || '')}" maxlength="160"></label>`;
const collectProfile = () => ({ name: $('#cf-name').value.trim(), phone: $('#cf-phone').value.replace(/\D/g, ''),
  dob: $('#cf-dob').value, city: $('#cf-city').value.trim(), segment: $('#cf-segment').value,
  email: $('#cf-email').value.trim(), demo: $('#cf-demo').checked, story: $('#cf-story').value.trim() });

async function openCustomer(id) {
  const r = await api(`/api/admin/customers/${encodeURIComponent(id)}`);
  if (!r.ok) return toast('Customer not found', true);
  const c = r.body;
  const loan = (c.loans || []).map((l) => `<div class="kv"><span>Loan ${esc(l.id)}</span><b>${esc(l.product_code)} · ₹${Number(l.principal).toLocaleString('en-IN')} over ${esc(l.tenure)} months</b></div>`).join('');
  const card = (c.cards || []).map((k) => `<div class="kv"><span>Card ${esc(k.id)}</span><b>${esc(k.product)} ending ${esc(k.last4)} · limit ₹${Number(k.credit_limit).toLocaleString('en-IN')}</b></div>`).join('');
  $('#c-detail').innerHTML = `
    <div class="admin-bar"><h2>${esc(c.name)} <span class="muted">${esc(c.id)}</span></h2>
      <span class="pill ${c.status === 'active' ? 'good' : 'bad'}">${esc(c.status)}</span><span class="pill neutral">${esc(c.source)}</span></div>
    ${loan}${card}<div class="kv"><span>Open service requests</span><b>${c.open_requests}</b></div>
    <form id="cf" autocomplete="off"><h3>Profile</h3><div class="form-grid">${profileFields(c)}</div>
      <div id="cf-errors"></div>
      <div class="actions"><button class="btn primary" type="submit">Save profile</button></div></form>
    <h3>Sign-in access</h3>
    <div class="form-grid">
      <label class="wide">Reason (recorded in the change log)<input id="cs-reason" maxlength="200" placeholder="${c.status === 'active' ? 'e.g. reported lost phone' : 'e.g. identity re-verified at branch'}"></label>
    </div>
    <div class="actions"><button id="cs-toggle" class="btn ${c.status === 'active' ? 'danger' : 'primary'}">${c.status === 'active' ? 'Disable sign-in' : 'Enable sign-in'}</button></div>
    <p class="muted">A disabled customer cannot sign in by app or phone. Their accounts and requests are kept.</p>`;
  $('#cf').addEventListener('submit', async (e) => {
    e.preventDefault();
    const res = await post(`/api/admin/customers/${encodeURIComponent(c.id)}`, { profile: collectProfile() });
    if (!res.ok) { $('#cf-errors').innerHTML = errorList(res.body.errors); return; }
    toast(res.body.unchanged ? 'No changes to save' : 'Profile saved'); loadCustomers(c.id);
  });
  $('#cs-toggle').addEventListener('click', async () => {
    const next = c.status === 'active' ? 'disabled' : 'active';
    const res = await post(`/api/admin/customers/${encodeURIComponent(c.id)}/status`, { status: next, reason: $('#cs-reason').value.trim() });
    if (!res.ok) return toast((res.body.errors || ['Failed']).join('; '), true);
    toast(next === 'disabled' ? `${c.name} can no longer sign in` : `${c.name} can sign in again`); loadCustomers(c.id);
  });
}

$('#c-new').addEventListener('click', () => {
  const loans = st.products.filter((p) => p.kind === 'loan'), cards = st.products.filter((p) => p.kind === 'card');
  const opts = (list) => list.map((p) => `<option value="${esc(p.code)}">${esc(p.code)} — ${esc(p.name)}</option>`).join('');
  $('#c-detail').innerHTML = `
    <div class="admin-bar"><h2>New customer</h2></div>
    <form id="cn" autocomplete="off"><div class="form-grid">${profileFields({})}</div>
      <fieldset class="rule"><legend><label class="check"><input id="cn-has-loan" type="checkbox" checked> Personal loan</label></legend>
        <div class="form-grid">
          <label>Product<select id="cn-loan-product">${opts(loans)}</select></label>
          <label>Amount (₹)<input id="cn-principal" type="number" min="10000" step="1000" value="500000"></label>
          <label>Tenure (months)<input id="cn-tenure" type="number" min="3" value="36"></label>
          <label>EMIs already paid<input id="cn-paid" type="number" min="1" value="6"></label>
          <label>Interest rate (% a year)<input id="cn-rate" type="number" min="1" step="0.01" value="11.00"></label>
        </div></fieldset>
      <fieldset class="rule"><legend><label class="check"><input id="cn-has-card" type="checkbox"> Credit card</label></legend>
        <div class="form-grid">
          <label>Product<select id="cn-card-product">${opts(cards)}</select></label>
          <label>Credit limit (₹)<input id="cn-limit" type="number" min="10000" step="10000" value="100000"></label>
        </div></fieldset>
      <div id="cn-errors"></div>
      <div class="actions"><button class="btn primary" type="submit">Create customer</button></div>
      <p class="muted">IDs are assigned automatically. Loan schedule, payments and card transactions are generated (synthetic).</p>
    </form>`;
  $('#cn').addEventListener('submit', async (e) => {
    e.preventDefault();
    const p = collectProfile();
    if (!p.email) delete p.email;
    if ($('#cn-has-loan').checked) p.loan = { product: $('#cn-loan-product').value, principal: Number($('#cn-principal').value),
      tenure_months: Number($('#cn-tenure').value), emis_paid: Number($('#cn-paid').value), rate: Number($('#cn-rate').value) };
    if ($('#cn-has-card').checked) p.card = { product: $('#cn-card-product').value, limit: Number($('#cn-limit').value) };
    const res = await post('/api/admin/customers', { persona: p });
    if (!res.ok) { $('#cn-errors').innerHTML = errorList(res.body.errors); return; }
    toast(`${res.body.customer.name} created as ${res.body.customer.id}`);
    $('#c-scope').value = 'named'; $('#c-q').value = ''; loadCustomers(res.body.customer.id);
  });
});

// ------------------------------------------------------------------ change log
$('#l-refresh').addEventListener('click', loadLog);
async function loadLog() {
  const r = await api('/api/admin/audit');
  $('#l-table tbody').innerHTML = (r.body.entries || []).map((e) => `<tr><td>${esc(e.ts)}</td><td>${esc(e.actor)}</td>
    <td>${esc(e.action)}</td><td><b>${esc(e.target)}</b></td>
    <td>${(e.changes || []).length ? `<ul class="changes">${e.changes.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : '<span class="muted">—</span>'}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">No changes yet.</td></tr>';
}

if (st.token || !location.hostname.endsWith('.run.app')) start(); else showGate();
