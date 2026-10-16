/* Preview: the real game served from a slide checkout (/preview/<pid>/v<n>/) or the live work
   tree (/preview/<pid>/work/), in a phone/tablet/desktop frame, with reload, fullscreen, open in
   a new tab and "Run tests" (the in-browser harness at /runner/<pid>/<ref>). Games and the harness
   come from the preview listener (a separate origin, see K.util.previewOrigin); test results arrive
   as window messages of type 'kyora:test-result' and are accepted only from that origin. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  const SIZES = ['phone', 'tablet', 'desktop'];

  let app = null;
  let ref = '';
  let wantedRef = '';
  let loadedUrl = '';
  let lastResult = null;

  function U() { return K.util; }
  function state() { return K.store.state; }
  function $(id) { return document.getElementById(id); }

  function previewUrl(pid, r) { return U().previewHref('/preview/' + U().enc(pid) + '/' + U().enc(r) + '/index.html'); }
  function runnerUrl(pid, r) {
    const run = /^run-/.test(r) ? r.slice(4) : '';
    return U().previewHref('/runner/' + U().enc(pid) + '/' + U().enc(r) + '?run=' + encodeURIComponent(run) + '&iter=&auto=1&post=1');
  }

  function refOptions() {
    const slides = state().slides.slice().sort((a, b) => (b.number || 0) - (a.number || 0));
    const opts = [{ value: 'work', label: 'Live work tree' }];
    slides.forEach((s) => opts.push({ value: 'v' + s.number, label: 'v' + s.number + ' · ' + U().clip(s.title || s.tag || '', 40) }));
    return opts;
  }

  function defaultRef() {
    const cur = state().project && state().project.current_slide;
    if (cur != null && state().slides.some((s) => String(s.number) === String(cur))) return 'v' + cur;
    const slides = state().slides;
    if (slides.length) return 'v' + slides.reduce((m, s) => Math.max(m, s.number || 0), 0);
    return 'work';
  }

  function renderRefSelect() {
    const sel = $('preview-ref');
    if (!sel) return;
    const opts = refOptions();
    if (wantedRef && opts.some((o) => o.value === wantedRef)) { ref = wantedRef; wantedRef = ''; }
    if (!ref || !opts.some((o) => o.value === ref)) ref = defaultRef();
    sel.innerHTML = opts.map((o) => '<option value="' + U().esc(o.value) + '">' + U().esc(o.label) + '</option>').join('');
    sel.value = ref;
    sel.disabled = !state().projectId;
  }

  function setEmpty(title, why) {
    const empty = $('preview-empty');
    const stage = $('preview-stage');
    if (!empty) return;
    if (!title) { empty.hidden = true; if (stage) stage.hidden = false; return; }
    empty.innerHTML = '<strong>' + U().esc(title) + '</strong><span class="why">' + U().esc(why || '') + '</span>';
    empty.hidden = false;
    if (stage) stage.hidden = true;
  }

  /* Load the iframe only while the Preview view is visible (games are heavy). */
  function load(force) {
    const frame = $('preview-frame');
    const urlEl = $('preview-url');
    const open = $('btn-preview-open');
    const pid = state().projectId;
    const buttons = ['btn-preview-reload', 'btn-preview-full', 'btn-preview-tests'];
    buttons.forEach((id) => { const b = $(id); if (b) b.disabled = !pid; });
    if (!pid) {
      if (frame && loadedUrl) { frame.src = 'about:blank'; loadedUrl = ''; }
      if (urlEl) urlEl.textContent = '';
      if (open) { open.setAttribute('aria-disabled', 'true'); open.setAttribute('href', '#/preview'); open.removeAttribute('target'); }
      setEmpty(state().projectsMissing ? 'Preview needs the projects API' : 'No project selected',
        state().projectsMissing ? 'This server did not load kyora/projects/routes.py; see Self-check.' : 'Create or pick a project in Studio, then run the pipeline or import a game ZIP.');
      return;
    }
    const url = previewUrl(pid, ref || 'work');
    if (urlEl) urlEl.textContent = url;
    if (open) { open.removeAttribute('aria-disabled'); open.setAttribute('href', url); open.setAttribute('target', '_blank'); }
    if (state().view !== 'preview') return;
    if (!force && url === loadedUrl) return;
    setEmpty('');
    loadedUrl = url;
    if (frame) frame.src = force ? url + '?kyora_reload=' + Date.now() : url;
    checkPreview(pid, ref || 'work', url);
  }

  /* The game frame is cross-origin (preview listener), so ask this origin whether the ref has an
     index.html; nothing to preview -> explain instead of showing an error body in the frame. */
  async function checkPreview(pid, r, url) {
    const res = await K.api.maybe(K.api.get('/api/projects/' + U().enc(pid) + '/preview-check', { ref: r }));
    if (url !== loadedUrl) return; // the user picked something else meanwhile
    const missing = state().status && state().status.missing && state().status.missing['routes:projects.routes'];
    if (res.ok && res.data && res.data.ok === false) {
      setEmpty('Nothing to preview at "' + r + '"', 'The server answered: ' + (res.data.error || 'not found') + '. Run the pipeline first, or pick a slide that has an index.html.');
    } else if (!res.ok && missing) {
      setEmpty('Nothing to preview at "' + r + '"', 'The preview route is not available on this server (kyora/projects/routes.py did not load).');
    }
  }

  function setSize(size) {
    if (SIZES.indexOf(size) < 0) size = 'phone';
    const device = $('device');
    if (device) device.setAttribute('data-size', size);
    Array.prototype.forEach.call(document.querySelectorAll('#device-seg button[data-size]'), (b) => b.setAttribute('aria-pressed', String(b.getAttribute('data-size') === size)));
    U().lsSet('kyora.previewSize', size);
  }

  function fullscreen() {
    const stage = $('preview-stage');
    if (!stage) return;
    const req = stage.requestFullscreen || stage.webkitRequestFullscreen || stage.webkitRequestFullScreen;
    if (!req) {
      K.toast('This browser has no element fullscreen (iPhone Safari). Use Open to play it in its own tab.', 'info');
      return;
    }
    try {
      const p = req.call(stage);
      if (p && typeof p.catch === 'function') p.catch((err) => K.toast('Fullscreen refused: ' + (err && err.message ? err.message : err), 'warn'));
    } catch (err) {
      K.toast('Fullscreen refused: ' + (err && err.message ? err.message : err), 'warn');
    }
  }

  function runTests() {
    const pid = state().projectId;
    if (!pid) { K.toast('Select a project first.', 'warn'); return; }
    const card = $('runner-card');
    const frame = $('runner-frame');
    if (!frame) return;
    frame.src = runnerUrl(pid, ref || 'work');
    if (card) card.hidden = false;
    const hint = $('testres-hint');
    if (hint) hint.textContent = 'running on ' + (ref || 'work') + '…';
  }

  /* ---------------------------------------------------------------- results */
  function counter(tone, n, label) {
    return '<div class="counter"' + (tone ? ' data-tone="' + tone + '"' : '') + '><b>' + U().esc(n == null ? '-' : n) + '</b><span>' + label + '</span></div>';
  }

  function renderResults(msg) {
    const host = $('test-results');
    const hint = $('testres-hint');
    if (!host) return;
    const s = msg.summary || {};
    const results = Array.isArray(msg.results) ? msg.results : [];
    const syntax = Array.isArray(msg.syntax_checks) ? msg.syntax_checks : [];
    const shots = Array.isArray(msg.screenshots) ? msg.screenshots : [];
    const total = s.total != null ? s.total : results.length;
    const rate = total ? (s.passed || 0) / total : null;
    let html = '<div class="test-summary">' + counter('ok', s.passed, 'passed') + counter('danger', s.failed, 'failed') + counter('', s.skipped, 'skipped') + counter('', total, 'total') + '</div>';
    html += '<p class="help" style="margin:8px 0 0">Pass rate of these browser tests: <b>' + U().esc(U().pctText(rate)) + '</b>. The run gate also counts static and Node checks.</p>';
    if (results.length) {
      html += '<div class="test-list" role="list">' + results.map((t) => {
        const st = t.status === 'pass' || t.status === 'fail' ? t.status : 'skip';
        const mark = st === 'pass' ? '✓' : st === 'fail' ? '✗' : '–';
        return '<div class="test-row" role="listitem" data-status="' + st + '"><span class="ts-mark" aria-label="' + st + '">' + mark + '</span>' +
          '<span><b>' + U().esc(t.id || '') + '</b> ' + U().esc(t.name || '') + '</span><span class="ts-ms">' + (t.ms != null ? U().esc(t.ms) + ' ms' : '') + '</span>' +
          (t.error ? '<span class="ts-err">' + U().esc(t.error) + '</span>' : '') + '</div>';
      }).join('') + '</div>';
    }
    if (syntax.length) {
      html += '<h3 class="card-title" style="margin-top:12px">Syntax checks</h3><div class="test-list" role="list">' + syntax.map((c) => {
        const st = c.ok ? 'pass' : 'fail';
        return '<div class="test-row" role="listitem" data-status="' + st + '"><span class="ts-mark">' + (c.ok ? '✓' : '✗') + '</span><span class="mono">' + U().esc(c.file || '') + '</span><span class="ts-ms">' + U().esc(c.method || '') + '</span>' +
          (c.error ? '<span class="ts-err">' + U().esc(c.error) + '</span>' : '') + '</div>';
      }).join('') + '</div>';
    }
    const imgs = shots.filter((x) => x && typeof x.data_url === 'string' && /^data:image\/(png|jpeg|webp);base64,/.test(x.data_url));
    if (imgs.length) {
      html += '<h3 class="card-title" style="margin-top:12px">Screenshots</h3><div class="att-grid">' + imgs.map((x) =>
        '<figure class="att" style="margin:0"><img src="' + U().esc(x.data_url) + '" alt="Screenshot ' + U().esc(x.name || '') + '"><figcaption class="att-name">' + U().esc(x.name || '') + '</figcaption></figure>').join('') + '</div>';
    }
    if (msg.env) {
      const e = msg.env;
      html += '<p class="help" style="margin:8px 0 0">' + U().esc([e.viewport ? 'viewport ' + e.viewport.join('×') : '', e.touch ? 'touch' : '', e.duration_ms != null ? K.metrics.fmtMs(e.duration_ms) : '', e.ua ? U().clip(e.ua, 80) : ''].filter(Boolean).join(' · ')) + '</p>';
    }
    host.innerHTML = html;
    if (hint) hint.textContent = (msg.ref || '?') + (msg.run_id ? ' · run ' + msg.run_id : '') + ' · ' + (s.passed != null ? s.passed : '?') + '/' + (total != null ? total : '?') + ' passed · ' + U().relTime(Date.now());
  }

  function onMessage(e) {
    if (!e.origin || e.origin !== U().previewOrigin()) return; // only the harness on the preview listener
    const msg = e.data;
    if (!msg || typeof msg !== 'object' || msg.type !== 'kyora:test-result') return;
    lastResult = msg;
    renderResults(msg);
  }

  /* ---------------------------------------------------------------- wiring */
  function onRoute(view, params) {
    if (view !== 'preview') return;
    const r = params && params.get('ref');
    if (r) {
      wantedRef = r;
      renderRefSelect();
    }
    load(false);
  }

  function init(appApi) {
    app = appApi;
    setSize(U().lsGet('kyora.previewSize', 'phone'));
    const sel = $('preview-ref');
    if (sel) sel.addEventListener('change', () => { ref = sel.value; load(false); });
    Array.prototype.forEach.call(document.querySelectorAll('#device-seg button[data-size]'), (b) => b.addEventListener('click', () => setSize(b.getAttribute('data-size'))));
    const reload = $('btn-preview-reload');
    if (reload) reload.addEventListener('click', () => load(true));
    const full = $('btn-preview-full');
    if (full) full.addEventListener('click', fullscreen);
    const tests = $('btn-preview-tests');
    if (tests) tests.addEventListener('click', runTests);
    const open = $('btn-preview-open');
    if (open) open.addEventListener('click', (e) => { if (open.getAttribute('aria-disabled') === 'true') { e.preventDefault(); K.toast('Select a project first.', 'warn'); } });
    const close = $('btn-runner-close');
    if (close) close.addEventListener('click', () => {
      const card = $('runner-card');
      const frame = $('runner-frame');
      if (frame) frame.src = 'about:blank';
      if (card) card.hidden = true;
    });
    window.addEventListener('message', onMessage);
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('projectId') >= 0) { ref = ''; loadedUrl = ''; }
      if (keys.indexOf('slides') >= 0 || keys.indexOf('projectId') >= 0 || keys.indexOf('project') >= 0) { renderRefSelect(); load(false); }
      if (keys.indexOf('view') >= 0 && s.view === 'preview') load(false);
      if (keys.indexOf('projectsMissing') >= 0 || keys.indexOf('status') >= 0) load(false); // status: preview origin known
    });
    renderRefSelect();
    load(false);
  }

  (K.modules = K.modules || {}).preview = {
    init, onRoute, renderResults,
    open(r) { wantedRef = r; renderRefSelect(); app.navigate('preview', 'ref=' + encodeURIComponent(r)); },
    lastResult() { return lastResult; }
  };
})();
