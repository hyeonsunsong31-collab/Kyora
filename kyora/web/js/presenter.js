/* Presenter: NVIDIA Tokkio, vendored unmodified and deployed by the user. GET /api/tokkio/health
   decides: configured and its UI answered 2xx/3xx (ui.ok) -> the Tokkio web UI in an iframe;
   otherwise "Tokkio not deployed" with the reason (an HTTP error status is named) and what Tokkio
   needs. Below, the last tokkio.view / tokkio.narration events show what the avatar is
   asked to present (title, stages, buttons as inert chips, narration text). No avatar is drawn here. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  const MAX_VIEWS = 6;
  const MAX_NARRATIONS = 20;

  let health = null;
  let healthError = null;
  let checking = false;
  let lastCheck = 0;
  let frameUrl = '';
  let views = [];       // newest first: {ts, title, preview_url, screenshot_b64?, stages, buttons, run_id}
  let narrations = [];  // newest first: {ts, text, run_id}
  let cleared = false;

  function U() { return K.util; }
  function $(id) { return document.getElementById(id); }

  function uiUrl() {
    if (health && health.ui && health.ui.url) return health.ui.url;
    const s = K.store.state.settings;
    return (s && s.keys && s.keys.tokkio && s.keys.tokkio.ui_url) || '';
  }

  async function check(force) {
    if (checking) return;
    if (!force && health && Date.now() - lastCheck < 20000) { renderMain(); return; }
    checking = true;
    renderMain();
    const r = await K.api.maybe(K.api.get('/api/tokkio/health', { views: 1 }));
    checking = false;
    lastCheck = Date.now();
    if (r.ok) {
      health = r.data || {};
      healthError = null;
      if (Array.isArray(health.recent_views) && !views.length) {
        views = health.recent_views.slice(0, MAX_VIEWS).map((v) => Object.assign({ ts: v.ts }, v));
      }
    } else {
      health = null;
      healthError = r;
    }
    renderMain();
    renderFeed();
  }

  function chip(label, tone, title) {
    return '<span class="chip"' + (tone ? ' data-tone="' + tone + '"' : '') + (title ? ' title="' + U().esc(title) + '"' : '') + '><span class="dot" aria-hidden="true"></span>' + U().esc(label) + '</span>';
  }

  function statusChips(h) {
    const out = [];
    const ui = h.ui || {};
    if (ui.configured) {
      out.push(ui.reachable ? chip('UI reachable' + (ui.status ? ' · HTTP ' + ui.status : '') + (ui.latency_ms != null ? ' · ' + K.metrics.fmtMs(ui.latency_ms) : ''), ui.ok ? 'ok' : 'warn', ui.error || '')
        : chip('UI unreachable', 'danger', ui.error || ''));
    } else {
      out.push(chip('UI not configured', '', 'tokkio.ui_url is empty'));
    }
    const c = h.configurator || {};
    if (c.configured) out.push(c.reachable ? chip('Configurator ' + (c.openapi_found ? 'reachable (OpenAPI found)' : 'reachable'), c.openapi_found ? 'ok' : 'warn', c.error || '') : chip('Configurator unreachable', 'warn', c.error || ''));
    if (h.checked_at) out.push('<span class="hint" style="font-size:var(--fs-0);color:var(--fg-subtle)">checked ' + U().esc(U().relTime(h.checked_at)) + '</span>');
    return '<div class="row" style="margin-bottom:12px">' + out.join('') + '</div>';
  }

  function notDeployedHtml(reason) {
    return '<h2>Tokkio not deployed</h2>' +
      (reason ? '<div class="notice" data-tone="warn" style="margin-bottom:12px">' + reason + '</div>' : '') +
      '<p>NVIDIA Tokkio is NVIDIA\'s digital-human workflow (ACE). It runs only on an <b>NVIDIA-GPU Kubernetes cluster</b> with <b>NGC containers</b> (an NGC licence and API key are required), so Kyora cannot start it on this device.</p>' +
      '<p>Kyora ships Tokkio 5.0.0-ga <b>unmodified</b> in <code>vendor/tokkio-5.0.0-ga/</code> plus a small overlay in <code>tokkio-kyora/</code>: a frame processor that lets the avatar narrate pipeline progress (text written by Nemotron-3-Ultra) and show previews and stage tables through Tokkio\'s custom-view channel.</p>' +
      '<ol class="steps">' +
      '<li>Deploy Tokkio on your GPU cluster following <code>docs/tokkio-overlay.md</code> (in the Kyora folder): prerequisites, one-click targets and the required secrets.</li>' +
      '<li>Upload the overlay with <code>python tokkio-kyora/deploy/upload_overlay.py --apply</code> (a dry run without <code>--apply</code> shows the file map).</li>' +
      '<li>Set <b>Tokkio UI URL</b> (and optionally the ACE Configurator URL) in <a href="#/settings">Settings</a>, then press Re-check.</li>' +
      '</ol>' +
      '<p class="help" style="margin-top:12px;font-size:var(--fs-1);color:var(--fg-subtle)">Until then this panel shows, below, exactly what the overlay would send to the avatar. Nothing here imitates an avatar.</p>';
  }

  /* Main panel for a health answer: the Tokkio frame only when the UI answered with a page
     (ui.ok: a 2xx/3xx status); an HTTP error (a 502 ingress, a 404 from another service) or no
     answer means Tokkio is not deployed there. Returns {deployed, reason} (reason is HTML). */
  function mainState(h, url) {
    const ui = (h && h.ui) || {};
    if (h && h.configured && ui.ok && url) return { deployed: true, reason: '' };
    let reason = '';
    if (h && h.error) reason = U().esc(h.error);
    else if (h && h.configured && ui.reachable) {
      const answer = /^HTTP \d/.test(ui.error || '') ? ui.error : 'HTTP ' + (ui.status || '?') + (ui.error ? ' (' + ui.error + ')' : '');
      reason = 'Tokkio UI at <code>' + U().esc(url || '?') + '</code> answered <b>' + U().esc(answer) + '</b> instead of a page, so the deployment is not serving it.';
    } else if (h && h.configured) {
      reason = 'Tokkio UI is configured at <code>' + U().esc(url || '?') + '</code> but did not answer' + (ui.error ? ': ' + U().esc(ui.error) : '') + '.';
    }
    return { deployed: false, reason };
  }

  function renderMain() {
    const root = $('presenter-root');
    if (!root) return;
    let main = $('tokkio-main');
    if (!main) {
      root.innerHTML = '<div class="split"><div class="stack"><section class="card" id="tokkio-main" aria-live="polite"></section></div>' +
        '<div class="stack"><section class="card" aria-labelledby="h-tokkio-view"><h2 id="h-tokkio-view">Custom view <span class="hint">tokkio.view</span></h2><div id="tokkio-views"></div></section>' +
        '<section class="card" aria-labelledby="h-tokkio-narr"><h2 id="h-tokkio-narr">Narration <span class="hint">tokkio.narration · Nemotron-3-Ultra</span></h2><div class="narration-list" id="tokkio-narrations"></div></section></div></div>';
      main = $('tokkio-main');
      renderFeed();
    }
    if (!health) {
      if (healthError) {
        const missing = healthError.missing;
        main.innerHTML = notDeployedHtml(missing ? 'The Tokkio connector is not available on this server (kyora/models/tokkio.py did not load), so the deployment cannot be checked.' : 'Health check failed: ' + U().esc(U().errText(healthError.error)));
      } else {
        main.innerHTML = '<div class="row"><span class="spinner" aria-hidden="true"></span><span>Checking Tokkio…</span></div>';
      }
      frameUrl = '';
      return;
    }
    const url = uiUrl();
    const view = mainState(health, url);
    if (view.deployed) {
      if (frameUrl !== url || !main.querySelector('iframe.presenter-frame')) {
        frameUrl = url;
        main.innerHTML = '<h2>Tokkio <span class="hint">' + U().esc(url) + '</span></h2><div id="tokkio-status"></div>' +
          '<iframe class="presenter-frame" title="NVIDIA Tokkio digital human" allow="microphone; camera; autoplay; fullscreen" allowfullscreen referrerpolicy="no-referrer"></iframe>' +
          '<p class="help" style="margin:8px 0 0;font-size:var(--fs-0);color:var(--fg-subtle)">Served by your Tokkio deployment. If the frame stays blank, the deployment may forbid embedding: <a href="' + U().esc(url) + '" target="_blank" rel="noopener">open it in a new tab</a>.</p>';
        main.querySelector('iframe').src = url;
      }
      const st = $('tokkio-status');
      if (st) st.innerHTML = statusChips(health);
      return;
    }
    frameUrl = '';
    main.innerHTML = (health.configured || health.configurator ? statusChips(health) : '') + notDeployedHtml(view.reason);
  }

  function viewCard(v) {
    const stages = Array.isArray(v.stages) ? v.stages : [];
    const buttons = Array.isArray(v.buttons) ? v.buttons : [];
    const tone = (s) => s === 'done' ? 'ok' : s === 'failed' ? 'danger' : s === 'blocked' ? 'warn' : s === 'running' ? 'accent' : '';
    const shot = typeof v.screenshot_b64 === 'string' && v.screenshot_b64 ? (v.screenshot_b64.indexOf('data:') === 0 ? v.screenshot_b64 : 'data:image/png;base64,' + v.screenshot_b64) : '';
    return '<article class="tokkio-view" style="padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:12px">' +
      '<div class="row between"><b>' + U().esc(v.title || 'Untitled view') + '</b><span style="font-size:var(--fs-0);color:var(--fg-subtle)">' + U().esc(v.ts ? U().relTime(v.ts) : '') + (v.run_id ? ' · ' + U().esc(v.run_id) : '') + '</span></div>' +
      (shot ? '<div class="tv-shot"><img src="' + U().esc(shot) + '" alt="Screenshot sent with the view"></div>' : (v.has_screenshot ? '<p class="help" style="margin:0;font-size:var(--fs-0)">(a screenshot was sent with this view)</p>' : '')) +
      (v.preview_url ? '<div class="preview-url">' + U().esc(v.preview_url) + '</div>' : '') +
      (stages.length ? '<div class="row">' + stages.map((s) => '<span class="chip"' + (tone(s.status) ? ' data-tone="' + tone(s.status) + '"' : '') + '><span class="dot" aria-hidden="true"></span>' + U().esc((s.name || '?') + ': ' + (s.status || '?')) + '</span>').join('') + '</div>' : '') +
      (buttons.length ? '<div class="row" aria-label="Buttons the avatar would offer (inert here)">' + buttons.map((b) => '<span class="chip" aria-disabled="true" title="' + U().esc(b.id || '') + ' - inert preview of a Tokkio button">' + U().esc(b.text || b.id || 'button') + '</span>').join('') + '</div>' : '') +
      '</article>';
  }

  function renderFeed() {
    const vh = $('tokkio-views');
    const nh = $('tokkio-narrations');
    if (vh) {
      vh.innerHTML = views.length && !cleared ? views.map(viewCard).join('')
        : '<div class="empty"><strong>' + (cleared ? 'View cleared' : 'No view yet') + '</strong><span class="why">During a run the pipeline asks the avatar to show plan, gate and slide cards; they appear here as sent.</span></div>';
    }
    if (nh) {
      nh.innerHTML = narrations.length ? narrations.map((n) => '<div class="narration">' + U().esc(n.text) + '<time>' + U().esc(n.ts ? U().fmtClock(n.ts) : '') + (n.run_id ? ' · ' + U().esc(n.run_id) : '') + '</time></div>').join('')
        : '<div class="empty"><strong>No narration yet</strong><span class="why">Short narration lines written by Nemotron-3-Ultra at plan, build, gate and slide milestones.</span></div>';
    }
  }

  function onEvent(ev) {
    const d = ev.data || {};
    if (ev.type === 'tokkio.view') {
      cleared = false;
      views.unshift(Object.assign({ ts: ev.ts, run_id: ev.run_id }, d));
      views = views.slice(0, MAX_VIEWS);
      renderFeed();
    } else if (ev.type === 'tokkio.narration') {
      if (!d.text) return;
      narrations.unshift({ ts: ev.ts, text: String(d.text), run_id: ev.run_id });
      narrations = narrations.slice(0, MAX_NARRATIONS);
      renderFeed();
    } else if (ev.type === 'tokkio.clear') {
      cleared = true;
      renderFeed();
    } else if (ev.type === 'settings.changed' && K.store.state.view === 'presenter') {
      check(true);
    }
  }

  function init() {
    const btn = $('btn-tokkio-refresh');
    if (btn) btn.addEventListener('click', () => check(true));
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('view') >= 0 && s.view === 'presenter') check(false);
    });
    renderMain();
  }

  (K.modules = K.modules || {}).presenter = { init, onEvent, check, mainState };
})();
