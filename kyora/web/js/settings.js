/* Settings: GET /api/settings -> a grouped form. Only paths listed in `patchable` are editable.
   Secrets are never shown: the masked value is the placeholder and a secret is sent only when
   the user typed a new one. Save = PUT /api/settings {keys:{dotted:value}, settings:{dotted:value}};
   400 bodies are shown as validation errors; server host/port changes show "restart required". */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  const KIMI_EFFORTS = ['low', 'high', 'max'];
  /* The server refuses these over HTTP: they are the argv of a process Kyora runs. */
  const FILE_ONLY_NOTE = 'Not editable over HTTP: this is a command Kyora executes. Edit it in settings.json in the config folder and restart Kyora.';

  /* section: 'keys' (keys.json) or 'settings' (settings.json); path: dotted path inside it. */
  const GROUPS = [
    { title: 'NVIDIA API Catalog', hint: 'Nemotron-3-Ultra + Kimi K3', fields: [
      { s: 'keys', p: 'nvidia.base_url', label: 'Base URL', type: 'url', help: 'OpenAI-compatible endpoint, default https://integrate.api.nvidia.com/v1' },
      { s: 'keys', p: 'nvidia.nemotron.model', label: 'Nemotron model', type: 'text' },
      { s: 'keys', p: 'nvidia.nemotron.api_key', label: 'Nemotron API key', type: 'secret' },
      { s: 'keys', p: 'nvidia.kimi.model', label: 'Kimi K3 model', type: 'text' },
      { s: 'keys', p: 'nvidia.kimi.api_key', label: 'Kimi K3 API key', type: 'secret' },
      { s: 'keys', p: 'nvidia.nemotron.base_url', label: 'Nemotron base URL', type: 'url', help: 'Empty = the base URL above.' },
      { s: 'keys', p: 'nvidia.kimi.base_url', label: 'Kimi K3 base URL', type: 'url', help: 'Empty = the base URL above. Another OpenAI-compatible endpoint works with its own key, e.g. https://api.moonshot.ai/v1 with a Moonshot API key for the real Kimi K3 (set the model id that endpoint uses).' },
      { s: 'keys', p: 'nvidia.nemotron.fallback_models', label: 'Nemotron fallback models (comma separated)', type: 'csv', help: 'Tried in order when the model is gone (HTTP 404/410) or stays overloaded (503); every substitution is shown in the log and in Self-check.' },
      { s: 'keys', p: 'nvidia.kimi.fallback_models', label: 'Kimi K3 fallback models (comma separated)', type: 'csv', help: 'Tried in order when the model is gone (HTTP 404/410) or stays overloaded (503). NVIDIA retired moonshotai/kimi-k3 on 2026-10-15 (HTTP 410), so these answer the Kimi role there.' },
      { s: 'keys', p: 'nvidia.share_keys', label: 'Share keys between the roles', type: 'bool', help: 'A role whose own key is refused (HTTP 401/403) uses the other role\'s key on the same endpoint. A key is never sent to another endpoint.' }
    ] },
    { title: 'Anthropic', hint: 'Claude without a subscription', fields: [
      { s: 'keys', p: 'anthropic.api_key', label: 'Anthropic API key', type: 'secret', help: 'For the Messages-API engine: the same Claude models without a Claude subscription, billed per use to this key (needs pip install anthropic). The Claude Code CLI uses your subscription login (claude login) instead.' }
    ] },
    { title: 'Kyora agent engine', hint: 'no Claude needed', fields: [
      { s: 'settings', p: 'agent_engine.enabled', label: 'Use the Kyora agent engine', type: 'bool', help: 'Kyora\'s own code engine: BUILD and FIX run on an OpenAI-compatible model with your NVIDIA keys, with the same judged file and shell tools. It is a different model than Claude and is not claimed to match it; the quality gate measures its output exactly like any other engine\'s.' },
      { s: 'settings', p: 'agent_engine.model', label: 'Model', type: 'text', help: 'Needs function calling at the endpoint. Self-check (Run full check) sends one tool-call request to verify it.' },
      { s: 'settings', p: 'agent_engine.fallback_models', label: 'Fallback models (comma separated)', type: 'csv', help: 'Tried in order when a model is gone (HTTP 404/410), stays overloaded or refuses function tools; every substitution is shown in the log.' },
      { s: 'settings', p: 'agent_engine.max_turns', label: 'Max turns', type: 'int', min: 1 },
      { s: 'settings', p: 'agent_engine.max_tokens', label: 'Max output tokens per turn', type: 'int', min: 1 },
      { s: 'settings', p: 'agent_engine.timeout_s', label: 'Wall timeout (s)', type: 'int', min: 1 },
      { s: 'settings', p: 'agent_engine.context_chars', label: 'Context budget (characters)', type: 'int', min: 1, help: 'Above this, older tool output is elided from the conversation; the newest turns are kept.' },
      { s: 'settings', p: 'agent_engine.temperature', label: 'Temperature', type: 'float', min: 0, max: 2, step: 0.05 },
      { s: 'settings', p: 'agent_engine.top_p', label: 'Top p', type: 'float', min: 0, max: 2, step: 0.05 },
      { s: 'settings', p: 'agent_engine.thinking', label: 'Thinking mode (models that support it)', type: 'bool' },
      { s: 'settings', p: 'agent_engine.self_check_tool', label: 'Let the model run Kyora\'s checks (kyora_check tool) before it stops', type: 'bool' },
      { s: 'keys', p: 'agent.base_url', label: 'Endpoint (OpenAI-compatible)', type: 'url', help: 'Empty = the NVIDIA endpoint and keys above (each key tried in turn when one is refused). Any OpenAI-compatible /v1 URL works with its own key below.' },
      { s: 'keys', p: 'agent.api_key', label: 'Endpoint API key', type: 'secret', help: 'Sent only to the endpoint above. Empty on the NVIDIA endpoint = the NVIDIA keys.' }
    ] },
    { title: 'Code engines', hint: 'order + failover', fields: [
      { s: 'settings', p: 'engines.order', label: 'Order (comma separated)', type: 'csv', help: 'The first available engine runs BUILD and FIX. Names: hub (a linked Kyora Hub), claude_code (Claude Code CLI), anthropic_api (Anthropic Messages API), openai_agent (the Kyora agent engine).' },
      { s: 'settings', p: 'engines.failover', label: 'Continue on the next engine after an account failure', type: 'bool', help: 'When an engine stops for an account reason - a usage limit, an ended subscription, no credits, a login or key that is refused, an unreachable service or a retired model - the same BUILD/FIX continues on the next available engine in the order above, told about any partial work. Task failures (max turns, timeouts) never fail over; the gate measures the result as always.' },
      { s: 'settings', p: 'engines.auto_resume_on_limit', label: 'Resume by itself after a usage limit', type: 'bool', help: 'When no other engine is available and the engine reported when its limit resets, the blocked run resumes by itself shortly after that time.' }
    ] },
    { title: 'Claude Code', hint: 'code engine', fields: [
      { s: 'settings', p: 'claude_code.command', label: 'Command (JSON array)', type: 'json', help: 'e.g. ["claude"] or ["npx","@anthropic-ai/claude-code"].', ro: FILE_ONLY_NOTE },
      { s: 'settings', p: 'claude_code.model', label: 'Model', type: 'text', help: 'Requested model; the model actually used is shown on every run.' },
      { s: 'settings', p: 'claude_code.fallback_models', label: 'Fallback models (comma separated)', type: 'csv' },
      { s: 'settings', p: 'claude_code.effort', label: 'Effort', type: 'select', options: EFFORTS },
      { s: 'settings', p: 'claude_code.max_turns', label: 'Max turns', type: 'int' },
      { s: 'settings', p: 'claude_code.timeout_s', label: 'Wall timeout (s)', type: 'int' },
      { s: 'settings', p: 'claude_code.idle_timeout_s', label: 'Idle timeout (s)', type: 'int' },
      { s: 'settings', p: 'claude_code.resume_for_fixes', label: 'Resume the session for fixes', type: 'bool' },
      { s: 'settings', p: 'claude_code.extra_args', label: 'Extra CLI arguments (JSON array)', type: 'json', help: 'Appended to every invocation, e.g. ["--verbose"].', ro: FILE_ONLY_NOTE },
      { s: 'settings', p: 'claude_code.judge_tools', label: 'Judged tools', type: 'json' }
    ] },
    { title: 'Kimi K3', hint: 'planner + judge', fields: [
      { s: 'settings', p: 'kimi.reasoning_effort', label: 'Reasoning effort', type: 'select', options: KIMI_EFFORTS },
      { s: 'settings', p: 'kimi.judge_reasoning_effort', label: 'Judge reasoning effort', type: 'select', options: KIMI_EFFORTS },
      { s: 'settings', p: 'kimi.effort_field', label: 'Effort field', type: 'select', options: ['top_level', 'extra_body'], help: 'Switch to extra_body if the gateway rejects a top-level reasoning_effort (Self-check shows the error). extra_body sends chat_template_kwargs.reasoning_effort, which Kimi K3 is not documented to read: best-effort, the model may ignore it.' }
    ] },
    { title: 'Nemotron-3-Ultra', hint: 'design director', fields: [
      { s: 'settings', p: 'nemotron.thinking_for_design', label: 'Thinking mode for design documents', type: 'bool' }
    ] },
    { title: 'Quality gate', hint: 'measured per run', fields: [
      { s: 'settings', p: 'quality.min_pass_rate', label: 'Minimum pass rate (0-1)', type: 'float', min: 0, max: 1, step: 0.01, help: '0.94 = at least 94 % of counted checks, plus every required check.' },
      { s: 'settings', p: 'quality.max_fix_iterations', label: 'Max fix iterations', type: 'int' },
      { s: 'settings', p: 'quality.browser_wait_s', label: 'Wait for browser tests (s)', type: 'int', help: 'After this the run is BLOCKED until a browser runs the tests.' },
      { s: 'settings', p: 'quality.require_plan_approval', label: 'Require plan approval', type: 'bool' },
      { s: 'settings', p: 'quality.auto_run_browser_tests', label: 'Auto-run browser tests on the device that started the run', type: 'bool' }
    ] },
    { title: 'Kyora Hub', hint: 'LAN code engine', fields: [
      { s: 'keys', p: 'hub.url', label: 'Hub URL', type: 'url', help: 'A PC or Android Kyora started with "kyora hub". Easiest: scan its QR or open its link.' },
      { s: 'keys', p: 'hub.token', label: 'Hub token', type: 'secret' },
      { s: 'settings', p: 'hub.expose_ui', label: 'Expose this UI on the LAN (token required)', type: 'bool' }
    ] },
    { title: 'NVIDIA Tokkio', hint: 'presenter', fields: [
      { s: 'keys', p: 'tokkio.ui_url', label: 'Tokkio UI URL', type: 'url', help: 'Your deployed Tokkio web UI. Leave empty until it is deployed.' },
      { s: 'keys', p: 'tokkio.configurator_url', label: 'ACE Configurator URL', type: 'url' }
    ] },
    { title: 'Server', hint: 'restart required', fields: [
      { s: 'settings', p: 'server.host', label: 'Bind host', type: 'text', help: '127.0.0.1 keeps Kyora local; 0.0.0.0 listens on the LAN.' },
      { s: 'settings', p: 'server.port', label: 'Port', type: 'int', min: 1, max: 65535 },
      { s: 'settings', p: 'server.preview_port', label: 'Preview port', type: 'int', help: 'Separate origin for game previews and browser tests; empty = port + 1.' },
      { s: 'settings', p: 'server.allowed_hosts', label: 'Allowed host names (JSON array)', type: 'json', help: 'Extra Host names accepted besides localhost and this machine\'s LAN names, e.g. a reverse proxy.' },
      { s: 'settings', p: 'server.workers', label: 'Run workers', type: 'int' }
    ] }
  ];

  let data = null;
  let notice = null; // {tone, html}
  let saving = false;

  function U() { return K.util; }
  function fid(f) { return 'set-' + f.s + '-' + f.p.replace(/\./g, '-'); }
  function getPath(obj, path) {
    let cur = obj;
    const parts = path.split('.');
    for (let i = 0; i < parts.length; i++) {
      if (!cur || typeof cur !== 'object' || !(parts[i] in cur)) return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }
  function editable(f) {
    return !!(data && Array.isArray(data.patchable) && data.patchable.indexOf(f.p) >= 0);
  }

  /* Display representation of a stored value (string, or boolean for checkboxes). */
  function toRaw(f, v) {
    if (f.type === 'bool') return !!v;
    if (f.type === 'secret') return '';
    if (v == null) return '';
    if (f.type === 'json') return JSON.stringify(v);
    if (f.type === 'csv') return Array.isArray(v) ? v.join(', ') : String(v);
    return String(v);
  }

  function fieldHtml(f) {
    const v = getPath(data[f.s], f.p);
    const id = fid(f);
    const ro = !editable(f);
    const dis = ro ? ' disabled' : '';
    const help = (f.help || '') + (ro ? (f.help ? ' ' : '') + (f.ro || 'Read-only here; edit ' + (f.s === 'keys' ? 'keys.json' : 'settings.json') + ' in the config folder.') : '');
    const attrs = ' id="' + id + '" data-s="' + f.s + '" data-p="' + U().esc(f.p) + '" data-type="' + f.type + '"' + dis;
    let control;
    if (f.type === 'bool') {
      return '<div class="field" data-field="' + U().esc(f.s + '.' + f.p) + '"><label class="check"><input type="checkbox"' + attrs + (v ? ' checked' : '') + '><span>' + U().esc(f.label) + '</span></label>' +
        (help ? '<div class="help">' + U().esc(help) + '</div>' : '') + '<div class="error" role="alert" hidden></div></div>';
    }
    if (f.type === 'select') {
      const opts = f.options.slice();
      if (v != null && opts.indexOf(String(v)) < 0) opts.push(String(v));
      control = '<select' + attrs + '>' + opts.map((o) => '<option value="' + U().esc(o) + '"' + (String(v) === o ? ' selected' : '') + '>' + U().esc(o) + '</option>').join('') + '</select>';
    } else if (f.type === 'secret') {
      const masked = v ? String(v) : '';
      control = '<input type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" data-masked="true"' + attrs +
        ' value="" placeholder="' + U().esc(masked || 'not set') + '" aria-describedby="' + id + '-help">';
    } else {
      const type = f.type === 'int' || f.type === 'float' ? 'number' : (f.type === 'url' ? 'url' : 'text');
      const num = type === 'number' ? (' inputmode="' + (f.type === 'int' ? 'numeric' : 'decimal') + '" step="' + (f.step || 1) + '"' + (f.min != null ? ' min="' + f.min + '"' : '') + (f.max != null ? ' max="' + f.max + '"' : '')) : '';
      control = '<input type="' + type + '" spellcheck="false" autocapitalize="off"' + num + attrs + ' value="' + U().esc(toRaw(f, v)) + '"' + (f.type === 'json' || f.type === 'csv' ? ' class="mono"' : '') + '>';
    }
    return '<div class="field" data-field="' + U().esc(f.s + '.' + f.p) + '"><label for="' + id + '">' + U().esc(f.label) + '</label>' + control +
      '<div class="help" id="' + id + '-help">' + U().esc(help ? help + (f.type === 'secret' ? ' ' : '') : '') + U().esc(f.type === 'secret' ? (v ? 'Stored and masked; type a new value to replace it.' : 'Not set; type a value to store it.') : '') + '</div><div class="error" role="alert" hidden></div></div>';
  }

  function render() {
    const root = document.getElementById('settings-root');
    if (!root) return;
    const sub = document.getElementById('settings-sub');
    if (!data) {
      root.innerHTML = '<section class="card"><div class="empty"><strong>Settings unavailable</strong><span class="why">GET /api/settings did not answer. Is the Kyora server running?</span></div></section>';
      return;
    }
    if (sub) sub.innerHTML = 'Keys are masked; typing a new value replaces the stored one. Masked values are never sent back.' + (data.config_dir ? ' Config folder: <code>' + U().esc(data.config_dir) + '</code>' : '');
    let html = '<div id="settings-notice"' + (notice ? '' : ' hidden') + '>' + (notice ? '<div class="notice" data-tone="' + notice.tone + '" role="status">' + notice.html + '</div>' : '') + '</div>';
    html += GROUPS.map((g, gi) => '<section class="card settings-group" aria-labelledby="sg-' + gi + '"><h2 id="sg-' + gi + '">' + U().esc(g.title) + (g.hint ? ' <span class="hint">' + U().esc(g.hint) + '</span>' : '') + '</h2>' +
      g.fields.filter((f) => getPath(data[f.s], f.p) !== undefined || editable(f)).map(fieldHtml).join('') + '</section>').join('');
    html += '<div class="settings-actions"><span class="dirty" id="settings-dirty" aria-live="polite">No unsaved changes</span>' +
      '<button type="button" class="btn ghost" id="btn-settings-revert" disabled>Revert</button>' +
      '<button type="button" class="btn primary" id="btn-settings-save" disabled>Save</button></div>';
    root.innerHTML = html;
    Array.prototype.forEach.call(root.querySelectorAll('input[data-p], select[data-p]'), (el) => {
      el.setAttribute('data-initial', el.type === 'checkbox' ? String(el.checked) : el.value);
    });
    updateDirty();
  }

  function inputs() {
    const root = document.getElementById('settings-root');
    return root ? Array.prototype.slice.call(root.querySelectorAll('input[data-p], select[data-p]')) : [];
  }
  function isDirty(el) {
    if (el.disabled) return false;
    if (el.getAttribute('data-type') === 'secret') return el.value.trim() !== '';
    return (el.type === 'checkbox' ? String(el.checked) : el.value) !== el.getAttribute('data-initial');
  }
  function dirtyInputs() { return inputs().filter(isDirty); }

  function updateDirty() {
    const n = dirtyInputs().length;
    const label = document.getElementById('settings-dirty');
    const save = document.getElementById('btn-settings-save');
    const revert = document.getElementById('btn-settings-revert');
    if (label) label.textContent = n ? n + ' unsaved change' + (n > 1 ? 's' : '') : 'No unsaved changes';
    if (save) save.disabled = !n || saving;
    if (revert) revert.disabled = !n || saving;
  }

  function setFieldError(el, msg) {
    const field = el.closest('.field');
    const box = field && field.querySelector('.error');
    if (!box) return;
    box.textContent = msg || '';
    box.hidden = !msg;
    if (msg) el.setAttribute('aria-invalid', 'true'); else el.removeAttribute('aria-invalid');
  }

  /* The field's min/max (rendered as input attributes), checked before the server validates them again. */
  function rangeError(el, n) {
    const lo = el.min != null && el.min !== '' ? Number(el.min) : null;
    const hi = el.max != null && el.max !== '' ? Number(el.max) : null;
    if (lo != null && hi != null && (n < lo || n > hi)) return { error: 'Use a value between ' + lo + ' and ' + hi + '.' };
    if (lo != null && n < lo) return { error: 'Must be at least ' + lo + '.' };
    if (hi != null && n > hi) return { error: 'Must be at most ' + hi + '.' };
    return null;
  }

  /* Parse an input into the JSON value the API expects; returns {value} or {error}. */
  function parseInput(el) {
    const type = el.getAttribute('data-type');
    const path = el.getAttribute('data-p');
    const raw = el.type === 'checkbox' ? el.checked : el.value.trim();
    if (type === 'bool') return { value: !!raw };
    if (type === 'int') {
      if (!/^-?\d+$/.test(raw)) return { error: 'Enter a whole number.' };
      const n = parseInt(raw, 10);
      if (n < 0) return { error: 'Must not be negative.' };
      if (path === 'server.port' && (n < 1 || n > 65535)) return { error: 'Port must be 1-65535.' };
      return rangeError(el, n) || { value: n };
    }
    if (type === 'float') {
      const n = Number(raw);
      if (raw === '' || !isFinite(n)) return { error: 'Enter a number.' };
      if (path === 'quality.min_pass_rate' && (n < 0 || n > 1)) return { error: 'Use a fraction between 0 and 1 (0.94 = 94 %).' };
      return rangeError(el, n) || { value: n };
    }
    if (type === 'json') {
      let v;
      try { v = JSON.parse(raw || '[]'); } catch (err) { return { error: 'Not valid JSON: ' + err.message }; }
      if (!Array.isArray(v) || !v.every((x) => typeof x === 'string')) return { error: 'Must be a JSON array of strings, e.g. ["claude"].' };
      if (path === 'claude_code.command' && !v.length) return { error: 'The command needs at least one element.' };
      return { value: v };
    }
    if (type === 'csv') return { value: raw.split(',').map((x) => x.trim()).filter(Boolean) };
    if (type === 'url') {
      if (raw && !/^https?:\/\/[^\s]+$/i.test(raw)) return { error: 'Enter an http(s):// URL or leave it empty.' };
      return { value: raw.replace(/\/+$/, '') };
    }
    return { value: raw };
  }

  async function save() {
    if (saving) return;
    const dirty = dirtyInputs();
    if (!dirty.length) return;
    const patch = { keys: {}, settings: {} };
    let bad = 0;
    dirty.forEach((el) => {
      const r = parseInput(el);
      setFieldError(el, r.error || '');
      if (r.error) { bad += 1; return; }
      patch[el.getAttribute('data-s')][el.getAttribute('data-p')] = r.value;
    });
    if (bad) { K.toast('Fix the highlighted field' + (bad > 1 ? 's' : '') + ' first.', 'warn'); return; }
    saving = true;
    updateDirty();
    const r = await K.api.maybe(K.api.put('/api/settings', patch));
    saving = false;
    if (!r.ok) {
      const msg = U().errText(r.error);
      let mapped = false;
      dirty.forEach((el) => {
        const p = el.getAttribute('data-p');
        const last = p.split('.').pop();
        if (msg.indexOf(p) >= 0 || msg.indexOf(last) >= 0) { setFieldError(el, msg); mapped = true; }
      });
      showNotice('danger', '<strong>Not saved:</strong> ' + U().esc(msg) + (mapped ? '' : ' (HTTP ' + U().esc(r.error.status) + ')'));
      updateDirty();
      return;
    }
    const res = r.data || {};
    const changed = Array.isArray(res.changed) ? res.changed : [];
    K.toast(changed.length ? 'Saved: ' + changed.length + ' setting' + (changed.length > 1 ? 's' : '') + ' changed' : 'Nothing changed', 'ok');
    notice = res.restart_required ? { tone: 'warn', html: '<strong>Restart required.</strong> Server host/port changes take effect after Kyora is restarted.' } : null;
    data = null;
    const fresh = await K.app.loadSettings();
    data = fresh;
    render();
    K.app.refreshStatus();
  }

  function showNotice(tone, html) {
    notice = { tone, html };
    const box = document.getElementById('settings-notice');
    if (box) { box.innerHTML = '<div class="notice" data-tone="' + tone + '" role="status">' + html + '</div>'; box.hidden = false; }
  }

  function init() {
    const root = document.getElementById('settings-root');
    if (root) {
      root.addEventListener('input', (e) => { if (e.target.matches('[data-p]')) { setFieldError(e.target, ''); updateDirty(); } });
      root.addEventListener('change', (e) => { if (e.target.matches('[data-p]')) updateDirty(); });
      root.addEventListener('click', (e) => {
        if (e.target.closest('#btn-settings-save')) save();
        else if (e.target.closest('#btn-settings-revert')) { notice = null; render(); }
      });
      root.innerHTML = '<section class="card"><div class="row"><span class="spinner" aria-hidden="true"></span><span>Loading settings…</span></div></section>';
    }
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('settings') < 0) return;
      if (saving) return;
      if (data && dirtyInputs().length) return; // keep the user's edits
      data = s.settings;
      render();
    });
    window.addEventListener('beforeunload', (e) => {
      if (dirtyInputs().length) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  (K.modules = K.modules || {}).settings = { init, render, parseInput, GROUPS };
})();
