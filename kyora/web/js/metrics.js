/* Measured model metrics: tiles + sparklines.
   Values come only from model.metrics (Nemotron / Kimi) and engine.* events (Claude Code).
   Unknown values render as "-". Nothing is estimated here; when the backend reports
   tokens_source != "usage" the tile says so. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  const SYSTEMS = [
    { key: 'nemotron', name: 'Nemotron-3-Ultra', role: 'design director', match: /nemotron/i },
    { key: 'kimi', name: 'Kimi K3', role: 'planner + judge', match: /kimi/i },
    { key: 'claude', name: 'Claude Code', role: 'code engine (effort max)', match: /claude|anthropic|hub/i }
  ];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function emptyMetrics() {
    const m = {};
    SYSTEMS.forEach((s) => {
      m[s.key] = { model: null, tps: null, ttft_ms: null, total_ms: null, prompt_tokens: null, completion_tokens: null,
        tokens_source: null, history: [], calls: 0, busy: false, error: null, turns: null, cost_usd: null, duration_ms: null,
        fallback_used: false, requested: null, backend: null, last_at: null };
    });
    return m;
  }

  /* Map an event's role/model to a tile key. */
  function systemFor(role, model, backend) {
    const hay = [role, model, backend].filter(Boolean).join(' ');
    for (const s of SYSTEMS) if (s.match.test(hay)) return s.key;
    if (/judge|plan|intake|review/i.test(role || '')) return 'kimi';
    if (/design|narr|ui|copy|gdd/i.test(role || '')) return 'nemotron';
    return null;
  }

  function applyMetricsEvent(metrics, ev) {
    const d = ev.data || {};
    const t = ev.type;
    if (t === 'model.request' || t === 'model.metrics' || t === 'model.done' || t === 'model.error' || t === 'model.delta') {
      const key = systemFor(d.role, d.model);
      if (!key) return null;
      const m = metrics[key];
      if (d.model) m.model = d.model;
      if (t === 'model.request') { m.busy = true; m.error = null; }
      if (t === 'model.done') { m.busy = false; }
      if (t === 'model.error') { m.busy = false; m.error = d.error || ('status ' + d.status); }
      if (t === 'model.metrics') {
        m.busy = false;
        m.calls += 1;
        m.last_at = ev.ts || Date.now() / 1000;
        ['tps', 'ttft_ms', 'total_ms', 'prompt_tokens', 'completion_tokens', 'tokens_source'].forEach((f) => {
          if (d[f] !== undefined) m[f] = d[f];
        });
        if (typeof d.tps === 'number') {
          m.history.push(d.tps);
          if (m.history.length > 60) m.history.shift();
        }
      }
      return key;
    }
    if (t.indexOf('engine.') === 0) {
      const m = metrics.claude;
      if (t === 'engine.session') { m.busy = true; m.model = d.model || m.model; m.backend = d.backend || m.backend; m.error = null; }
      if (t === 'engine.model_resolved') { m.model = d.active || m.model; m.requested = d.requested || m.requested; m.fallback_used = !!d.fallback_used; }
      if (t === 'engine.result') {
        m.busy = false; m.calls += 1; m.last_at = ev.ts || Date.now() / 1000;
        if (d.num_turns != null) m.turns = d.num_turns;
        if (d.duration_ms != null) m.duration_ms = d.duration_ms;
        if (d.cost_usd !== undefined) m.cost_usd = d.cost_usd;
        if (d.is_error) m.error = d.subtype || 'error';
      }
      if (t === 'engine.error') { m.busy = false; m.error = d.error || 'engine error'; }
      return 'claude';
    }
    return null;
  }

  function fmtNum(v, digits) {
    if (v == null || v === '' || isNaN(v)) return '-';
    const n = Number(v);
    if (digits === undefined) digits = n >= 100 ? 0 : n >= 10 ? 1 : 2;
    return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  }
  function fmtMs(ms) {
    if (ms == null || isNaN(ms)) return '-';
    if (ms < 1000) return Math.round(ms) + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + ' s';
    return Math.floor(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
  }
  function fmtTokens(p, c) {
    if (p == null && c == null) return '-';
    const parts = [];
    if (p != null) parts.push(fmtNum(p, 0) + ' in');
    if (c != null) parts.push(fmtNum(c, 0) + ' out');
    return parts.join(' / ');
  }

  function sparkline(values, w, h) {
    w = w || 120; h = h || 34;
    if (!values || values.length < 2) {
      return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true"><path d="M0 ' + (h - 2) + ' H' + w + '" stroke-dasharray="2 3" opacity="0.4"/></svg>';
    }
    const max = Math.max.apply(null, values), min = Math.min.apply(null, values);
    const span = (max - min) || 1;
    const step = w / (values.length - 1);
    const pts = values.map((v, i) => [i * step, h - 3 - ((v - min) / span) * (h - 8)]);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const area = d + ' L' + w + ' ' + h + ' L0 ' + h + ' Z';
    return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true"><path class="spark-area" d="' + area + '"/><path d="' + d + '"/></svg>';
  }

  function renderTiles(metrics) {
    return SYSTEMS.map((s) => {
      const m = metrics[s.key];
      const isClaude = s.key === 'claude';
      const big = isClaude
        ? (m.turns != null ? fmtNum(m.turns, 0) + '<small>turns</small>' : '-<small>turns</small>')
        : (fmtNum(m.tps) + '<small>tok/s</small>');
      const stats = isClaude
        ? [['duration', fmtMs(m.duration_ms)], ['cost', m.cost_usd == null ? '-' : '$' + fmtNum(m.cost_usd, 3)], ['tokens', m.completion_tokens == null && m.prompt_tokens == null ? '-' : fmtTokens(m.prompt_tokens, m.completion_tokens)]]
        : [['TTFT', fmtMs(m.ttft_ms)], ['latency', fmtMs(m.total_ms)], ['tokens', fmtTokens(m.prompt_tokens, m.completion_tokens)]];
      const foot = [];
      if (m.busy) foot.push('<span class="tile-live">● live</span>');
      if (m.error) foot.push('<span style="color:var(--danger)">' + esc(String(m.error).slice(0, 60)) + '</span>');
      if (!isClaude && m.tokens_source && m.tokens_source !== 'usage') foot.push('<span title="Token counts were not reported by the API; backend marked them as ' + esc(m.tokens_source) + '">tokens: ' + esc(m.tokens_source) + '</span>');
      if (isClaude && m.fallback_used) foot.push('<span style="color:var(--warn)">fallback from ' + esc(m.requested || '?') + '</span>');
      foot.push('<span>' + (m.calls ? m.calls + (isClaude ? ' session' : ' call') + (m.calls > 1 ? 's' : '') : 'no calls yet') + '</span>');
      return (
        '<article class="tile" data-sys="' + s.key + '" aria-label="' + esc(s.name) + ' metrics">' +
        '<div class="tile-head"><span class="tile-name">' + esc(s.name) + '</span><span class="tile-role">' + esc(s.role) + '</span></div>' +
        '<div class="tile-model" title="' + esc(m.model || '') + '">' + esc(m.model || (isClaude ? 'model resolved at session start' : 'model not called yet')) + '</div>' +
        '<div class="tile-main"><div class="tile-big">' + big + '</div>' + (isClaude ? '' : sparkline(m.history)) + '</div>' +
        '<div class="tile-stats">' + stats.map((p) => '<div>' + esc(p[0]) + '<b>' + esc(p[1]) + '</b></div>').join('') + '</div>' +
        '<div class="tile-foot">' + foot.join('') + '</div>' +
        '</article>'
      );
    }).join('');
  }

  K.metrics = { SYSTEMS, emptyMetrics, applyMetricsEvent, renderTiles, sparkline, fmtNum, fmtMs, fmtTokens, systemFor };
})();
