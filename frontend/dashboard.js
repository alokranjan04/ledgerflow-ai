/* KPI dashboard: business, AI-quality and safety KPIs from /api/metrics, against the PRD pilot targets. */
'use strict';
const $ = (s) => document.querySelector(s);
const token = new URLSearchParams(location.search).get('admin') || '';
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (x) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`);

// [label, value, target text, status: good | warn | bad | neutral]
function tiles(m) {
  const k = m.kpis || {}, c = m.counters || {}, lat = m.latency_ms || {};
  const checked = c['grounding.checked'] || 0, fallback = c['grounding.fallback'] || 0;
  const vChecked = c['grounding.voice_checked'] || 0, vFlag = c['grounding.voice_unsupported'] || 0;
  const voice = lat['voice.voice.user_to_first_audio'] || lat['voice.phone.user_to_first_audio'];
  const text = lat['turn.text.total'];
  const sentOk = k.step_up_sent ? k.step_up_verified / k.step_up_sent : null;
  const st = (ok, have = true) => (!have ? 'neutral' : ok ? 'good' : 'bad');
  return [
    ['Containment (north star)', pct(k.containment_rate), 'pilot ≥ 40%', st(k.containment_rate >= 0.4, k.containment_rate != null)],
    ['Conversations', k.sessions_engaged ?? 0, 'engaged sessions', 'neutral'],
    ['Hand-offs to agents', k.handoffs ?? 0, 'with full context', 'neutral'],
    ['Requests completed', k.actions_executed ?? 0, 'disputes, statements, prepayments', 'neutral'],
    ['Grounding fallbacks (text)', checked ? `${fallback} / ${checked}` : '—', 'unsupported figures shown: 0', st(fallback === 0, checked > 0)],
    ['Voice figure flags', vChecked ? pct(vFlag / vChecked) : '—', 'pilot < 2% of voice turns', st(vFlag / Math.max(vChecked, 1) < 0.02, vChecked > 0)],
    ['Step-up codes verified', sentOk == null ? '—' : pct(sentOk), `${k.step_up_failed || 0} wrong codes`, 'neutral'],
    ['Voice: speech → first audio', voice ? `${voice.p50} / ${voice.p95} ms` : '—', 'p50 < 1000 · p95 < 2000', st(voice && voice.p50 < 1000 && voice.p95 < 2000, !!voice)],
    ['Text turn', text ? `${text.p50} / ${text.p95} ms` : '—', 'full answer; first progress is sooner', 'neutral'],
    ['Active sessions', m.active_sessions ?? 0, `${esc(m.state_backend)} · ${esc(m.role)}`, 'neutral'],
  ];
}

async function load() {
  $('#error').textContent = '';
  try {
    const r = await fetch('/api/metrics', { headers: token ? { 'X-Admin-Token': token } : {} });
    const m = await r.json();
    if (!r.ok) throw new Error(m.error || `HTTP ${r.status}`);
    $('#tiles').innerHTML = tiles(m).map(([label, value, target, s]) =>
      `<div class="tile ${s}"><div class="tile-label">${esc(label)}</div><div class="tile-value">${esc(value)}</div><div class="tile-target">${esc(target)}</div></div>`).join('');
    const rows = Object.entries(m.latency_ms || {}).filter(([n]) => /^(turn|llm|voice|connector)\./.test(n)).sort();
    $('#latency').innerHTML = '<tr><th>Path</th><th class="num">n</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th></tr>' +
      (rows.length ? rows.map(([n, v]) => `<tr><td>${esc(n)}</td><td class="num">${v.n}</td><td class="num">${v.p50}</td><td class="num">${v.p95}</td><td class="num">${v.p99}</td></tr>`).join('')
        : '<tr><td colspan="5" class="muted">No samples yet.</td></tr>');
    $('#updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $('#error').textContent = `Could not load metrics: ${e.message}`;
  }
}
$('#refresh').onclick = load;
setInterval(() => { if (!document.hidden) load(); }, 10000);
load();
