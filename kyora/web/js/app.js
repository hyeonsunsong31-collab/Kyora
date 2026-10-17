/* Kyora app shell: hash router, shared state store, the single event stream with its
   per-run reducer, the Studio and Pipeline views, toasts and modals.
   Loaded last (defer); boots every module registered on window.Kyora.modules.
   Plain ES2017, no framework, no build step. Every number shown comes from an event or
   an API response; unknown values render as "-". */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  const api = K.api;
  const modules = (K.modules = K.modules || {});

  /* ================================================================== utilities */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function lsGet(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch (_) { return fallback; }
  }
  function lsSet(key, value) {
    try {
      if (value === undefined || value === null || value === '') window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, JSON.stringify(value));
    } catch (_) { /* storage blocked (private mode, quota): selection is simply not remembered */ }
  }
  function toDate(v) {
    if (v == null || v === '') return null;
    if (v instanceof Date) return v;
    if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v);
    const d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtClock(v) {
    const d = toDate(v);
    return d ? pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) : '--:--:--';
  }
  function fmtDate(v) {
    const d = toDate(v);
    if (!d) return '-';
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function relTime(v) {
    const d = toDate(v);
    if (!d) return '';
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 45) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return fmtDate(d);
  }
  function debounce(fn, ms) {
    let t = null;
    return function () {
      const args = arguments;
      const self = this;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(self, args), ms);
    };
  }
  function unwrapList(body, key) {
    if (Array.isArray(body)) return body;
    if (body && Array.isArray(body[key])) return body[key];
    return [];
  }
  function pctText(rate) {
    if (typeof rate !== 'number' || isNaN(rate)) return '-';
    const v = rate * 100;
    return v.toFixed(v >= 99.95 ? 0 : 1) + ' %';
  }
  function clip(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  function errText(err) {
    if (!err) return 'unknown error';
    if (err.body && typeof err.body === 'object' && err.body.error) return String(err.body.error);
    return err.message || String(err);
  }
  function enc(s) { return encodeURIComponent(String(s)); }
  /* Game code (previews, the test harness) is served by the preview listener: a separate origin on
     this host at /api/status preview_port, so it can never call this origin's API. '' until the
     status is known; relative /preview and /runner URLs then redirect there. */
  function previewOrigin() {
    const port = state.status && state.status.preview_port;
    return port ? window.location.protocol + '//' + window.location.hostname + ':' + port : '';
  }
  function previewHref(path) { return /^\//.test(path || '') ? previewOrigin() + path : path; }

  K.util = { $, $$, esc, lsGet, lsSet, toDate, fmtClock, fmtDate, relTime, debounce, unwrapList, pctText, clip, errText, enc, previewOrigin, previewHref };

  /* Judge verdict counters of a run: decisions plus where each verdict came from (rule, cache, Kimi K3). */
  function emptyJudge() { return { allowed: 0, denied: 0, unavailable: 0, rule: 0, cached: 0, kimi: 0 }; }

  /* ================================================================== store */
  const state = {
    view: 'studio',
    conn: 'idle',
    status: null,
    statusError: null,
    settings: null,
    projects: [],
    projectsMissing: false,
    projectsLoaded: false,
    projectId: '',
    project: null,
    attachments: [],
    slides: [],
    slidesMissing: false,
    runs: [],
    runsMissing: false,
    runId: '',
    run: null,
    stages: {},
    gate: null,
    judge: emptyJudge(),
    metrics: K.metrics.emptyMetrics(),
    kind: 'generate'
  };
  const subscribers = [];
  const store = {
    state,
    get() { return state; },
    /* Shallow-merge `patch` into the state and notify subscribers with the changed keys. */
    set(patch) {
      const keys = Object.keys(patch || {});
      if (!keys.length) return;
      Object.assign(state, patch);
      notify(keys);
    },
    /* fn(state, changedKeys) ; returns an unsubscribe function. */
    subscribe(fn) {
      subscribers.push(fn);
      return function () {
        const i = subscribers.indexOf(fn);
        if (i >= 0) subscribers.splice(i, 1);
      };
    }
  };
  function notify(keys) {
    subscribers.slice().forEach((fn) => {
      try { fn(state, keys); } catch (err) { console.error('store subscriber failed', err); }
    });
  }
  K.store = store;

  /* ================================================================== toasts */
  function toast(message, tone, opts) {
    opts = opts || {};
    const root = $('#toasts');
    if (!root) return null;
    const t = document.createElement('div');
    t.className = 'toast';
    if (tone) t.setAttribute('data-tone', tone);
    t.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
    const text = document.createElement('div');
    text.className = 'toast-text';
    text.textContent = String(message);
    const close = document.createElement('button');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '×';
    t.appendChild(text);
    t.appendChild(close);
    const remove = () => { if (t.parentNode) t.parentNode.removeChild(t); };
    close.addEventListener('click', remove);
    root.appendChild(t);
    while (root.children.length > 4) root.removeChild(root.firstChild);
    setTimeout(remove, opts.timeout || (tone === 'danger' ? 9000 : 5000));
    return t;
  }
  K.toast = toast;

  /* ================================================================== modal */
  let modalSeq = 0;
  /* openModal({title, body (html string), node (element), actions:[{label,value,tone,primary}], cancelValue})
     -> Promise<value>. Focus is trapped inside; Escape and backdrop clicks resolve cancelValue. */
  function openModal(opts) {
    return new Promise((resolve) => {
      const root = $('#modal-root') || document.body;
      const prevFocus = document.activeElement;
      const id = 'modal-title-' + (++modalSeq);
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      const modal = document.createElement('div');
      modal.className = 'modal';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      modal.setAttribute('aria-labelledby', id);
      modal.innerHTML = '<h2 id="' + id + '"></h2><div class="modal-body"></div><div class="modal-actions"></div>';
      modal.querySelector('h2').textContent = opts.title || '';
      const body = modal.querySelector('.modal-body');
      if (opts.body) body.innerHTML = opts.body;
      if (opts.node) body.appendChild(opts.node);
      const actions = modal.querySelector('.modal-actions');
      (opts.actions || []).forEach((a) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn' + (a.primary ? ' primary' : '') + (a.tone === 'danger' ? ' danger' : '') + (a.ghost ? ' ghost' : '');
        b.textContent = a.label;
        b.setAttribute('data-modal-value', '1');
        b.addEventListener('click', () => {
          const v = typeof a.value === 'function' ? a.value() : a.value;
          if (v === undefined) return; // validation failed inside value()
          finish(v);
        });
        actions.appendChild(b);
      });
      backdrop.appendChild(modal);
      root.appendChild(backdrop);
      const prevOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      let done = false;
      function finish(v) {
        if (done) return;
        done = true;
        document.removeEventListener('keydown', onKey, true);
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        document.body.style.overflow = prevOverflow;
        if (prevFocus && typeof prevFocus.focus === 'function') {
          try { prevFocus.focus({ preventScroll: true }); } catch (_) { prevFocus.focus(); }
        }
        resolve(v);
      }
      function focusables() {
        return $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', modal).filter((el) => !el.disabled && el.offsetParent !== null);
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); finish(opts.cancelValue); return; }
        if (e.key === 'Enter' && opts.enterValue && e.target && e.target.tagName === 'INPUT') {
          e.preventDefault();
          const v = opts.enterValue();
          if (v !== undefined) finish(v);
          return;
        }
        if (e.key === 'Tab') {
          const f = focusables();
          if (!f.length) return;
          const first = f[0];
          const last = f[f.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      }
      document.addEventListener('keydown', onKey, true);
      backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) finish(opts.cancelValue); });
      const initial = (opts.initialFocus && modal.querySelector(opts.initialFocus)) || modal.querySelector('.modal-actions .btn.primary') || focusables()[0];
      if (initial) setTimeout(() => initial.focus(), 0);
    });
  }

  /* confirm(title, bodyHtml, {okText, cancelText, tone}) -> Promise<boolean> */
  function confirmModal(title, bodyHtml, opts) {
    opts = opts || {};
    return openModal({
      title,
      body: bodyHtml,
      cancelValue: false,
      actions: [
        { label: opts.cancelText || 'Cancel', value: false, ghost: true },
        { label: opts.okText || 'Continue', value: true, primary: opts.tone !== 'danger', tone: opts.tone }
      ]
    });
  }

  /* promptText(title, label, {value, placeholder, help, okText, required}) -> Promise<string|null> */
  function promptText(title, label, opts) {
    opts = opts || {};
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const fid = 'modal-input-' + (modalSeq + 1);
    wrap.innerHTML = '<label for="' + fid + '"></label><input type="text" id="' + fid + '" autocomplete="off"><div class="help"></div><div class="error" role="alert"></div>';
    wrap.querySelector('label').textContent = label || '';
    const input = wrap.querySelector('input');
    input.value = opts.value || '';
    if (opts.placeholder) input.placeholder = opts.placeholder;
    wrap.querySelector('.help').textContent = opts.help || '';
    const errorEl = wrap.querySelector('.error');
    const read = () => {
      const v = input.value.trim();
      if (!v && opts.required !== false) { errorEl.textContent = 'Please enter a value.'; input.focus(); return undefined; }
      return v;
    };
    return openModal({
      title,
      node: wrap,
      cancelValue: null,
      enterValue: read,
      initialFocus: 'input',
      actions: [
        { label: 'Cancel', value: null, ghost: true },
        { label: opts.okText || 'OK', value: read, primary: true }
      ]
    });
  }
  K.openModal = openModal;
  K.confirm = confirmModal;
  K.promptText = promptText;

  /* ================================================================== router */
  const VIEWS = ['studio', 'pipeline', 'preview', 'slides', 'presenter', 'selfcheck', 'bench', 'settings'];
  const VIEW_TITLES = { studio: 'Studio', pipeline: 'Pipeline', preview: 'Preview', slides: 'Slides', presenter: 'Presenter', selfcheck: 'Self-check', bench: 'Bench', settings: 'Settings' };

  /* Accepts '#/view', '#/view?x=y', '#hub=..&token=..' (hub link) and '#view'. */
  function parseHash(hash) {
    let h = hash == null ? (window.location.hash || '') : hash;
    if (h.charAt(0) === '#') h = h.slice(1);
    let view = '';
    let query = '';
    if (h.charAt(0) === '/') {
      const q = h.indexOf('?');
      view = q >= 0 ? h.slice(1, q) : h.slice(1);
      query = q >= 0 ? h.slice(q + 1) : '';
    } else if (h.indexOf('=') >= 0) {
      query = h;
    } else {
      view = h;
    }
    view = (view.split('/')[0] || '').toLowerCase();
    let params;
    try { params = new URLSearchParams(query); } catch (_) { params = new URLSearchParams(''); }
    if (VIEWS.indexOf(view) < 0) view = params.has('hub') ? 'selfcheck' : 'studio';
    return { view, params };
  }

  function navigate(view, query) {
    const target = '#/' + view + (query ? '?' + query : '');
    if (window.location.hash === target) route();
    else window.location.hash = target;
  }

  function showView(view) {
    const prev = state.view;
    $$('section.view[data-view]').forEach((sec) => { sec.hidden = sec.getAttribute('data-view') !== view; });
    $$('#nav a[data-view]').forEach((a) => {
      if (a.getAttribute('data-view') === view) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    document.title = 'Kyora · ' + (VIEW_TITLES[view] || 'Studio');
    if (prev !== view) {
      try { window.scrollTo(0, 0); } catch (_) { /* old browsers */ }
    }
    store.set({ view });
    if (view === 'pipeline') schedulePipeline();
    syncStream();
  }

  function route() {
    const parsed = parseHash();
    showView(parsed.view);
    if (parsed.params.has('hub')) {
      const sc = modules.selfcheck;
      if (sc && typeof sc.handleHubLink === 'function') sc.handleHubLink(parsed.params.get('hub'), parsed.params.get('token') || '');
    }
    Object.keys(modules).forEach((name) => {
      const m = modules[name];
      if (m && typeof m.onRoute === 'function') {
        try { m.onRoute(parsed.view, parsed.params); } catch (err) { console.error('route handler failed', name, err); }
      }
    });
  }

  /* ================================================================== status + settings */
  const ENGINE_NAMES = { claude_code: 'Claude Code CLI', hub: 'Kyora Hub', anthropic_api: 'Anthropic Messages API' };
  const NO_ENGINE_HINT = 'Install Claude Code and run claude login, link a Kyora Hub, or add an Anthropic API key';

  async function refreshStatus() {
    const r = await api.maybe(api.get('/api/status'));
    if (r.ok) store.set({ status: r.data, statusError: null });
    else store.set({ statusError: errText(r.error) });
    return r.ok ? r.data : null;
  }

  async function loadSettings() {
    const r = await api.maybe(api.get('/api/settings'));
    if (r.ok) store.set({ settings: r.data });
    return r.ok ? r.data : null;
  }

  function settingValue(path, fallback) {
    const s = state.settings && state.settings.settings;
    let cur = s;
    const parts = path.split('.');
    for (let i = 0; i < parts.length; i++) {
      if (!cur || typeof cur !== 'object' || !(parts[i] in cur)) return fallback;
      cur = cur[parts[i]];
    }
    return cur;
  }
  function minPassRate() {
    const v = settingValue('quality.min_pass_rate', 0.94);
    return typeof v === 'number' ? v : 0.94;
  }

  function chipHtml(label, tone, title) {
    return '<span class="chip"' + (tone ? ' data-tone="' + tone + '"' : '') + ' title="' + esc(title || '') + '"><span class="dot" aria-hidden="true"></span>' + esc(label) + (title ? '<span class="sr-only">: ' + esc(title) + '</span>' : '') + '</span>';
  }

  function renderCapabilities() {
    const host = $('#cap-strip');
    if (!host) return;
    const st = state.status;
    if (!st) {
      host.innerHTML = state.statusError ? chipHtml('Status unavailable', 'danger', state.statusError) : '<span class="chip"><span class="spinner" aria-hidden="true"></span>Checking</span>';
      return;
    }
    const comp = st.components || {};
    const missing = st.missing || {};
    const engines = st.engines || {};
    const out = [];
    const model = (name, key) => {
      if (comp[key]) out.push(chipHtml(name + ': configured', 'info', 'Client configured. Reachability and latency are measured in Self-check, not assumed.'));
      else out.push(chipHtml(name + ': missing', 'danger', missing[key] || missing.nim_client || 'component not loaded'));
    };
    model('Nemotron-3-Ultra', 'nemotron');
    model('Kimi K3', 'kimi');
    if (comp.judge && comp.kimi) out.push(chipHtml('Judge: rules + Kimi K3', 'info', 'Write, Edit, MultiEdit, NotebookEdit and Bash calls of the code engine pass through the judge before they run: local rules allow planned files, standard game paths and read-only commands, Kimi K3 decides the rest. Read, Glob and Grep are not judged.'));
    else if (comp.judge) out.push(chipHtml('Judge: rules only', 'warn', 'Kimi K3 is not configured: only built-in rules judge Write/Edit/Bash calls; calls the rules do not settle are allowed unjudged and counted as unavailable.'));
    else out.push(chipHtml('Judge: missing', 'danger', missing.judge || 'not loaded'));
    const eng = (key, label) => {
      const e = engines[key] || {};
      if (e.available) out.push(chipHtml(label + ': ready', 'ok', e.reason || 'available'));
      else if (/not configured|not set/i.test(e.reason || '')) out.push(chipHtml(label + ': off', '', e.reason));
      else out.push(chipHtml(label + ': unavailable', key === 'claude_code' ? 'danger' : 'warn', e.reason || missing[key] || 'unavailable'));
    };
    eng('claude_code', 'Claude Code');
    eng('hub', 'Hub');
    eng('anthropic_api', 'Anthropic API');
    out.push(comp.tokkio ? chipHtml('Tokkio: connector', 'info', 'Deployment state is checked in the Presenter view.') : chipHtml('Tokkio: not loaded', '', missing.tokkio || 'connector module missing'));
    out.push(comp.run_manager ? chipHtml('Pipeline: ready', 'ok', 'Run manager loaded') : chipHtml('Pipeline: missing', 'danger', missing.run_manager || 'run manager not loaded'));
    out.push(comp.store ? chipHtml('Projects: ready', 'ok', 'Project store loaded') : chipHtml('Projects: missing', 'danger', missing.store || 'project store not loaded'));
    const routeMissing = Object.keys(missing).filter((k) => k.indexOf('routes:') === 0);
    if (routeMissing.length) out.push(chipHtml(routeMissing.length + ' API module' + (routeMissing.length > 1 ? 's' : '') + ' missing', 'warn', routeMissing.map((k) => k.slice(7) + ': ' + missing[k]).join('\n')));
    if (st.hub_mode) out.push(chipHtml('This device is a Hub', 'accent', 'Serving its code engine on the LAN'));
    host.innerHTML = out.join('');
  }

  function renderEngineBanner() {
    const host = $('#engine-banner');
    if (!host) return;
    const st = state.status;
    const set = (tone, icon, title, subHtml) => {
      host.setAttribute('data-tone', tone);
      host.innerHTML = '<div class="eb-icon" aria-hidden="true">' + icon + '</div><div><div class="eb-title">' + esc(title) + '</div><div class="eb-sub">' + subHtml + '</div></div>';
    };
    if (!st) {
      if (state.statusError) set('unknown', '?', 'Status unavailable', esc(state.statusError) + ' - the server did not answer <code>/api/status</code>.');
      return;
    }
    const sel = st.selected_engine;
    if (sel && sel.available && !sel.error) {
      const d = sel.details || {};
      const bits = [];
      if (d.version) bits.push(esc(d.version));
      if (d.logged_in === true) bits.push('logged in' + (d.auth_method ? ' (' + esc(d.auth_method) + ')' : ''));
      if (d.model) bits.push('model <code>' + esc(d.model) + '</code>' + (d.fallback_models && d.fallback_models.length ? ', fallbacks ' + d.fallback_models.map((m) => '<code>' + esc(m) + '</code>').join(', ') : ''));
      if (d.effort) bits.push('effort ' + esc(d.effort));
      if (d.url) bits.push(esc(d.url));
      if (sel.reason) bits.push(esc(sel.reason));
      set('ok', '✓', 'Code engine: ' + (ENGINE_NAMES[sel.backend] || sel.backend || 'available'), bits.join(' · ') || 'available');
      return;
    }
    const engines = st.engines || {};
    const reasons = Object.keys(engines).map((k) => '<li><b>' + esc(ENGINE_NAMES[k] || k) + ':</b> ' + esc(engines[k].reason || 'unavailable') + '</li>').join('');
    set('danger', '!', 'BLOCKED: no code engine', esc(NO_ENGINE_HINT) + '. Runs stop at BUILD and can be resumed.' + (sel && sel.error ? ' (' + esc(sel.error) + ')' : '') + (reasons ? '<ul style="margin:6px 0 0;padding-left:1.1em">' + reasons + '</ul>' : ''));
  }

  /* ================================================================== projects */
  function projectName(p) { return (p && (p.name || p.id)) || ''; }

  async function loadProjects(preferId) {
    const r = await api.maybe(api.get('/api/projects'));
    if (!r.ok) {
      store.set({ projects: [], projectsMissing: !!r.missing, projectsLoaded: true });
      if (!r.missing) toast('Could not load projects: ' + errText(r.error), 'danger');
      if (state.projectId) await selectProject('');
      else await loadRuns();
      return [];
    }
    const list = unwrapList(r.data, 'projects');
    store.set({ projects: list, projectsMissing: false, projectsLoaded: true });
    const wanted = preferId || state.projectId || lsGet('kyora.projectId', '');
    const pick = list.some((p) => p.id === wanted) ? wanted : (list[0] ? list[0].id : '');
    if (pick !== state.projectId || (pick && !state.project)) await selectProject(pick);
    else if (!state.runs.length) await loadRuns();
    return list;
  }

  async function selectProject(pid) {
    pid = pid || '';
    const changed = pid !== state.projectId;
    store.set({ projectId: pid, project: changed ? null : state.project, attachments: changed ? [] : state.attachments, slides: changed ? [] : state.slides, runs: changed ? [] : state.runs });
    lsSet('kyora.projectId', pid);
    syncStream();
    if (changed) setRun(null);
    if (!pid) {
      await loadRuns();
      return;
    }
    await Promise.all([loadProjectDetail(pid), loadRuns()]);
  }

  async function loadProjectDetail(pid) {
    pid = pid || state.projectId;
    if (!pid) return null;
    const r = await api.maybe(api.get('/api/projects/' + enc(pid)));
    if (pid !== state.projectId) return null;
    if (!r.ok) {
      if (!r.missing) toast('Could not load the project: ' + errText(r.error), 'danger');
      return null;
    }
    const body = r.data || {};
    const project = body.project || body;
    const patch = { project, attachments: Array.isArray(project.attachments) ? project.attachments : [] };
    const slides = Array.isArray(body.slides) ? body.slides : (Array.isArray(project.slides) ? project.slides : null);
    if (slides) patch.slides = slides;
    store.set(patch);
    if (modules.slides && typeof modules.slides.refresh === 'function') modules.slides.refresh();
    return project;
  }

  async function createProject(name) {
    const r = await api.maybe(api.post('/api/projects', { name }));
    if (!r.ok) {
      toast(r.missing ? 'The projects API is not available on this server (kyora/projects/routes.py did not load). See Self-check.' : 'Could not create the project: ' + errText(r.error), 'danger');
      return null;
    }
    const p = (r.data && (r.data.project || r.data)) || {};
    toast('Project "' + projectName(p) + '" created', 'ok');
    await loadProjects(p.id);
    return p.id || null;
  }

  async function newProjectFlow(suggest) {
    const name = await promptText('New project', 'Project name', { value: suggest || '', placeholder: 'e.g. Sky Runner', help: 'Each project is its own git repository; every generation or change becomes a slide.' });
    if (!name) return null;
    return createProject(name);
  }

  function suggestName(prompt) {
    const words = String(prompt || '').replace(/[^\w\s-]/g, ' ').split(/\s+/).filter(Boolean).slice(0, 4);
    return words.length ? words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ') : '';
  }

  /* Returns the selected project id, or asks for a name and creates one. */
  async function ensureProject(promptHint) {
    if (state.projectId) return state.projectId;
    if (state.projectsMissing) {
      toast('Projects are unavailable on this server (kyora/projects/routes.py did not load), so nothing can be started. See Self-check.', 'danger');
      return null;
    }
    return newProjectFlow(suggestName(promptHint));
  }

  async function deleteProject() {
    const p = state.project || state.projects.find((x) => x.id === state.projectId);
    if (!state.projectId) { toast('No project selected.', 'warn'); return; }
    const ok = await confirmModal('Delete "' + projectName(p) + '"?', '<p>This removes the project folder, its git history, slides and attachments from this device. It cannot be undone.</p><p class="help">Downloaded slide ZIPs are not affected.</p>', { okText: 'Delete project', tone: 'danger' });
    if (!ok) return;
    const r = await api.maybe(api.del('/api/projects/' + enc(state.projectId)));
    if (!r.ok) {
      toast(r.missing ? 'The projects API is not available on this server.' : 'Not deleted: ' + errText(r.error), 'danger');
      return;
    }
    toast('Project deleted', 'ok');
    lsSet('kyora.run.' + state.projectId, '');
    await loadProjects('');
  }

  function renderProjects() {
    const list = state.projects;
    const opts = list.map((p) => '<option value="' + esc(p.id) + '">' + esc(projectName(p)) + '</option>').join('');
    const studioSel = $('#studio-project');
    const topSel = $('#top-project');
    if (studioSel) {
      studioSel.innerHTML = list.length ? opts : '<option value="">' + (state.projectsMissing ? 'Projects API unavailable' : 'No projects yet') + '</option>';
      studioSel.value = state.projectId;
      studioSel.disabled = !list.length;
    }
    if (topSel) {
      topSel.innerHTML = '<option value="">' + (state.projectsMissing ? 'Projects unavailable' : 'No project') + '</option>' + opts;
      topSel.value = state.projectId;
    }
    const del = $('#btn-delete-project');
    if (del) del.disabled = !state.projectId;
    const newBtn = $('#btn-new-project');
    if (newBtn) newBtn.disabled = state.projectsMissing;
    const empty = $('#project-empty');
    if (empty) {
      if (state.projectsMissing) {
        const why = state.status && state.status.missing && (state.status.missing['routes:projects.routes'] || state.status.missing.store);
        empty.innerHTML = '<strong>Projects API not available</strong><span class="why">This server did not load <code>kyora/projects/routes.py</code>' + (why ? ' (' + esc(why) + ')' : '') + '. Projects, attachments, slides and runs need it; open Self-check for details.</span>';
        empty.hidden = false;
      } else if (state.projectsLoaded && !list.length) {
        empty.innerHTML = '<strong>No projects yet</strong><span class="why">Create one with New, or describe a game and press Run: Kyora asks for a name first.</span>';
        empty.hidden = false;
      } else {
        empty.hidden = true;
      }
    }
  }

  /* ================================================================== studio */
  function setKind(kind) {
    store.set({ kind: kind === 'modify' ? 'modify' : 'generate' });
  }

  function renderKind() {
    $$('#kind-seg button[data-kind]').forEach((b) => b.setAttribute('aria-pressed', String(b.getAttribute('data-kind') === state.kind)));
    const sel = $('#parent-slide');
    if (!sel) return;
    sel.disabled = state.kind !== 'modify' || !state.slides.length;
    if (state.kind !== 'modify') sel.value = '';
    else if (!sel.value && state.slides.length) {
      const cur = state.project && state.project.current_slide;
      const last = state.slides[state.slides.length - 1];
      sel.value = String(cur || (last && last.number) || '');
    }
  }

  function renderParentSelect() {
    const sel = $('#parent-slide');
    if (!sel) return;
    const prev = sel.value;
    const slides = state.slides.slice().sort((a, b) => (a.number || 0) - (b.number || 0));
    sel.innerHTML = '<option value="">' + (slides.length ? 'None (fresh start)' : 'No slides yet') + '</option>' +
      slides.map((s) => '<option value="' + esc(s.number) + '">v' + esc(s.number) + ' · ' + esc(clip(s.title || s.tag || '', 48)) + '</option>').join('');
    if (prev && slides.some((s) => String(s.number) === prev)) sel.value = prev;
    renderKind();
  }

  function renderImportZip() {
    const sel = $('#import-zip');
    if (!sel) return;
    const prev = sel.value;
    const zips = (modules.attachments && typeof modules.attachments.zipAttachments === 'function') ? modules.attachments.zipAttachments() : [];
    sel.innerHTML = '<option value="">' + (zips.length ? 'No import' : 'No ZIP attached') + '</option>' +
      zips.map((a) => '<option value="' + esc(a.id) + '">' + esc(clip(a.name || a.id, 48)) + (a.role === 'import' ? ' (import)' : '') + '</option>').join('');
    if (prev && zips.some((a) => a.id === prev)) sel.value = prev;
    else if (!prev) {
      const imp = zips.filter((a) => a.role === 'import');
      if (imp.length) sel.value = imp[imp.length - 1].id;
    }
    sel.disabled = !zips.length;
  }

  /* Prefill the studio for a modification/branch on `parent` (slide number). */
  function prefillStudio(opts) {
    opts = opts || {};
    setKind(opts.kind || 'modify');
    renderParentSelect();
    const sel = $('#parent-slide');
    if (sel && opts.parent != null) {
      sel.disabled = false;
      sel.value = String(opts.parent);
    }
    navigate('studio');
    const p = $('#prompt');
    if (p) setTimeout(() => { try { p.focus({ preventScroll: false }); } catch (_) { p.focus(); } }, 50);
    if (opts.message) toast(opts.message, 'info');
  }

  let runBusy = false;
  async function runPipeline() {
    if (runBusy) return;
    const promptEl = $('#prompt');
    const prompt = (promptEl && promptEl.value || '').trim();
    const importZip = ($('#import-zip') && $('#import-zip').value) || '';
    if (!prompt && !importZip) {
      toast('Describe the game or the change first.', 'warn');
      if (promptEl) promptEl.focus();
      return;
    }
    const kind = state.kind;
    const parentVal = kind === 'modify' ? (($('#parent-slide') && $('#parent-slide').value) || '') : '';
    if (kind === 'modify' && !parentVal) {
      toast('Pick the slide to modify. No slides yet? Generate first or import a game ZIP.', 'warn');
      return;
    }
    runBusy = true;
    const btn = $('#btn-run');
    const label = btn ? btn.textContent : '';
    try {
      const pid = await ensureProject(prompt);
      if (!pid) return;
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Starting'; }
      const body = {
        prompt,
        kind,
        parent_slide: parentVal ? Number(parentVal) : null,
        attachment_ids: (modules.attachments && typeof modules.attachments.selectedIds === 'function') ? modules.attachments.selectedIds() : [],
        options: { engine_pref: ($('#engine-pref') && $('#engine-pref').value) || 'auto', import_zip_attachment: importZip || null }
      };
      const r = await api.maybe(api.post('/api/projects/' + enc(pid) + '/runs', body));
      if (!r.ok) {
        toast(r.missing ? 'The pipeline API is not available on this server (kyora/pipeline/routes.py did not load). Nothing was started.' : 'Run not started: ' + errText(r.error), 'danger');
        return;
      }
      const run = (r.data && (r.data.run || r.data)) || {};
      if (run.id) {
        markStartedHere(run.id);
        upsertRun({ id: run.id, project_id: pid, state: run.state || 'created', kind, prompt, created_at: run.created_at || Date.now() / 1000 });
        await selectRun(run.id);
      }
      toast('Run started' + (run.id ? ' (' + run.id + ')' : ''), 'ok');
      navigate('pipeline');
    } finally {
      runBusy = false;
      if (btn) { btn.disabled = false; btn.textContent = label || 'Run pipeline'; }
    }
  }

  function initStudio() {
    const studioSel = $('#studio-project');
    const topSel = $('#top-project');
    if (studioSel) studioSel.addEventListener('change', () => selectProject(studioSel.value));
    if (topSel) topSel.addEventListener('change', () => selectProject(topSel.value));
    const newBtn = $('#btn-new-project');
    if (newBtn) newBtn.addEventListener('click', () => newProjectFlow(''));
    const delBtn = $('#btn-delete-project');
    if (delBtn) delBtn.addEventListener('click', deleteProject);
    $$('#kind-seg button[data-kind]').forEach((b) => b.addEventListener('click', () => setKind(b.getAttribute('data-kind'))));
    const runBtn = $('#btn-run');
    if (runBtn) runBtn.addEventListener('click', runPipeline);
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && state.view === 'studio') {
        e.preventDefault();
        runPipeline();
      }
    });
    const recent = $('#recent-runs');
    if (recent) recent.addEventListener('click', (e) => {
      const b = e.target.closest('[data-run]');
      if (!b) return;
      selectRun(b.getAttribute('data-run')).then(() => navigate('pipeline'));
    });
  }

  /* ================================================================== runs list */
  const ACTIVE_STATES = ['created', 'intake', 'plan', 'awaiting_approval', 'design', 'build', 'test', 'gate', 'review', 'fix', 'finalize'];
  const STATE_LABELS = { created: 'Created', intake: 'Intake', plan: 'Planning', awaiting_approval: 'Awaiting approval', design: 'Design', build: 'Building', test: 'Testing', gate: 'Gate', review: 'Review', fix: 'Fixing', finalize: 'Finalizing', complete: 'Complete', failed_gate: 'Below gate', blocked: 'BLOCKED', failed: 'Failed', cancelled: 'Cancelled' };
  function stateTone(st) {
    if (st === 'complete') return 'ok';
    if (st === 'failed' || st === 'cancelled') return 'danger';
    if (st === 'blocked' || st === 'failed_gate' || st === 'awaiting_approval') return 'warn';
    if (ACTIVE_STATES.indexOf(st) >= 0) return 'accent';
    return '';
  }
  function runTs(r) {
    const d = toDate(r && (r.created_at || r.created_ts));
    return d ? d.getTime() : 0;
  }

  function upsertRun(summary) {
    if (!summary || !summary.id) return;
    const runs = state.runs.slice();
    const i = runs.findIndex((r) => r.id === summary.id);
    if (i >= 0) runs[i] = Object.assign({}, runs[i], summary);
    else runs.push(summary);
    runs.sort((a, b) => runTs(b) - runTs(a));
    store.set({ runs });
  }

  /* Runs created by any device or the CLI are also discoverable from the persisted event log,
     which keeps the pipeline view useful when /api/runs is not available. */
  async function discoverRunsFromEvents(pid) {
    const head = await api.maybe(api.get('/api/events.json', { since: 2147483647, limit: 1 }));
    const last = head.ok && head.data ? Number(head.data.last_id || 0) : 0;
    const r = await api.maybe(api.get('/api/events.json', { types: 'run.created,run.state', project: pid || undefined, since: Math.max(0, last - 20000), limit: 2000 }));
    if (!r.ok) return [];
    const map = {};
    ((r.data && r.data.events) || []).forEach((ev) => {
      if (!ev.run_id) return;
      const d = ev.data || {};
      const cur = map[ev.run_id] || (map[ev.run_id] = { id: ev.run_id, project_id: ev.project_id, state: 'created', created_at: ev.ts });
      if (ev.type === 'run.created') { cur.created_at = ev.ts; if (d.kind) cur.kind = d.kind; if (d.prompt) cur.prompt = d.prompt; }
      if (ev.type === 'run.state' && d.state) cur.state = d.state;
    });
    return Object.keys(map).map((k) => map[k]);
  }

  async function loadRuns() {
    const pid = state.projectId;
    const r = await api.maybe(api.get('/api/runs', { project: pid || undefined }));
    if (pid !== state.projectId) return;
    let runs = [];
    let missing = false;
    if (r.ok) runs = unwrapList(r.data, 'runs');
    else missing = !!r.missing;
    if (pid) runs = runs.filter((x) => !x.project_id || x.project_id === pid);
    const discovered = await discoverRunsFromEvents(pid);
    if (pid !== state.projectId) return;
    discovered.forEach((d) => { if (!runs.some((x) => x.id === d.id)) runs.push(d); });
    runs.sort((a, b) => runTs(b) - runTs(a));
    store.set({ runs, runsMissing: missing });
    if (!state.runId) {
      const remembered = lsGet('kyora.run.' + (pid || '_'), '');
      const pick = runs.some((x) => x.id === remembered) ? remembered : (runs[0] ? runs[0].id : '');
      if (pick) await selectRun(pick);
      else schedulePipeline();
    }
  }

  function runTitle(r) {
    return clip((r.prompt || '').replace(/\s+/g, ' ').trim() || r.title || r.id, 80);
  }

  function renderRecentRuns() {
    const host = $('#recent-runs');
    if (!host) return;
    const runs = state.runs.slice(0, 8);
    if (!runs.length) {
      const why = state.runsMissing
        ? 'The runs API is not available on this server (kyora/pipeline/routes.py did not load); runs started elsewhere still appear from the event log.'
        : (state.projectId ? 'Runs of this project appear here as soon as the pipeline publishes events.' : 'Select or create a project; its runs appear here.');
      host.innerHTML = '<div class="empty"><strong>No runs yet</strong><span class="why">' + esc(why) + '</span></div>';
      return;
    }
    host.innerHTML = runs.map((r) => {
      const st = r.state || 'created';
      return '<button type="button" class="run-item" data-run="' + esc(r.id) + '"' + (r.id === state.runId ? ' aria-current="true"' : '') + '>' +
        '<span class="badge" data-tone="' + stateTone(st) + '">' + esc(STATE_LABELS[st] || st) + '</span>' +
        '<span style="min-width:0"><span class="ri-title" style="display:block">' + esc(runTitle(r)) + '</span>' +
        '<span class="ri-sub">' + esc(r.id) + (r.kind ? ' · ' + esc(r.kind) : '') + (r.created_at ? ' · ' + esc(relTime(r.created_at)) : '') + '</span></span>' +
        '<span aria-hidden="true" style="color:var(--fg-subtle)">›</span></button>';
    }).join('');
  }

  function renderRunSelect() {
    const sel = $('#run-select');
    if (!sel) return;
    const runs = state.runs.slice();
    if (state.runId && !runs.some((r) => r.id === state.runId)) runs.unshift({ id: state.runId, state: state.run && state.run.state });
    sel.innerHTML = '<option value="">' + (runs.length ? 'Select a run' : 'No run selected') + '</option>' + runs.map((r) =>
      '<option value="' + esc(r.id) + '">' + esc(r.id) + ' · ' + esc(STATE_LABELS[r.state] || r.state || '?') + (r.created_at ? ' · ' + esc(relTime(r.created_at)) : '') + '</option>').join('');
    sel.value = state.runId || '';
  }

  /* "Started on this device" marks which device auto-runs browser tests. */
  function startedRuns() { const v = lsGet('kyora.startedRuns', []); return Array.isArray(v) ? v : []; }
  function markStartedHere(runId) {
    const list = startedRuns().filter((x) => x !== runId);
    list.push(runId);
    lsSet('kyora.startedRuns', list.slice(-50));
  }
  function startedHere(runId) { return startedRuns().indexOf(runId) >= 0; }

  /* ================================================================== run reducer */
  const STAGE_IDS = (K.PIPELINE_STAGES && K.PIPELINE_STAGES.length) ? K.PIPELINE_STAGES.slice() : ['intake', 'plan', 'design', 'build', 'test', 'gate', 'finalize', 'review', 'fix'];

  function newRunCtx(id, projectId) {
    return {
      id, projectId: projectId || '', state: '', iteration: null, kind: '', prompt: '',
      stages: {}, gate: null, threshold: null, judge: emptyJudge(),
      metrics: K.metrics.emptyMetrics(), engine: {}, log: [], lastId: 0, loading: true, pending: [],
      awaitBrowser: null, browserSummary: null, blockedReason: '', error: '', review: null, slide: null,
      detail: null, firstTs: null, lastTs: null, delta: null
    };
  }

  function setStage(ctx, name, patch) {
    if (!name) return;
    ctx.stages[name] = Object.assign({ status: 'idle' }, ctx.stages[name] || {}, patch);
  }
  function runningStage(ctx) {
    const ids = Object.keys(ctx.stages);
    for (let i = 0; i < ids.length; i++) if (ctx.stages[ids[i]].status === 'running') return ids[i];
    return STAGE_IDS.indexOf(ctx.state) >= 0 ? ctx.state : null;
  }
  /* Close every running stage except `keep` (the pipeline is sequential). */
  function settleRunning(ctx, status, message, keep) {
    Object.keys(ctx.stages).forEach((id) => {
      if (id === keep) return;
      const s = ctx.stages[id];
      if (s.status !== 'running') return;
      let st = status;
      if (st === 'done' && id === 'gate' && ctx.gate && ctx.gate.ok === false) st = 'failed';
      ctx.stages[id] = Object.assign({}, s, { status: st, pct: st === 'done' ? 100 : s.pct, message: message || s.message });
    });
  }
  function applyTerminal(ctx, st, d) {
    if (st === 'complete') {
      settleRunning(ctx, 'done');
      if (!ctx.stages.finalize || ctx.stages.finalize.status === 'idle') setStage(ctx, 'finalize', { status: 'done', pct: 100 });
    } else if (st === 'failed_gate') {
      settleRunning(ctx, 'done');
      if (!ctx.stages.gate || ctx.stages.gate.status !== 'failed') setStage(ctx, 'gate', { status: 'failed', pct: 100, message: 'below threshold' });
    } else if (st === 'blocked') {
      const s = (d && d.stage) || runningStage(ctx);
      if (s) setStage(ctx, s, { status: 'blocked', message: (d && d.reason) || ctx.blockedReason || 'blocked' });
    } else if (st === 'failed') {
      settleRunning(ctx, 'failed', (d && d.error) || 'failed');
    } else if (st === 'cancelled') {
      settleRunning(ctx, 'failed', 'cancelled');
    } else if (st === 'awaiting_approval') {
      settleRunning(ctx, 'done', '', 'plan');
      setStage(ctx, 'plan', { status: 'done', pct: 100, message: 'awaiting approval' });
    }
  }

  function line(ev, kind, cat, label, text, level) {
    return { ts: ev.ts, kind, cat, label, text: clip(text == null ? '' : text, 4000), level: level || '' };
  }
  function countChecks(checks) {
    if (!Array.isArray(checks)) return '';
    const c = { pass: 0, fail: 0, skip: 0, other: 0 };
    checks.forEach((x) => { const s = x && (x.status || (x.ok === true ? 'pass' : x.ok === false ? 'fail' : '')); if (c[s] != null) c[s] += 1; else c.other += 1; });
    return c.pass + ' pass, ' + c.fail + ' fail' + (c.skip ? ', ' + c.skip + ' skipped' : '') + (c.other ? ', ' + c.other + ' other' : '');
  }
  /* test.static/test.node carry either a checks array or counts ({passed, failed, deferred, total} / {checks: n, failed}). */
  function checkSummary(d) {
    if (Array.isArray(d.checks)) return countChecks(d.checks) || 'done';
    const parts = [];
    if (d.passed != null) parts.push(d.passed + ' pass');
    if (d.failed != null) parts.push(d.failed + ' fail');
    if (d.deferred) parts.push(d.deferred + ' deferred to the browser');
    if (d.total != null) parts.push(d.total + ' total');
    else if (typeof d.checks === 'number') parts.push(d.checks + ' total');
    return parts.join(', ') || 'done';
  }
  function sysName(role, model, backend) {
    const key = K.metrics.systemFor(role, model, backend);
    const s = K.metrics.SYSTEMS.find((x) => x.key === key);
    return s ? s.name : (role || model || 'model');
  }

  /* Apply one event to a run context; returns the log lines it produced. Pure apart from ctx. */
  function applyRunEvent(ctx, ev) {
    const d = ev.data || {};
    const t = ev.type;
    const out = [];
    if (ev.id && ev.id > ctx.lastId) ctx.lastId = ev.id;
    if (ev.ts) { if (!ctx.firstTs) ctx.firstTs = ev.ts; ctx.lastTs = ev.ts; }
    if (!ctx.projectId && ev.project_id) ctx.projectId = ev.project_id;
    if (t.indexOf('model.') === 0 || t.indexOf('engine.') === 0) K.metrics.applyMetricsEvent(ctx.metrics, ev);
    switch (t) {
      case 'run.created':
        if (d.kind) ctx.kind = d.kind;
        if (d.prompt) ctx.prompt = d.prompt;
        if (!ctx.state) ctx.state = 'created';
        out.push(line(ev, 'system', 'gate', 'run', 'run created' + (d.kind ? ' (' + d.kind + ')' : '')));
        break;
      case 'run.state': {
        const st = d.state || ctx.state;
        ctx.state = st;
        if (d.iteration != null) ctx.iteration = d.iteration;
        if (STAGE_IDS.indexOf(st) >= 0) {
          settleRunning(ctx, 'done', '', st);
          const cur = ctx.stages[st];
          if (!cur || cur.status !== 'running') setStage(ctx, st, { status: 'running', pct: 0, message: '', ms: null });
        } else {
          applyTerminal(ctx, st, d);
        }
        out.push(line(ev, 'system', 'gate', 'state', (STATE_LABELS[st] || st) + (d.iteration != null ? ' · iteration ' + d.iteration : '')));
        break;
      }
      case 'run.blocked':
        ctx.state = 'blocked';
        ctx.blockedReason = d.reason || 'blocked';
        applyTerminal(ctx, 'blocked', d);
        out.push(line(ev, 'error', 'errors', 'blocked', 'BLOCKED: ' + ctx.blockedReason));
        break;
      case 'run.awaiting_approval':
        ctx.state = 'awaiting_approval';
        applyTerminal(ctx, 'awaiting_approval', d);
        out.push(line(ev, 'system', 'gate', 'plan', 'plan ready - waiting for approval'));
        break;
      case 'run.completed': {
        // published for both outcomes: {slide, state: complete|failed_gate, gate_ok, pass_rate, threshold}
        const st = d.state === 'failed_gate' || d.gate_ok === false ? 'failed_gate' : 'complete';
        ctx.state = st;
        if (d.slide != null) ctx.slide = (typeof d.slide === 'object' && d.slide) ? d.slide.number : d.slide;
        applyTerminal(ctx, st, d);
        out.push(st === 'complete'
          ? line(ev, 'system', 'gate', 'done', 'run complete' + (ctx.slide != null ? ' - slide v' + ctx.slide : ''))
          : line(ev, 'error', 'errors', 'below', 'below the gate' + (typeof d.pass_rate === 'number' ? ' (' + pctText(d.pass_rate) + ')' : '') + (ctx.slide != null ? ' - slide v' + ctx.slide + ' committed, run not complete' : '')));
        break;
      }
      case 'run.failed':
        ctx.state = 'failed';
        ctx.error = d.error || 'failed';
        applyTerminal(ctx, 'failed', d);
        out.push(line(ev, 'error', 'errors', 'failed', ctx.error));
        break;
      case 'run.cancelled':
        ctx.state = 'cancelled';
        applyTerminal(ctx, 'cancelled', d);
        out.push(line(ev, 'system', 'gate', 'cancel', 'run cancelled; working tree rolled back'));
        break;
      case 'stage.started':
        settleRunning(ctx, 'done', '', d.stage);
        setStage(ctx, d.stage, { status: 'running', pct: 0, message: d.message || '', ms: null });
        // the stage being executed is the run's state, even when no run.state accompanies it
        if (STAGE_IDS.indexOf(d.stage) >= 0 && (!ctx.state || ACTIVE_STATES.indexOf(ctx.state) >= 0)) ctx.state = d.stage;
        out.push(line(ev, 'system', 'gate', 'stage', (d.stage || '?') + ' started'));
        break;
      case 'stage.progress': {
        const cur = ctx.stages[d.stage] || {};
        const patch = { pct: d.pct != null ? d.pct : cur.pct, message: d.message != null ? d.message : cur.message };
        if (cur.status !== 'blocked') patch.status = 'running';
        setStage(ctx, d.stage, patch);
        break;
      }
      case 'stage.finished': {
        const status = d.status || (d.skipped ? 'skipped' : (d.ok === false ? 'failed' : 'done'));
        setStage(ctx, d.stage, { status, pct: 100, ms: d.ms, message: d.message || (d.ok === false ? (d.error || '') : '') });
        const took = d.ms != null ? ' in ' + K.metrics.fmtMs(d.ms) : '';
        out.push(status === 'failed'
          ? line(ev, 'error', 'errors', 'stage', (d.stage || '?') + ' failed' + took + (d.error ? ': ' + d.error : ''))
          : line(ev, 'system', 'gate', 'stage', (d.stage || '?') + ' ' + status + took));
        break;
      }
      case 'model.delta': {
        const prev = ctx.delta && ctx.delta.role === d.role ? ctx.delta.text : '';
        ctx.delta = { role: d.role, model: d.model, text: (prev + (d.content || d.reasoning || '')).slice(-600), reasoning: !d.content && !!d.reasoning };
        break;
      }
      case 'model.done':
        ctx.delta = null;
        break;
      case 'model.metrics': {
        ctx.delta = null;
        const parts = [sysName(d.role, d.model) + (d.role ? ' (' + d.role + ')' : '')];
        if (typeof d.tps === 'number') parts.push(K.metrics.fmtNum(d.tps) + ' tok/s');
        if (d.ttft_ms != null) parts.push('TTFT ' + K.metrics.fmtMs(d.ttft_ms));
        if (d.total_ms != null) parts.push('total ' + K.metrics.fmtMs(d.total_ms));
        if (d.completion_tokens != null) parts.push(d.completion_tokens + ' tokens out');
        if (d.tokens_source && d.tokens_source !== 'usage') parts.push('tokens: ' + d.tokens_source);
        out.push(line(ev, 'model', 'model', 'metrics', parts.join(' · ')));
        break;
      }
      case 'model.error':
        ctx.delta = null;
        out.push(line(ev, 'error', 'errors', 'model', sysName(d.role, d.model) + ': ' + (d.error || 'error') + (d.status ? ' (HTTP ' + d.status + ')' : '')));
        break;
      case 'engine.session':
        ctx.engine = Object.assign({}, ctx.engine, { backend: d.backend || ctx.engine.backend, model: d.model || ctx.engine.model, session_id: d.session_id });
        out.push(line(ev, 'system', 'engine', 'session', 'session ' + (d.backend || 'engine') + (d.model ? ' · ' + d.model : '') + (d.permission_mode ? ' · ' + d.permission_mode : '')));
        break;
      case 'engine.model_resolved':
        ctx.engine = Object.assign({}, ctx.engine, { active_model: d.active, requested_model: d.requested, fallback_used: !!d.fallback_used });
        out.push(line(ev, 'system', 'engine', 'model', 'active model ' + (d.active || '?') + (d.fallback_used ? ' (fallback from ' + (d.requested || '?') + ')' : '')));
        break;
      case 'engine.text':
        out.push(line(ev, 'text', 'engine', 'claude', d.text));
        break;
      case 'engine.thinking':
        out.push(line(ev, 'thinking', 'engine', 'think', d.text));
        break;
      case 'engine.tool_use':
        out.push(line(ev, 'tool', 'engine', d.name || 'tool', d.summary || ''));
        break;
      case 'engine.tool_result':
        out.push(d.is_error ? line(ev, 'error', 'errors', 'result', d.excerpt || 'tool error') : line(ev, 'result', 'engine', 'result', d.excerpt || 'ok'));
        break;
      case 'engine.stderr':
        out.push(line(ev, 'stderr', 'engine', 'stderr', d.line));
        break;
      case 'engine.error':
        out.push(line(ev, 'error', 'errors', 'engine', d.error || 'engine error'));
        break;
      case 'engine.compat_retry':
        out.push(line(ev, 'stderr', 'engine', 'retry', 'CLI compatibility retry: ' + (d.reason || '') + (d.argv_change ? ' (' + d.argv_change + ')' : '')));
        break;
      case 'engine.result':
        ctx.engine = Object.assign({}, ctx.engine, { num_turns: d.num_turns, duration_ms: d.duration_ms, cost_usd: d.cost_usd });
        out.push(line(ev, d.is_error ? 'error' : 'system', d.is_error ? 'errors' : 'engine', 'result',
          (d.subtype || 'result') + (d.num_turns != null ? ' · ' + d.num_turns + ' turns' : '') + (d.duration_ms != null ? ' · ' + K.metrics.fmtMs(d.duration_ms) : '')));
        break;
      case 'judge.verdict':
        if (d.decision === 'deny') ctx.judge.denied += 1; else ctx.judge.allowed += 1;
        if (d.source === 'rule') ctx.judge.rule += 1;
        else if (d.source === 'cache') ctx.judge.cached += 1;
        else if (d.source === 'kimi') ctx.judge.kimi += 1;
        out.push(line(ev, 'judge', 'judge', d.decision === 'deny' ? 'deny' : 'allow',
          (d.tool_name ? d.tool_name + ': ' : '') + (d.reason || '') + ' (' + (d.source || 'judge') + (d.latency_ms != null ? ', ' + K.metrics.fmtMs(d.latency_ms) : '') + ')', d.decision === 'deny' ? 'deny' : 'allow'));
        break;
      case 'judge.unavailable':
        ctx.judge.unavailable += 1;
        out.push(line(ev, 'judge', 'judge', 'judge', 'judge unavailable: ' + (d.reason || 'no reason given') + ' (tool call allowed, fail-open)', 'warn'));
        break;
      case 'test.static':
        out.push(line(ev, 'test', 'gate', 'static', 'static checks: ' + checkSummary(d)));
        break;
      case 'test.node':
        out.push(line(ev, 'test', 'gate', 'node', d.available === false ? 'node checks skipped: Node.js not available on this device' : 'node checks: ' + checkSummary(d)));
        break;
      case 'test.await_browser':
        ctx.awaitBrowser = { runner_url: d.runner_url, iteration: d.iteration, ts: ev.ts, done: false };
        out.push(line(ev, 'test', 'gate', 'browser', 'waiting for in-browser tests (iteration ' + (d.iteration != null ? d.iteration : '?') + ')'));
        break;
      case 'test.browser_result': {
        ctx.browserSummary = d.summary || null;
        if (ctx.awaitBrowser) ctx.awaitBrowser.done = true;
        const s = d.summary || {};
        out.push(line(ev, 'test', 'gate', 'browser', 'browser tests: ' + (s.passed != null ? s.passed : '?') + '/' + (s.total != null ? s.total : '?') + ' passed'));
        break;
      }
      case 'gate.result':
        ctx.gate = d;
        if (typeof d.threshold === 'number') ctx.threshold = d.threshold;
        if (ctx.awaitBrowser) ctx.awaitBrowser.done = true;
        out.push(line(ev, d.ok ? 'test' : 'error', d.ok ? 'gate' : 'errors', 'gate',
          'gate ' + (d.passed != null ? d.passed : '?') + '/' + (d.total != null ? d.total : '?') + ' = ' + pctText(d.pass_rate) +
          ' (threshold ' + pctText(d.threshold) + ') ' + (d.ok ? 'PASS' : 'FAIL') +
          (d.required_failures && d.required_failures.length ? ' · required failures: ' + d.required_failures.join(', ') : ''), d.ok ? '' : 'deny'));
        break;
      case 'fix.iteration':
        if (d.n != null) ctx.iteration = d.n;
        out.push(line(ev, 'system', 'gate', 'fix', 'fix iteration ' + (d.n != null ? d.n : '?')));
        break;
      case 'review.decision':
        ctx.review = d;
        out.push(line(ev, 'judge', 'judge', 'review', 'review: ' + (d.decision || '?') + (Array.isArray(d.reasons) && d.reasons.length ? ' - ' + d.reasons.join('; ') : '')));
        break;
      case 'slide.created':
        ctx.slide = d.number;
        out.push(line(ev, 'system', 'gate', 'slide', 'slide v' + d.number + ' created' + (d.tag ? ' (' + d.tag + (d.sha ? ' @ ' + String(d.sha).slice(0, 8) : '') + ')' : '')));
        break;
      case 'deck.updated':
        out.push(line(ev, 'system', 'gate', 'deck', 'deck updated'));
        break;
      default:
        break;
    }
    return out;
  }

  /* Seed a run context from run.json (GET /api/runs/{id}); events applied later refine it. */
  function applyRunDetail(ctx, run) {
    if (!run || typeof run !== 'object') return;
    ctx.detail = run;
    if (run.project_id) ctx.projectId = run.project_id;
    if (run.state) ctx.state = run.state;
    if (run.iteration != null) ctx.iteration = run.iteration;
    if (run.kind) ctx.kind = run.kind;
    if (run.prompt) ctx.prompt = run.prompt;
    if (run.stages && typeof run.stages === 'object') {
      Object.keys(run.stages).forEach((id) => {
        const s = run.stages[id] || {};
        const status = s.status === 'done' || s.status === 'running' || s.status === 'failed' || s.status === 'skipped' || s.status === 'blocked' ? s.status : 'idle';
        const ms = s.ms != null ? s.ms : (s.started && s.finished ? (toDate(s.finished) - toDate(s.started)) : null);
        setStage(ctx, id, { status, pct: status === 'done' ? 100 : (s.pct || 0), ms, message: s.message || '' });
      });
    }
    if (run.gate && run.gate.final) ctx.gate = Object.assign({ threshold: run.gate.threshold }, run.gate.final);
    if (run.gate && typeof run.gate.threshold === 'number') ctx.threshold = run.gate.threshold;
    if (run.judge) ctx.judgeDetail = run.judge;
    if (run.engine) ctx.engine = Object.assign({}, run.engine, ctx.engine);
    if (run.result_slide != null) ctx.slide = run.result_slide;
    if (run.error) ctx.error = run.error;
    if (run.blocked_reason) ctx.blockedReason = run.blocked_reason;
    if (run.state === 'blocked' || run.state === 'failed' || run.state === 'cancelled' || run.state === 'complete' || run.state === 'failed_gate') applyTerminal(ctx, run.state, { reason: run.blocked_reason, error: run.error });
  }

  K.runReducer = { newRunCtx, applyRunEvent, applyRunDetail, STATE_LABELS, stateTone };

  /* ================================================================== run selection */
  let selectSeq = 0;
  function setRun(ctx) {
    state.run = ctx;
    state.runId = ctx ? ctx.id : '';
    mirrorRun();
    resetLog(ctx);
    store.set({ runId: state.runId, run: ctx });
    schedulePipeline();
  }
  function mirrorRun() {
    const ctx = state.run;
    state.stages = ctx ? ctx.stages : {};
    state.gate = ctx ? ctx.gate : null;
    state.judge = ctx ? ctx.judge : emptyJudge();
    state.metrics = ctx ? ctx.metrics : K.metrics.emptyMetrics();
  }

  async function fetchRunEvents(rid) {
    let since = 0;
    let all = [];
    for (let page = 0; page < 25; page++) {
      const r = await api.maybe(api.get('/api/events.json', { run: rid, since, limit: 2000 }));
      if (!r.ok) break;
      const evs = (r.data && r.data.events) || [];
      all = all.concat(evs);
      if (evs.length < 2000) break;
      since = evs[evs.length - 1].id;
    }
    return all;
  }

  /* Select a run: seed from run.json, replay its persisted events, then apply live ones. */
  async function selectRun(rid) {
    rid = rid || '';
    const seq = ++selectSeq;
    if (!rid) { setRun(null); lsSet('kyora.run.' + (state.projectId || '_'), ''); return; }
    const known = state.runs.find((r) => r.id === rid);
    const ctx = newRunCtx(rid, (known && known.project_id) || state.projectId);
    if (known) { ctx.state = known.state || ''; ctx.kind = known.kind || ''; ctx.prompt = known.prompt || ''; }
    setRun(ctx);
    lsSet('kyora.run.' + (state.projectId || '_'), rid);
    const [detail, events] = await Promise.all([api.maybe(api.get('/api/runs/' + enc(rid))), fetchRunEvents(rid)]);
    if (seq !== selectSeq || state.run !== ctx) return;
    if (detail.ok) applyRunDetail(ctx, (detail.data && (detail.data.run || detail.data)) || {});
    const lines = [];
    events.forEach((ev) => { Array.prototype.push.apply(lines, applyRunEvent(ctx, ev)); });
    if (ctx.judgeDetail && !ctx.judge.allowed && !ctx.judge.denied && !ctx.judge.unavailable) {
      Object.keys(ctx.judge).forEach((k) => { ctx.judge[k] = Number(ctx.judgeDetail[k]) || 0; });
    }
    ctx.loading = false;
    const pending = ctx.pending.slice().sort((a, b) => (a.id || 0) - (b.id || 0));
    ctx.pending = [];
    pending.forEach((ev) => {
      if (ev.id && ev.id <= ctx.lastId) return;
      Array.prototype.push.apply(lines, applyRunEvent(ctx, ev));
    });
    appendLog(ctx, lines);
    mirrorRun();
    schedulePipeline();
    handleAwaitBrowser(ctx);
  }

  /* ================================================================== live log */
  const LOG_MAX = 2000;
  const LOG_CATS = [
    { id: 'engine', label: 'Engine' }, { id: 'judge', label: 'Judge' }, { id: 'model', label: 'Models' },
    { id: 'gate', label: 'Run & gate' }, { id: 'errors', label: 'Errors' }
  ];
  const logHidden = (function () { const v = lsGet('kyora.logHidden', {}); return v && typeof v === 'object' ? v : {}; })();
  let follow = true;

  function logNode(l) {
    const div = document.createElement('div');
    div.className = 'log-line';
    div.setAttribute('data-kind', l.kind);
    div.setAttribute('data-cat', l.cat);
    if (l.level) div.setAttribute('data-level', l.level);
    div.innerHTML = '<span class="lt">' + fmtClock(l.ts) + '</span><span class="lk">' + esc(clip(l.label, 10)) + '</span><span class="lx"></span>';
    div.lastChild.textContent = l.text;
    if (logHidden[l.cat]) div.hidden = true;
    return div;
  }

  function updateLogCount(ctx) {
    const c = $('#log-count');
    if (c) c.textContent = (ctx ? ctx.log.length : 0) + ' lines';
  }

  function resetLog(ctx) {
    const host = $('#log');
    if (!host) return;
    host.innerHTML = '';
    if (ctx && ctx.log.length) {
      const frag = document.createDocumentFragment();
      ctx.log.forEach((l) => frag.appendChild(logNode(l)));
      host.appendChild(frag);
    }
    updateLogCount(ctx);
    if (follow) host.scrollTop = host.scrollHeight;
    renderStreamTail(ctx);
  }

  function appendLog(ctx, lines) {
    if (!lines || !lines.length) return;
    Array.prototype.push.apply(ctx.log, lines);
    let drop = ctx.log.length - LOG_MAX;
    if (drop > 0) ctx.log.splice(0, drop);
    if (ctx !== state.run) return;
    const host = $('#log');
    if (!host) return;
    const frag = document.createDocumentFragment();
    lines.slice(-LOG_MAX).forEach((l) => frag.appendChild(logNode(l)));
    host.appendChild(frag);
    drop = host.children.length - LOG_MAX;
    while (drop-- > 0) host.removeChild(host.firstChild);
    updateLogCount(ctx);
    if (follow) host.scrollTop = host.scrollHeight;
  }

  function setFollow(on) {
    follow = !!on;
    const b = $('#btn-log-follow');
    if (b) { b.setAttribute('aria-pressed', String(follow)); b.textContent = follow ? 'Following' : 'Paused - follow'; }
    const host = $('#log');
    if (follow && host) host.scrollTop = host.scrollHeight;
  }

  function initLog() {
    const filters = $('#log-filters');
    if (filters) {
      filters.innerHTML = LOG_CATS.map((c) => '<button type="button" class="chip" data-cat="' + c.id + '" aria-pressed="' + String(!logHidden[c.id]) + '">' + esc(c.label) + '</button>').join('');
      filters.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-cat]');
        if (!b) return;
        const cat = b.getAttribute('data-cat');
        logHidden[cat] = !logHidden[cat];
        b.setAttribute('aria-pressed', String(!logHidden[cat]));
        lsSet('kyora.logHidden', logHidden);
        $$('#log .log-line').forEach((n) => { if (n.getAttribute('data-cat') === cat) n.hidden = !!logHidden[cat]; });
        if (follow) { const host = $('#log'); if (host) host.scrollTop = host.scrollHeight; }
      });
    }
    const fb = $('#btn-log-follow');
    if (fb) fb.addEventListener('click', () => setFollow(!follow));
    const host = $('#log');
    if (host) host.addEventListener('scroll', () => {
      const near = host.scrollHeight - host.scrollTop - host.clientHeight < 24;
      if (!near && follow) setFollow(false);
      else if (near && !follow) setFollow(true);
    }, { passive: true });
  }

  function renderStreamTail(ctx) {
    const tail = $('#stream-tail');
    if (!tail) return;
    const d = ctx && ctx.delta;
    if (!d || !d.text) { tail.hidden = true; return; }
    tail.hidden = false;
    tail.setAttribute('data-role', K.metrics.systemFor(d.role, d.model) || '');
    tail.textContent = sysName(d.role, d.model) + (d.reasoning ? ' (reasoning)' : '') + ': …' + d.text.slice(-280);
  }

  /* ================================================================== pipeline view */
  let pipeRaf = 0;
  function schedulePipeline() {
    if (pipeRaf) return;
    const run = () => { pipeRaf = 0; renderPipeline(); };
    pipeRaf = (typeof window.requestAnimationFrame === 'function' && !document.hidden) ? window.requestAnimationFrame(run) : setTimeout(run, 60);
  }

  function renderPipeline() {
    const ctx = state.run;
    mirrorRun();
    renderRunSelect();
    renderRecentRuns();
    renderRunStrip(ctx);
    renderChart(ctx);
    renderGate(ctx);
    renderJudge(ctx);
    const tiles = $('#metric-tiles');
    if (tiles) tiles.innerHTML = K.metrics.renderTiles(ctx ? ctx.metrics : K.metrics.emptyMetrics());
    renderControls(ctx);
    renderNotice(ctx);
    renderNavBadge(ctx);
    renderStreamTail(ctx);
    notify(['run']);
  }

  function renderRunStrip(ctx) {
    const strip = $('#run-strip');
    const sub = $('#pipe-sub');
    const iter = $('#iter-counter');
    if (iter) iter.textContent = 'iteration ' + (ctx && ctx.iteration != null ? ctx.iteration : '-');
    if (!strip) return;
    if (!ctx) {
      strip.innerHTML = '<span class="state">No run</span>';
      if (sub) sub.textContent = 'Live state of the selected run.';
      return;
    }
    const st = ctx.state || (ctx.loading ? '' : 'created');
    const chips = [];
    if (ctx.kind) chips.push('<span class="chip">' + esc(ctx.kind) + '</span>');
    const eng = ctx.engine || {};
    const model = eng.active_model || eng.model;
    if (eng.backend || model) chips.push('<span class="chip sys-claude"><span class="dot" aria-hidden="true"></span>' + esc((ENGINE_NAMES[eng.backend] || eng.backend || 'engine')) + (model ? ' · ' + esc(model) : '') + '</span>');
    if (eng.fallback_used) chips.push('<span class="chip" data-tone="warn" title="The requested model was not available">fallback from ' + esc(eng.requested_model || '?') + '</span>');
    if (ctx.slide != null) chips.push('<span class="chip" data-tone="accent">slide v' + esc(ctx.slide) + '</span>');
    if (ctx.loading) chips.push('<span class="chip"><span class="spinner" aria-hidden="true"></span>loading history</span>');
    strip.innerHTML = '<span class="state" data-tone="' + stateTone(st) + '">' + esc(STATE_LABELS[st] || st || '…') + '</span>' + chips.join('');
    if (sub) sub.textContent = 'Run ' + ctx.id + (ctx.prompt ? ' · ' + clip(ctx.prompt.replace(/\s+/g, ' '), 90) : '') + (ctx.firstTs ? ' · started ' + relTime(ctx.firstTs) : '');
  }

  let lastChartWidth = 0;
  function renderChart(ctx) {
    const host = $('#pipe-chart');
    if (!host) return;
    const w = host.clientWidth;
    if (!w) return; // hidden view: rendered when shown
    lastChartWidth = w;
    const stages = ctx ? ctx.stages : {};
    host.innerHTML = K.renderPipelineChart(stages, { width: w });
    const summary = STAGE_IDS.map((id) => id + ' ' + ((stages[id] && stages[id].status) || 'idle')).join(', ');
    host.setAttribute('aria-label', 'Pipeline stage chart: ' + summary);
  }

  function renderGate(ctx) {
    const host = $('#gate-gauge');
    if (!host) return;
    const g = ctx && ctx.gate;
    const thr = (g && typeof g.threshold === 'number') ? g.threshold : (ctx && typeof ctx.threshold === 'number' ? ctx.threshold : minPassRate());
    let meta;
    if (g) {
      meta = '<div><b>' + esc(g.passed != null ? g.passed : '-') + ' / ' + esc(g.total != null ? g.total : '-') + '</b> counted checks passed</div>' +
        '<div>threshold <b>' + esc(pctText(thr)) + '</b> + all required checks</div>' +
        '<div>' + (g.ok ? '<span class="badge" data-tone="ok">gate passed</span>' : '<span class="badge" data-tone="warn">below gate</span>') + '</div>' +
        (g.required_failures && g.required_failures.length ? '<div style="color:var(--danger)">required failures: ' + esc(g.required_failures.join(', ')) + '</div>' : '');
    } else {
      meta = '<div><b>No gate result yet</b></div><div>Measured after the test stage from static, Node and in-browser checks. Nothing is shown before it.</div>';
    }
    host.innerHTML = K.renderGauge(g ? g.pass_rate : null, thr) + '<div class="gauge-meta">' + meta + '</div>';
  }

  function renderJudge(ctx) {
    const host = $('#judge-counters');
    if (!host) return;
    const j = ctx ? ctx.judge : emptyJudge();
    const cell = (tone, n, key, label) => '<div class="counter" data-tone="' + tone + '" data-counter="' + key + '"><b>' + (ctx ? esc(n) : '-') + '</b><span>' + esc(label || key) + '</span></div>';
    // verdict sources: local rules and the per-run cache answer without a model call; only "Kimi K3" asked Kimi
    host.innerHTML = cell('ok', j.allowed, 'allowed') + cell('danger', j.denied, 'denied') + cell('warn', j.unavailable, 'unavailable') +
      cell('', j.rule, 'rule', 'by rule') + cell('', j.cached, 'cached') + cell('', j.kimi, 'kimi', 'by Kimi K3');
  }

  function renderControls(ctx) {
    const host = $('#pipe-controls');
    if (!host) return;
    if (!ctx) { host.innerHTML = ''; return; }
    const st = ctx.state;
    const b = [];
    if (st === 'awaiting_approval') b.push('<button type="button" class="btn primary sm" data-action="approve">Approve plan</button>');
    if (st === 'blocked') b.push('<button type="button" class="btn primary sm" data-action="resume">Resume</button>');
    if (ACTIVE_STATES.indexOf(st) >= 0 || st === 'blocked') b.push('<button type="button" class="btn danger sm" data-action="cancel">Cancel run</button>');
    if (ctx.awaitBrowser && !ctx.awaitBrowser.done && ctx.awaitBrowser.runner_url) b.push('<button type="button" class="btn sm" data-action="run-tests">Run tests on this device</button>');
    if (ctx.slide != null && ctx.projectId) b.push('<a class="btn sm" href="#/preview?ref=v' + esc(ctx.slide) + '">Preview slide v' + esc(ctx.slide) + '</a>');
    else if (ctx.projectId && st && st !== 'created') b.push('<a class="btn sm ghost" href="#/preview?ref=work">Preview work tree</a>');
    host.innerHTML = b.join('');
  }

  function renderNotice(ctx) {
    const host = $('#pipe-notice');
    if (!host) return;
    if (!ctx) {
      host.innerHTML = '<div class="empty"><strong>No run selected</strong><span class="why">Start one in Studio. Runs from any device or the CLI appear here live.</span></div>';
      return;
    }
    const st = ctx.state;
    const out = [];
    const g = ctx.gate;
    if (st === 'blocked') {
      const reason = ctx.blockedReason || 'blocked';
      let hint = '';
      if (/engine/i.test(reason)) hint = NO_ENGINE_HINT + ', then press Resume.';
      else if (/browser/i.test(reason)) hint = 'Open "Run tests on this device" here or on any phone or PC, then press Resume.';
      else hint = 'Fix the cause, then press Resume; the run re-enters at the blocked stage.';
      out.push('<div class="notice" data-tone="warn" role="status"><strong>BLOCKED:</strong> ' + esc(reason) + '<br><span>' + esc(hint) + '</span></div>');
    } else if (st === 'awaiting_approval') {
      out.push('<div class="notice" data-tone="info" role="status"><strong>Plan ready.</strong> Kimi K3 produced the plan and acceptance tests; approve to continue to design and build (Settings › Quality › require plan approval).</div>');
    } else if (st === 'failed_gate') {
      out.push('<div class="notice" data-tone="warn" role="status"><strong>Below the gate.</strong> ' + (ctx.slide != null ? 'Slide v' + esc(ctx.slide) + ' was committed but ' : 'The result ') + 'is not complete: measured ' + esc(g ? pctText(g.pass_rate) : '-') + ' after the maximum number of fix iterations.</div>');
    } else if (st === 'failed') {
      out.push('<div class="notice" data-tone="danger" role="alert"><strong>Failed:</strong> ' + esc(ctx.error || 'see the log') + '</div>');
    } else if (st === 'complete') {
      out.push('<div class="notice" data-tone="ok" role="status"><strong>Complete.</strong> ' + (ctx.slide != null ? 'Slide v' + esc(ctx.slide) + ' ' : 'The result ') + 'passed the gate at ' + esc(g ? pctText(g.pass_rate) : '-') + '.</div>');
    } else if (st === 'cancelled') {
      out.push('<div class="notice" role="status"><strong>Cancelled.</strong> The working tree was reset to the parent slide.</div>');
    }
    const ab = ctx.awaitBrowser;
    if (ab && !ab.done) {
      const auto = settingValue('quality.auto_run_browser_tests', true) !== false;
      const here = startedHere(ctx.id);
      const msg = (auto && here)
        ? 'Running the in-browser tests in a hidden frame on this device (it started the run).'
        : 'The pipeline waits for a browser to run the acceptance tests' + (auto ? ' (auto-run happens only on the device that started the run)' : ' (auto-run is off in Settings)') + '.';
      out.push('<div class="notice" data-tone="info" role="status"><strong>Waiting for in-browser tests</strong> (iteration ' + esc(ab.iteration != null ? ab.iteration : '?') + '). ' + esc(msg) + '</div>');
    }
    host.innerHTML = out.join('');
  }

  function renderNavBadge(ctx) {
    const badge = $('#nav-badge-pipeline');
    if (!badge) return;
    const st = ctx && ctx.state;
    if (!st || st === 'created' && !ctx.lastId) { badge.hidden = true; return; }
    badge.hidden = false;
    const tone = st === 'complete' ? 'ok' : (st === 'failed' || st === 'blocked' || st === 'cancelled') ? 'danger' : (st === 'failed_gate' || st === 'awaiting_approval') ? '' : 'accent';
    if (tone) badge.setAttribute('data-tone', tone); else badge.removeAttribute('data-tone');
    badge.setAttribute('title', 'Run ' + (STATE_LABELS[st] || st));
  }

  async function runAction(action) {
    const ctx = state.run;
    if (!ctx) return;
    if (action === 'run-tests') { openVisibleRunner(ctx); return; }
    if (action === 'cancel') {
      const ok = await confirmModal('Cancel run ' + ctx.id + '?', '<p>The code engine is stopped and the working tree is reset to the parent slide. Work in progress for this run is discarded.</p>', { okText: 'Cancel run', cancelText: 'Keep running', tone: 'danger' });
      if (!ok) return;
    }
    const r = await api.maybe(api.post('/api/runs/' + enc(ctx.id) + '/' + action, {}));
    if (!r.ok) {
      toast(r.missing ? 'The pipeline API is not available on this server (kyora/pipeline/routes.py).' : action + ' failed: ' + errText(r.error), 'danger');
      return;
    }
    toast({ cancel: 'Cancel requested', approve: 'Plan approved', resume: 'Run resumed' }[action] || 'Done', 'ok');
  }

  /* ---------------------------------------------------------------- browser tests from the pipeline */
  const openedRunners = {};
  function hiddenFramesHost() { return $('#hidden-frames'); }

  function handleAwaitBrowser(ctx) {
    if (!ctx || ctx.loading) return;
    const ab = ctx.awaitBrowser;
    if (!ab || ab.done || !ab.runner_url) { closeHiddenRunner(ctx.id); return; }
    const auto = settingValue('quality.auto_run_browser_tests', true) !== false;
    if (!auto || !startedHere(ctx.id)) return;
    const key = ctx.id + ':' + ab.iteration;
    if (openedRunners[key]) return;
    openedRunners[key] = true;
    const host = hiddenFramesHost();
    if (!host) return;
    const f = document.createElement('iframe');
    f.setAttribute('title', 'Kyora browser tests for ' + ctx.id + ' (hidden)');
    f.setAttribute('data-run', ctx.id);
    f.setAttribute('tabindex', '-1');
    f.src = previewHref(ab.runner_url);
    host.appendChild(f);
    host.hidden = false; // rendered off-screen (see .hidden-frames) so games get real frames
    host.classList.add('hidden-frames');
    toast('Running browser tests for ' + ctx.id + ' on this device', 'info');
  }

  function closeHiddenRunner(runId) {
    const host = hiddenFramesHost();
    if (!host) return;
    $$('iframe[data-run="' + runId + '"]', host).forEach((f) => {
      setTimeout(() => { if (f.parentNode) f.parentNode.removeChild(f); if (!host.children.length) host.hidden = true; }, 3000);
    });
  }

  function openVisibleRunner(ctx) {
    const ab = ctx && ctx.awaitBrowser;
    if (!ab || !ab.runner_url) return;
    const card = $('#test-frame-card');
    const frame = $('#test-frame');
    if (!card || !frame) return;
    frame.src = previewHref(ab.runner_url);
    card.hidden = false;
    try { card.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (_) { card.scrollIntoView(); }
  }

  function initPipeline() {
    const sel = $('#run-select');
    if (sel) sel.addEventListener('change', () => selectRun(sel.value));
    const controls = $('#pipe-controls');
    if (controls) controls.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-action]');
      if (b) runAction(b.getAttribute('data-action'));
    });
    const close = $('#btn-close-testframe');
    if (close) close.addEventListener('click', () => {
      const card = $('#test-frame-card');
      const frame = $('#test-frame');
      if (frame) frame.src = 'about:blank';
      if (card) card.hidden = true;
    });
    const host = $('#pipe-chart');
    if (host) {
      const onResize = () => {
        const w = host.clientWidth;
        if (w && Math.abs(w - lastChartWidth) > 4) renderChart(state.run);
      };
      if (typeof window.ResizeObserver === 'function') {
        const ro = new window.ResizeObserver(() => { window.requestAnimationFrame(onResize); });
        ro.observe(host);
      } else {
        window.addEventListener('resize', debounce(onResize, 120));
      }
    }
    initLog();
  }

  /* ================================================================== event stream */
  let stream = null;
  let streamSince = 0;
  const CONN_LABELS = { idle: 'idle', connecting: 'connecting', live: 'live', polling: 'polling', reconnecting: 'reconnecting', offline: 'offline', closed: 'closed' };

  function streamFilters() {
    // Bench runs create their own projects and publish bench.* without the user's project,
    // so the Bench view listens unfiltered; every other view only receives its project's events.
    return { project: state.projectId && state.view !== 'bench' ? state.projectId : '' };
  }

  function syncStream() {
    if (!stream) return;
    const f = streamFilters();
    if (stream.filters.project !== f.project) stream.restart(f);
  }

  function startStream() {
    if (stream || !K.EventStream) return;
    const f = streamFilters();
    stream = new K.EventStream({ project: f.project, since: streamSince, onEvent: onStreamEvent, onState: onConnState });
    stream.start();
    K.stream = stream;
  }

  function onConnState(s, detail) {
    const el = $('#conn');
    if (el) {
      el.setAttribute('data-state', s);
      const label = el.querySelector('.conn-label');
      if (label) label.textContent = CONN_LABELS[s] || s;
      el.setAttribute('title', 'Event stream: ' + (CONN_LABELS[s] || s) + (detail ? ' (' + detail + ')' : ''));
    }
    store.set({ conn: s });
  }

  function belongs(ev) {
    return !state.projectId || !ev.project_id || ev.project_id === state.projectId;
  }

  const reloadStatusSoon = debounce(() => { refreshStatus(); loadSettings(); }, 400);

  function onStreamEvent(ev) {
    if (!ev || !ev.type) return;
    Object.keys(modules).forEach((name) => {
      const m = modules[name];
      if (m && typeof m.onEvent === 'function') {
        try { m.onEvent(ev); } catch (err) { console.error('module event handler failed', name, ev.type, err); }
      }
    });
    if (ev.type === 'settings.changed') reloadStatusSoon();
    if (ev.run_id && belongs(ev)) trackRun(ev);
    const ctx = state.run;
    if (ctx && ev.run_id === ctx.id) {
      if (ctx.loading) { ctx.pending.push(ev); return; }
      if (ev.id && ev.id <= ctx.lastId) return;
      const lines = applyRunEvent(ctx, ev);
      appendLog(ctx, lines);
      if (ev.type === 'model.delta') { renderStreamTail(ctx); return; }
      if (ev.type === 'test.await_browser' || ev.type === 'test.browser_result' || ev.type === 'gate.result' || ev.type.indexOf('run.') === 0) handleAwaitBrowser(ctx);
      schedulePipeline();
    }
  }

  /* Keep the runs list current and follow new runs of the selected project. */
  function trackRun(ev) {
    const d = ev.data || {};
    const known = state.runs.find((r) => r.id === ev.run_id);
    if (ev.type === 'run.created' || !known) {
      if (ev.type !== 'run.created' && ev.type !== 'run.state') return;
      upsertRun({ id: ev.run_id, project_id: ev.project_id, state: ev.type === 'run.state' ? d.state : 'created', created_at: ev.ts, kind: d.kind, prompt: d.prompt });
      if (ev.type === 'run.created') {
        const cur = state.run;
        const idle = !cur || (ACTIVE_STATES.indexOf(cur.state) < 0 && cur.state !== '');
        if (startedHere(ev.run_id) || idle || !cur) { if (!cur || cur.id !== ev.run_id) selectRun(ev.run_id); }
      }
      return;
    }
    let st = null;
    if (ev.type === 'run.state') st = d.state;
    else if (ev.type === 'stage.started' && ACTIVE_STATES.indexOf(known.state || 'created') >= 0 && STAGE_IDS.indexOf(d.stage) >= 0) st = d.stage;
    else if (ev.type === 'run.completed') st = (d.state === 'failed_gate' || d.gate_ok === false) ? 'failed_gate' : 'complete';
    else if (ev.type === 'run.failed') st = 'failed';
    else if (ev.type === 'run.cancelled') st = 'cancelled';
    else if (ev.type === 'run.blocked') st = 'blocked';
    else if (ev.type === 'run.awaiting_approval') st = 'awaiting_approval';
    if (st && known.state !== st) upsertRun({ id: ev.run_id, state: st });
  }

  async function fetchLastId() {
    const r = await api.maybe(api.get('/api/events.json', { since: 2147483647, limit: 1 }));
    return r.ok && r.data ? Number(r.data.last_id || 0) : 0;
  }

  /* ================================================================== subscriptions + boot */
  function onStoreChange(s, keys) {
    const has = (k) => keys.indexOf(k) >= 0;
    if (has('status') || has('statusError')) { renderCapabilities(); renderEngineBanner(); if (state.projectsMissing) renderProjects(); }
    if (has('projects') || has('projectId') || has('projectsMissing') || has('projectsLoaded')) renderProjects();
    if (has('slides') || has('project')) renderParentSelect();
    if (has('kind')) renderKind();
    if (has('attachments') || has('project')) renderImportZip();
    if (has('runs') || has('runId')) { renderRecentRuns(); renderRunSelect(); }
    if (has('settings')) schedulePipeline();
  }

  /* Public API for the view modules. */
  K.app = {
    store, navigate, route, parseHash, refreshStatus, loadSettings, loadProjects, loadProjectDetail,
    selectProject, selectRun, ensureProject, createProject, prefillStudio, startedHere, markStartedHere,
    settingValue, minPassRate, toast, confirm: confirmModal, promptText, openModal,
    refreshProject() { return loadProjectDetail(state.projectId); }
  };

  async function boot() {
    store.subscribe(onStoreChange);
    initStudio();
    initPipeline();
    renderProjects();
    renderRecentRuns();
    renderKind();
    Object.keys(modules).forEach((name) => {
      const m = modules[name];
      if (m && typeof m.init === 'function') {
        try { m.init(K.app); } catch (err) { console.error('module init failed', name, err); }
      }
    });
    window.addEventListener('hashchange', route);
    route();
    refreshStatus();
    loadSettings();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshStatus(); schedulePipeline(); } });
    setInterval(() => { if (!document.hidden) refreshStatus(); }, 120000);
    const results = await Promise.all([fetchLastId(), loadProjects()]);
    streamSince = results[0];
    startStream();
    schedulePipeline();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
