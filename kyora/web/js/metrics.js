/* Measured model metrics: tiles + sparklines.
   Values come only from model.metrics (Nemotron / Kimi) and engine.* events (the code engine:
   Claude Code, the Anthropic API, a Kyora Hub or the Kyora agent engine). Unknown values render
   as "-". Nothing is estimated here; when the backend reports tokens_source != "usage" the tile
   says so. The code-engine tile is named after the run's recorded backend (just "Code engine"
   until one is known) and effort; Claude Code's total_cost_usd is labelled as the CLI's list-price
   estimate, and an engine that reports no cost shows none. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  const SYSTEMS = [
    { key: 'nemotron', name: 'Nemotron-3-Ultra', role: 'design director', match: /nemotron/i },
    { key: 'kimi', name: 'Kimi K3', role: 'planner + judge', match: /kimi/i },
    { key: 'claude', name: 'Code engine', role: 'code engine', match: /claude|anthropic|hub|openai_agent|kyora agent/i }
  ];
  /* Roles that belong to the code engine although their model may be named like another tile's
     (the Kyora agent engine's fallback model can be a Nemotron). */
  const ENGINE_ROLES = ['agent', 'openai_agent', 'engine', 'code_engine'];
  /* Backends pipeline-chart.js may not name yet; K.engineLabel decides the rest. */
  const BACKEND_LABELS = { openai_agent: 'Kyora agent', 'hub:openai_agent': 'Kyora agent (Hub)' };

  function backendLabel(backend) {
    if (!backend) return '';
    if (BACKEND_LABELS[backend]) return BACKEND_LABELS[backend];
    return K.engineLabel ? K.engineLabel(backend) : String(backend);
  }
  function isAgent(backend) { return /openai_agent/.test(String(backend || '')); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function emptyMetrics() {
    const m = {};
    SYSTEMS.forEach((s) => {
      m[s.key] = { model: null, tps: null, ttft_ms: null, total_ms: null, prompt_tokens: null, completion_tokens: null,
        tokens_source: null, history: [], calls: 0, busy: false, error: null, turns: null, cost_usd: null, duration_ms: null,
        fallback_used: false, requested: null, backend: null, effort: null, last_at: null, failover_note: null };
    });
    return m;
  }

  /* Map an event's role/model to a tile key. */
  function systemFor(role, model, backend) {
    // the role decides first: a Kimi role answered by a Nemotron fallback model stays on the Kimi tile,
    // the agent engine's role on the code-engine tile
    if (role && ENGINE_ROLES.indexOf(String(role).toLowerCase()) >= 0) return 'claude';
    if (backend && SYSTEMS[2].match.test(String(backend))) return 'claude';
    for (const s of SYSTEMS) if (role && role.toLowerCase() === s.key) return s.key;
    const hay = [role, model, backend].filter(Boolean).join(' ');
    for (const s of SYSTEMS) if (s.match.test(hay)) return s.key;
    if (/judge|plan|intake|review/i.test(role || '')) return 'kimi';
    if (/design|narr|ui|copy|gdd/i.test(role || '')) return 'nemotron';
    return null;
  }

  function applyMetricsEvent(metrics, ev) {
    const d = ev.data || {};
    const t = ev.type;
    if (t === 'model.fallback') {
      const key = systemFor(d.role, d.model || d.to_model);
      if (!key) return null;
      const m = metrics[key];
      if (d.kind === 'key') m.key_note = (d.from_key || 'own key') + ' refused' + (d.status ? ' (HTTP ' + d.status + ')' : '') + ', using ' + (d.to_key || 'another key');
      else { m.fallback_used = true; m.requested = d.requested || d.from_model || m.requested; m.model = d.to_model || m.model; }
      return key;
    }
    if (t === 'model.request' || t === 'model.metrics' || t === 'model.done' || t === 'model.error' || t === 'model.delta') {
      const key = systemFor(d.role, d.model);
      if (!key) return null;
      const m = metrics[key];
      if (d.model) m.model = d.model;
      if (d.fallback && d.requested_model) { m.fallback_used = true; m.requested = d.requested_model; }
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
      if (t === 'engine.failover') {
        // the next engine takes over: its own session names its model and effort
        m.busy = false; m.error = null; m.backend = d.to || m.backend; m.model = null; m.effort = null;
        m.fallback_used = false; m.requested = null;
        m.failover_note = 'after ' + (backendLabel(d.from) || d.from || 'an engine') + ' stopped' + (d.failure_kind ? ' (' + d.failure_kind + ')' : '');
      }
      if (t === 'engine.session') { m.busy = true; m.model = d.model || m.model; m.backend = d.backend || m.backend; m.effort = d.effort || m.effort; m.error = null; }
      if (t === 'engine.model_resolved') { m.model = d.active || m.model; m.requested = d.requested || m.requested; m.fallback_used = !!d.fallback_used; }
      if (t === 'engine.result') {
        m.busy = false; m.calls += 1; m.last_at = ev.ts || Date.now() / 1000;
        if (d.num_turns != null) m.turns = d.num_turns;
        if (d.duration_ms != null) m.duration_ms = d.duration_ms;
        if (d.cost_usd !== undefined) m.cost_usd = d.cost_usd;
        if (d.usage && typeof d.usage === 'object') {  // the endpoint's own usage numbers, when the engine sends them
          if (typeof d.usage.prompt_tokens === 'number') m.prompt_tokens = d.usage.prompt_tokens;
          if (typeof d.usage.completion_tokens === 'number') m.completion_tokens = d.usage.completion_tokens;
        }
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

  /* Claude Code's total_cost_usd is computed by the CLI at API list prices; with a subscription login
     nothing is charged per run, so it is shown as an API-equivalent estimate, never as a measured cost. */
  const COST_NOTE = 'Claude Code\'s total_cost_usd: an API-equivalent estimate at list prices computed by the CLI, not a measured charge. A Claude subscription login is not billed per run.';
  const AGENT_COST_NOTE = 'The Kyora agent engine\'s endpoint reports no cost, so none is shown and none is estimated. Token totals are the endpoint\'s own usage numbers.';

  /* engine: the run's recorded engine info ({backend, effort}) from run.json / engine events, optional. */
  function renderTiles(metrics, engine) {
    engine = engine || {};
    return SYSTEMS.map((s) => {
      const m = metrics[s.key];
      const isClaude = s.key === 'claude';
      const backend = isClaude ? (m.backend || engine.backend || '') : '';
      const agent = isClaude && isAgent(backend);
      const effort = isClaude ? (engine.effort || m.effort || '') : '';
      const name = isClaude && backend ? (backendLabel(backend) || s.name) : s.name;
      // the agent engine has no Claude effort level; its session says whether thinking mode was sent
      const role = agent ? (effort === 'thinking' ? s.role + ' · thinking' : s.role)
        : (isClaude && effort ? s.role + ' · effort ' + effort : s.role);
      const big = isClaude
        ? (m.turns != null ? fmtNum(m.turns, 0) + '<small>turns</small>' : '-<small>turns</small>')
        : (fmtNum(m.tps) + '<small>tok/s</small>');
      const cost = agent && m.cost_usd == null
        ? ['cost', 'not reported', AGENT_COST_NOTE, 'cost']
        : ['API-equiv. est.', m.cost_usd == null ? '-' : '~$' + fmtNum(m.cost_usd, 3), COST_NOTE, 'cost'];
      const stats = isClaude
        ? [['duration', fmtMs(m.duration_ms)],
          cost,
          ['tokens', m.completion_tokens == null && m.prompt_tokens == null ? '-' : fmtTokens(m.prompt_tokens, m.completion_tokens)]]
        : [['TTFT', fmtMs(m.ttft_ms)], ['latency', fmtMs(m.total_ms)], ['tokens', fmtTokens(m.prompt_tokens, m.completion_tokens)]];
      const foot = [];
      if (m.busy) foot.push('<span class="tile-live">● live</span>');
      if (m.error) foot.push('<span style="color:var(--danger)">' + esc(String(m.error).slice(0, 60)) + '</span>');
      if (!isClaude && m.tokens_source && m.tokens_source !== 'usage') foot.push('<span title="Token counts were not reported by the API; backend marked them as ' + esc(m.tokens_source) + '">tokens: ' + esc(m.tokens_source) + '</span>');
      if (m.fallback_used) foot.push('<span style="color:var(--warn)">fallback from ' + esc(m.requested || '?') + '</span>');
      if (m.key_note) foot.push('<span style="color:var(--warn)">' + esc(m.key_note) + '</span>');
      if (isClaude && m.failover_note) foot.push('<span style="color:var(--warn)">' + esc(m.failover_note) + '</span>');
      foot.push('<span>' + (m.calls ? m.calls + (isClaude ? ' session' : ' call') + (m.calls > 1 ? 's' : '') : 'no calls yet') + '</span>');
      return (
        '<article class="tile" data-sys="' + s.key + '" aria-label="' + esc(s.name) + ' metrics">' +
        '<div class="tile-head"><span class="tile-name">' + esc(name) + '</span><span class="tile-role">' + esc(role) + '</span></div>' +
        '<div class="tile-model" title="' + esc(m.model || '') + '">' + esc(m.model || (isClaude ? 'model resolved at session start' : 'model not called yet')) + '</div>' +
        '<div class="tile-main"><div class="tile-big">' + big + '</div>' + (isClaude ? '' : sparkline(m.history)) + '</div>' +
        '<div class="tile-stats">' + stats.map((p) => '<div' + (p[3] ? ' data-stat="' + esc(p[3]) + '"' : '') + (p[2] ? ' title="' + esc(p[2]) + '"' : '') + '>' + esc(p[0]) + '<b>' + esc(p[1]) + '</b>' + (p[2] ? '<span class="sr-only">' + esc(p[2]) + '</span>' : '') + '</div>').join('') + '</div>' +
        '<div class="tile-foot">' + foot.join('') + '</div>' +
        '</article>'
      );
    }).join('');
  }

  K.metrics = { SYSTEMS, emptyMetrics, applyMetricsEvent, renderTiles, sparkline, fmtNum, fmtMs, fmtTokens, systemFor, backendLabel };
})();
