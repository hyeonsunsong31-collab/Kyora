/* Self-check: what is really reachable from this device (GET /api/selfcheck, POST
   /api/selfcheck/run), platform facts from /api/status, LAN URLs with QR codes, and the Hub link
   (hash '#hub=<url>&token=<t>' or '#/selfcheck?hub=..&token=..', confirmed by the user, then
   PUT /api/settings {keys:{hub:{url,token}}}). Every value shown is measured, never assumed. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  let app = null;
  let report = null;
  let reportError = null;
  let loadedAt = 0;
  let running = false;
  let lan = null;
  let linkBusy = false;

  function U() { return K.util; }
  function state() { return K.store.state; }
  function $(id) { return document.getElementById(id); }

  /* ---------------------------------------------------------------- report */
  async function load(force) {
    if (!force && report && Date.now() - loadedAt < 30000) { render(); return; }
    const r = await K.api.maybe(K.api.get('/api/selfcheck'));
    if (r.ok) { report = r.data; reportError = null; loadedAt = Date.now(); }
    else reportError = r;
    render();
  }

  async function runCheck(body, label) {
    if (running) return;
    running = true;
    setBusy(label);
    const r = await K.api.maybe(K.api.post('/api/selfcheck/run', body));
    running = false;
    setBusy('');
    if (r.ok) {
      report = r.data;
      reportError = null;
      loadedAt = Date.now();
      const s = report.summary || {};
      K.toast('Self-check done: ' + (s.ok || 0) + ' ok, ' + (s.warn || 0) + ' warn, ' + (s.fail || 0) + ' fail', s.fail ? 'warn' : 'ok');
      app.refreshStatus();
    } else {
      reportError = r;
      K.toast(r.missing ? 'Self-check is not available on this server.' : 'Self-check failed: ' + U().errText(r.error), 'danger');
    }
    render();
  }

  function setBusy(label) {
    ['btn-selfcheck-run', 'btn-selfcheck-claude'].forEach((id) => { const b = $(id); if (b) b.disabled = !!label; });
    const when = $('selfcheck-when');
    if (when && label) when.innerHTML = '<span class="spinner" aria-hidden="true" style="width:12px;height:12px;vertical-align:-2px"></span> ' + U().esc(label);
  }

  function fmtMeasured(m) {
    if (!m || typeof m !== 'object') return '';
    return Object.keys(m).filter((k) => m[k] != null && m[k] !== '' && !(Array.isArray(m[k]) && !m[k].length)).map((k) => {
      const v = m[k];
      let s;
      if (/_ms$/.test(k) && typeof v === 'number') s = K.metrics.fmtMs(v);
      else if (/_bytes$/.test(k) && typeof v === 'number') s = (v / 1073741824).toFixed(1) + ' GB';
      else if (Array.isArray(v)) s = v.join(', ');
      else if (typeof v === 'object') s = JSON.stringify(v);
      else s = String(v);
      return '<div><span style="color:var(--fg-subtle)">' + U().esc(k) + ':</span> ' + U().esc(U().clip(s, 160)) + '</div>';
    }).join('');
  }

  function render() {
    const host = $('selfcheck-table');
    const when = $('selfcheck-when');
    if (!host) return;
    if (!report) {
      if (reportError) {
        const e = reportError.error || {};
        const missing = reportError.missing || e.status === 503;
        host.innerHTML = '<div class="empty"><strong>' + (missing ? 'Self-check not available' : 'Self-check failed') + '</strong><span class="why">' +
          U().esc(missing ? 'This server did not load kyora/selfcheck.py' + (e.message ? ' (' + e.message + ')' : '') + '. The platform facts below come from /api/status.' : U().errText(e)) + '</span></div>';
      } else {
        host.innerHTML = '<div class="row"><span class="spinner" aria-hidden="true"></span><span>Loading the last report…</span></div>';
      }
      if (when && !running) when.textContent = '';
      return;
    }
    const items = Array.isArray(report.items) ? report.items : [];
    const s = report.summary || {};
    if (when && !running) {
      when.textContent = (report.generated_at ? U().relTime(report.generated_at) + ' · ' : '') + (s.ok || 0) + ' ok · ' + (s.warn || 0) + ' warn · ' + (s.fail || 0) + ' fail' + (s.skip ? ' · ' + s.skip + ' skipped' : '') +
        (report.probe_models === false ? ' · models not probed' : '');
    }
    host.innerHTML = '<div class="table-wrap"><table class="data"><thead><tr><th scope="col">Status</th><th scope="col">Check</th><th scope="col">Measured</th></tr></thead><tbody>' +
      items.map((it) => {
        const st = ['ok', 'warn', 'fail', 'skip'].indexOf(it.status) >= 0 ? it.status : 'skip';
        return '<tr data-check="' + U().esc(it.id) + '"><td style="white-space:nowrap"><span class="status-mark" data-status="' + st + '" aria-hidden="true"></span>' + U().esc(st) + '</td>' +
          '<td><b>' + U().esc(it.label || it.id) + '</b><div style="color:var(--fg-muted);word-break:break-word">' + U().esc(it.detail || '') + '</div></td>' +
          '<td style="font-family:var(--font-mono);font-size:var(--fs-0);min-width:120px">' + fmtMeasured(it.measured) + '</td></tr>';
      }).join('') + '</tbody></table></div>' +
      (report.probe_models === false ? '<p class="help" style="margin:8px 0 0;color:var(--fg-subtle);font-size:var(--fs-0)">Nemotron and Kimi K3 were not contacted in this report. "Run full check" sends one tiny request to each and measures the latency.</p>' : '');
  }

  /* ---------------------------------------------------------------- platform */
  function renderPlatform() {
    const host = $('platform-kv');
    if (!host) return;
    const st = state().status;
    if (!st) { host.innerHTML = '<dt>status</dt><dd>' + U().esc(state().statusError || 'loading…') + '</dd>'; return; }
    const p = st.platform || {};
    const rows = [
      ['Kyora', st.version],
      ['platform', [p.kind, p.machine].filter(Boolean).join(' · ')],
      ['python', p.python],
      ['zipapp', p.frozen_zipapp ? 'yes (kyora.pyz)' : 'no (source)'],
      ['server', st.server_url],
      ['home', st.home],
      ['config', st.config_dir],
      ['role', st.hub_mode ? 'Hub (serves its code engine on the LAN)' : 'Studio']
    ];
    if (p.is_ish) rows.push(['note', 'iSH: Claude Code cannot run here; link a Kyora Hub for BUILD']);
    if (p.is_termux) rows.push(['note', 'Termux: Claude Code is best effort; a Kyora Hub is the documented path']);
    host.innerHTML = rows.filter((r) => r[1] != null && r[1] !== '').map((r) => '<dt>' + U().esc(r[0]) + '</dt><dd>' + U().esc(r[1]) + '</dd>').join('');
  }

  /* ---------------------------------------------------------------- LAN */
  async function loadLan() {
    const r = await K.api.maybe(K.api.get('/api/lan-urls'));
    lan = r.ok ? (r.data || {}) : { error: U().errText(r.error) };
    renderLan();
  }

  function renderLan() {
    const host = $('lan-root');
    if (!host) return;
    if (!lan) { host.innerHTML = '<div class="row"><span class="spinner" aria-hidden="true"></span><span>Looking for LAN addresses…</span></div>'; return; }
    if (lan.error) { host.innerHTML = '<div class="empty"><strong>LAN addresses unavailable</strong><span class="why">' + U().esc(lan.error) + '</span></div>'; return; }
    const urls = Array.isArray(lan.urls) ? lan.urls : [];
    if (!urls.length) {
      host.innerHTML = '<div class="empty"><strong>No LAN address found</strong><span class="why">This device has no non-loopback IPv4 address, so phones cannot reach it directly.</span></div>';
      return;
    }
    const note = lan.expose_ui
      ? 'This UI answers other devices only with the hub token (open the link once with <code>?token=…</code>).'
      : 'Other devices can reach these addresses, but this UI only answers them when Settings › Kyora Hub › "Expose this UI" is on (token required). Starting with <code>kyora hub</code> serves the code engine.';
    host.innerHTML = '<p class="help" style="font-size:var(--fs-1);color:var(--fg-muted)">' + note + '</p><div class="lan-list">' + urls.map((u) =>
      '<div class="row" style="align-items:flex-start;gap:var(--s-4)"><div class="qr-box"><img src="/api/qr.svg?text=' + encodeURIComponent(u) + '" alt="QR code for ' + U().esc(u) + '" width="180" height="180" loading="lazy" data-qr></div>' +
      '<div style="min-width:0"><a href="' + U().esc(u) + '" target="_blank" rel="noopener">' + U().esc(u) + '</a></div></div>').join('') + '</div>';
    Array.prototype.forEach.call(host.querySelectorAll('img[data-qr]'), (img) => img.addEventListener('error', () => {
      const box = img.parentNode;
      if (box) box.outerHTML = '<div class="notice">QR code unavailable on this server; type the address instead.</div>';
    }, { once: true }));
  }

  /* ---------------------------------------------------------------- hub link */
  function hubKeys() {
    const s = state().settings;
    return (s && s.keys && s.keys.hub) || {};
  }

  function renderHub() {
    const host = $('hub-root');
    if (!host) return;
    const st = state().status || {};
    const hk = hubKeys();
    const eng = (st.engines && st.engines.hub) || null;
    const rows = [
      ['this device', st.hub_mode ? 'is a Hub (serves its code engine)' : 'is a studio (client)'],
      ['linked hub', hk.url || 'not linked'],
      ['hub token', hk.token ? hk.token + ' (masked)' : 'not set']
    ];
    if (eng) rows.push(['hub engine', eng.available ? 'available' : (eng.reason || 'unavailable')]);
    const kv = rows.map((r) => '<dt>' + U().esc(r[0]) + '</dt><dd>' + U().esc(r[1]) + '</dd>').join('');
    const existing = $('hub-kv');
    if (existing && $('hub-form')) {
      // keep the form (and whatever the user is typing); refresh the facts only
      existing.innerHTML = kv;
      const unlink = $('btn-hub-unlink');
      if (unlink) unlink.hidden = !hk.url;
      return;
    }
    host.innerHTML = '<dl class="kv" id="hub-kv">' + kv + '</dl>' +
      '<form id="hub-form" class="stack" style="margin-top:12px;gap:var(--s-3)" autocomplete="off">' +
      '<div class="field"><label for="hub-link-input">Hub link or URL</label><input type="url" id="hub-link-input" placeholder="http://192.168.1.20:8765/#hub=…&token=…" spellcheck="false" autocapitalize="off">' +
      '<div class="help">Paste the link printed by <code>kyora hub</code> (or its QR), or a base URL plus the token below.</div></div>' +
      '<div class="field"><label for="hub-token-input">Token (only when the link has none)</label><input type="password" id="hub-token-input" autocomplete="new-password" spellcheck="false"></div>' +
      '<div class="row"><button type="submit" class="btn primary sm">Link hub</button><button type="button" class="btn danger sm" id="btn-hub-unlink"' + (hk.url ? '' : ' hidden') + '>Unlink</button></div>' +
      '<p class="help" style="margin:0;font-size:var(--fs-0);color:var(--fg-subtle)">The hub receives your project workspace and prompts for BUILD. Only link a hub you run yourself, on a trusted network.</p></form>';
  }

  function parseHubInput(text, tokenFallback) {
    text = String(text || '').trim();
    if (!text) return null;
    const i = text.indexOf('#');
    if (i >= 0) {
      let frag = text.slice(i + 1);
      const q = frag.indexOf('?');
      if (frag.charAt(0) === '/' && q >= 0) frag = frag.slice(q + 1);
      const p = new URLSearchParams(frag);
      if (p.get('hub')) return { url: p.get('hub'), token: p.get('token') || tokenFallback || '' };
    }
    return { url: text.replace(/\/+$/, ''), token: tokenFallback || '' };
  }

  async function saveHub(url, token, opts) {
    opts = opts || {};
    if (url && !/^https?:\/\/[^\s/]+/i.test(url)) { K.toast('The hub URL must start with http:// or https://', 'danger'); return false; }
    const hub = { url: url ? url.replace(/\/+$/, '') : '' };
    if (token || opts.clearToken) hub.token = token || '';
    const r = await K.api.maybe(K.api.put('/api/settings', { keys: { hub } }));
    if (!r.ok) { K.toast('Hub settings not saved: ' + U().errText(r.error), 'danger'); return false; }
    await app.loadSettings();
    app.refreshStatus();
    renderHub();
    return true;
  }

  /* Called by the router for '#hub=..&token=..' or '#/selfcheck?hub=..'. The hash is cleared
     first (the token must not linger in the address bar or history), then the user confirms. */
  async function handleHubLink(hubUrl, token) {
    if (linkBusy) return;
    linkBusy = true;
    try {
      try { window.history.replaceState(null, '', window.location.pathname + window.location.search + '#/selfcheck'); } catch (_) { /* old browsers keep the hash */ }
      const url = String(hubUrl || '').trim();
      if (!/^https?:\/\/[^\s/]+/i.test(url)) { K.toast('Ignored a hub link with an invalid URL.', 'danger'); return; }
      const ok = await K.confirm('Link this device to a Kyora Hub?',
        '<p>The link asks Kyora to send BUILD jobs (your project workspace and prompts) to:</p><p><code>' + U().esc(url) + '</code></p>' +
        '<p class="help">' + (token ? 'A hub token is included and will be stored masked.' : 'No token is included; the hub will refuse jobs until you add one.') + ' Only accept links from a hub you run yourself.</p>',
        { okText: 'Link hub', cancelText: 'Ignore' });
      if (!ok) { K.toast('Hub link ignored', 'info'); return; }
      if (await saveHub(url, token)) K.toast('Linked to the Kyora Hub at ' + url, 'ok');
    } finally {
      linkBusy = false;
    }
  }

  /* ---------------------------------------------------------------- wiring */
  function onEvent(ev) {
    if (ev.type === 'selfcheck.update' && state().view === 'selfcheck' && !running) load(true);
  }

  function init(appApi) {
    app = appApi;
    const runBtn = $('btn-selfcheck-run');
    if (runBtn) runBtn.addEventListener('click', () => runCheck({ probe_models: true }, 'probing models, engines and Tokkio…'));
    const claudeBtn = $('btn-selfcheck-claude');
    if (claudeBtn) claudeBtn.addEventListener('click', () => runCheck({ probe_models: false, probe_claude: true }, 'resolving the Claude model (one tiny claude -p turn)…'));
    const hubRoot = $('hub-root');
    if (hubRoot) {
      hubRoot.addEventListener('submit', async (e) => {
        e.preventDefault();
        const parsed = parseHubInput(($('hub-link-input') || {}).value, ($('hub-token-input') || {}).value);
        if (!parsed) { K.toast('Paste a hub link or URL first.', 'warn'); return; }
        if (await saveHub(parsed.url, parsed.token)) {
          ['hub-link-input', 'hub-token-input'].forEach((id) => { const el = $(id); if (el) el.value = ''; });
          K.toast('Hub settings saved', 'ok');
        }
      });
      hubRoot.addEventListener('click', async (e) => {
        if (!e.target.closest('#btn-hub-unlink')) return;
        const ok = await K.confirm('Unlink the Kyora Hub?', '<p>BUILD will use this device\'s own engine (or report BLOCKED when it has none).</p>', { okText: 'Unlink', tone: 'danger' });
        if (ok && await saveHub('', '', { clearToken: true })) K.toast('Hub unlinked', 'ok');
      });
    }
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('status') >= 0 || keys.indexOf('statusError') >= 0) { renderPlatform(); renderHub(); }
      if (keys.indexOf('settings') >= 0) renderHub();
      if (keys.indexOf('view') >= 0 && s.view === 'selfcheck') { load(false); loadLan(); }
    });
    render();
    renderPlatform();
    renderHub();
    renderLan();
  }

  (K.modules = K.modules || {}).selfcheck = { init, onEvent, handleHubLink, load, parseHubInput };
})();
