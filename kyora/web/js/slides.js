/* Slides: the project's version strip from GET /api/projects/{id}/slides. Every generation or
   modification is a tagged commit (slide/vN); cards show number, title, date, the measured gate
   badge, engine/model (a warning chip when an engine call of the run failed) and lineage
   connectors (branches on their own row). Actions: Preview, Stack/Branch (prefills Studio),
   Download ZIP, Deck. Import ZIP -> POST /api/projects/{id}/import. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  const COL_W = 232 + 24; // .slide-card width + .strip-row gap (var(--s-5))

  let app = null;
  let lastPid = '';
  let refreshSeq = 0;

  function U() { return K.util; }
  function state() { return K.store.state; }

  /* ---------------------------------------------------------------- data */
  async function refresh() {
    const pid = state().projectId;
    const seq = ++refreshSeq;
    if (!pid) { K.store.set({ slides: [], slidesMissing: false }); return []; }
    const r = await K.api.maybe(K.api.get('/api/projects/' + U().enc(pid) + '/slides'));
    if (seq !== refreshSeq || pid !== state().projectId) return state().slides;
    if (!r.ok) {
      // keep slides that came with GET /api/projects/{id}; just record the route is missing
      K.store.set({ slidesMissing: !!r.missing });
      if (!r.missing) K.toast('Could not load slides: ' + U().errText(r.error), 'danger');
      render();
      return state().slides;
    }
    const slides = U().unwrapList(r.data, 'slides');
    K.store.set({ slides, slidesMissing: false });
    return slides;
  }

  /* Lay slides out in rows: a slide continues its parent's row when the parent is the row's
     last card, otherwise it starts a branch row one column to the right of its parent. */
  function layout(slides) {
    const sorted = slides.slice().sort((a, b) => (a.number || 0) - (b.number || 0));
    const byNum = {};
    sorted.forEach((s) => { byNum[s.number] = s; });
    const rows = [];
    const col = {};
    const rowOf = {};
    const children = {};
    sorted.forEach((s) => {
      const p = s.parent != null && s.parent !== '' ? Number(s.parent) : null;
      if (p != null && byNum[p]) (children[p] = children[p] || []).push(s.number);
      if (p != null && byNum[p] && rowOf[p] != null) {
        const row = rows[rowOf[p]];
        if (row[row.length - 1].number === p) {
          row.push(s);
          rowOf[s.number] = rowOf[p];
          col[s.number] = col[p] + 1;
          return;
        }
      }
      rows.push([s]);
      rowOf[s.number] = rows.length - 1;
      col[s.number] = (p != null && col[p] != null) ? col[p] + 1 : 0;
    });
    return { rows, col, rowOf, byNum, children };
  }

  /* ---------------------------------------------------------------- rendering */
  function gateBadge(s) {
    const g = s.gate;
    if (!g || typeof g.pass_rate !== 'number') {
      return '<span class="badge" title="No quality gate ran for this slide (for example an imported ZIP)">no gate</span>';
    }
    const thr = typeof g.threshold === 'number' ? g.threshold : (app ? app.minPassRate() : 0.94);
    const pct = U().pctText(g.pass_rate);
    const counts = (g.passed != null && g.total != null) ? g.passed + '/' + g.total + ' checks' : '';
    if (g.ok && g.pass_rate >= thr) {
      return '<span class="badge" data-tone="ok" title="Gate passed (' + U().esc(counts) + ', threshold ' + Math.round(thr * 100) + ' %)">✓ ' + U().esc(pct) + '</span>';
    }
    return '<span class="badge" data-tone="warn" title="Not complete: ' + U().esc(counts) + ', threshold ' + Math.round(thr * 100) + ' % plus required checks">below ' + Math.round(thr * 100) + '% · ' + U().esc(pct) + '</span>';
  }

  /* A code-engine call of the slide's run failed (engine.ok false, engine.engine_errors): the gate badge
     stays the measured result, this chip says the engine reported an error. */
  function engineErrorChip(eng) {
    const errs = eng && Array.isArray(eng.engine_errors) ? eng.engine_errors.filter((f) => f && typeof f === 'object') : [];
    if (!eng || (eng.ok !== false && !errs.length)) return '';
    const lines = errs.map((f) => (f.kind || 'engine') + (f.iteration != null ? ' (iteration ' + f.iteration + ')' : '') +
      (f.backend ? ', ' + f.backend : '') + ': ' + (f.error || 'unknown error') +
      (f.tree_changed ? ' - its files were measured' : ' - no file changed'));
    const title = 'The code engine reported an error during this build' + (lines.length ? ':\n' + lines.join('\n') : '.') +
      '\nThe gate badge is what was measured on the committed files.';
    return '<span class="chip sc-engine-error" data-tone="warn" title="' + U().esc(title) + '">engine reported an error during this build</span>';
  }

  const ICONS = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5l12 7-12 7z"/></svg>',
    branch: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11 7"/></svg>',
    stack: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l9 5-9 5-9-5z"/><path d="M3 14l9 5 9-5"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
    deck: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>'
  };

  function cardHtml(s, lay, pid, current) {
    const n = s.number;
    const eng = s.engine || {};
    const engText = [eng.backend, eng.active_model].filter(Boolean).join(' · ');
    const hasKids = !!(lay.children[n] && lay.children[n].length);
    const modifyLabel = hasKids ? 'Branch from here' : 'Stack change';
    const parent = s.parent != null && s.parent !== '' ? 'from v' + s.parent : 'root';
    const base = '/api/projects/' + U().enc(pid) + '/slides/' + U().enc(n);
    return '<article class="slide-card" data-n="' + U().esc(n) + '" data-current="' + (String(current) === String(n)) + '" aria-label="Slide v' + U().esc(n) + ': ' + U().esc(s.title || '') + '">' +
      '<div class="sc-thumb"><span class="sc-ver">v' + U().esc(n) + '</span><span class="sc-gate">' + gateBadge(s) + '</span>' +
      '<span class="sc-placeholder" aria-hidden="true">v' + U().esc(n) + '</span></div>' +
      '<div class="sc-body"><div class="sc-title" title="' + U().esc(s.title || '') + '">' + U().esc(s.title || s.tag || ('Slide ' + n)) + '</div>' +
      '<div class="sc-meta">' + U().esc(U().fmtDate(s.created_at)) + (s.run_id ? ' · ' + U().esc(s.run_id) : '') + '</div>' +
      '<div class="sc-meta" title="' + U().esc(engText) + '">' + (engText ? U().esc(engText) + (eng.fallback_used ? ' (fallback)' : '') : 'engine: -') + '</div>' +
      engineErrorChip(eng) +
      '<div class="sc-meta">' + U().esc(parent) + (s.tag ? ' · ' + U().esc(s.tag) : '') + (s.sha ? ' @ ' + U().esc(String(s.sha).slice(0, 7)) : '') + '</div></div>' +
      '<div class="sc-spacer"></div>' +
      '<div class="sc-actions">' +
      '<button type="button" data-act="preview" data-n="' + U().esc(n) + '">' + ICONS.play + 'Preview</button>' +
      '<button type="button" data-act="modify" data-n="' + U().esc(n) + '" title="' + (hasKids ? 'Start a modification on v' + n + ' (creates a branch)' : 'Stack a modification on v' + n) + '">' + (hasKids ? ICONS.branch : ICONS.stack) + modifyLabel + '</button>' +
      '<a href="' + base + '/download" download>' + ICONS.download + 'ZIP</a>' +
      '<a href="/projects/' + U().enc(pid) + '/deck" target="_blank" rel="noopener">' + ICONS.deck + 'Deck</a>' +
      '</div></article>';
  }

  function emptyState() {
    const st = state();
    if (st.projectsMissing) return ['Slides need the projects API', 'This server did not load kyora/projects/routes.py; see Self-check.'];
    if (!st.projectId) return ['No project selected', 'Create or pick a project in Studio. Its slides appear here.'];
    if (st.slides.length) return null;
    if (st.slidesMissing) return ['Slides API not available', 'GET /api/projects/{id}/slides is not served by this build yet.'];
    return ['No slides yet', 'Run the pipeline in Studio or import a game ZIP. Every generation or modification becomes a tagged commit.'];
  }

  function render() {
    const strip = document.getElementById('strip');
    const empty = document.getElementById('slides-empty');
    const wrap = document.getElementById('strip-wrap');
    if (!strip) return;
    const st = state();
    const e = emptyState();
    if (e) {
      strip.innerHTML = '';
      if (wrap) wrap.hidden = true;
      if (empty) { empty.innerHTML = '<strong>' + U().esc(e[0]) + '</strong><span class="why">' + U().esc(e[1]) + '</span>'; empty.hidden = false; }
      return;
    }
    if (wrap) wrap.hidden = false;
    if (empty) empty.hidden = true;
    const lay = layout(st.slides);
    const current = st.project && st.project.current_slide;
    strip.innerHTML = lay.rows.map((row, i) => {
      const first = row[0];
      const isBranch = i > 0 && first.parent != null && first.parent !== '';
      const indent = lay.col[first.number] * COL_W;
      return '<div class="strip-row' + (isBranch ? ' branch-row' : '') + '" style="padding-left:' + indent + 'px" role="list" aria-label="' + (isBranch ? 'Branch from v' + U().esc(first.parent) : 'Lineage') + '">' +
        row.map((s) => '<div role="listitem" style="display:contents">' + cardHtml(s, lay, st.projectId, current) + '</div>').join('') + '</div>';
    }).join('');
    drawConnectors(lay);
  }

  /* SVG connectors between parent and child cards (positions measured after layout). */
  function drawConnectors(lay) {
    const strip = document.getElementById('strip');
    if (!strip || !strip.offsetWidth) return; // hidden: redrawn when the view is shown
    lay = lay || layout(state().slides);
    const old = strip.querySelector('svg.connectors');
    if (old) old.parentNode.removeChild(old);
    const cards = {};
    Array.prototype.forEach.call(strip.querySelectorAll('.slide-card'), (c) => { cards[c.getAttribute('data-n')] = c; });
    const paths = [];
    Object.keys(lay.byNum).forEach((k) => {
      const s = lay.byNum[k];
      if (s.parent == null || s.parent === '') return;
      const pc = cards[String(s.parent)];
      const cc = cards[String(s.number)];
      if (!pc || !cc) return;
      const px = pc.offsetLeft, py = pc.offsetTop, pw = pc.offsetWidth, ph = pc.offsetHeight;
      const cx = cc.offsetLeft, cy = cc.offsetTop, ch = cc.offsetHeight;
      if (lay.rowOf[s.parent] === lay.rowOf[s.number]) {
        const y = py + ph / 2;
        paths.push('<path d="M' + (px + pw) + ' ' + y + ' H' + cx + '"/><circle cx="' + (px + pw) + '" cy="' + y + '" r="3"/>');
      } else {
        const sx = px + pw / 2;
        const ty = cy + ch / 2;
        paths.push('<path class="branch" d="M' + sx + ' ' + (py + ph) + ' V' + (ty - 10) + ' Q' + sx + ' ' + ty + ' ' + (sx + 10) + ' ' + ty + ' H' + cx + '"/><circle cx="' + sx + '" cy="' + (py + ph) + '" r="3"/>');
      }
    });
    if (!paths.length) return;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'connectors');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('width', String(strip.scrollWidth));
    svg.setAttribute('height', String(strip.scrollHeight));
    svg.innerHTML = paths.join('');
    strip.insertBefore(svg, strip.firstChild);
  }

  /* ---------------------------------------------------------------- actions */
  async function importZip(file) {
    if (!file) return;
    if (!/\.zip$/i.test(file.name) && String(file.type).indexOf('zip') < 0) { K.toast('Choose a .zip file.', 'warn'); return; }
    const pid = await app.ensureProject(file.name.replace(/\.zip$/i, ''));
    if (!pid) return;
    K.toast('Importing ' + file.name + '…', 'info');
    const form = new FormData();
    form.append('file', file, file.name);
    const r = await K.api.maybe(K.api.postForm('/api/projects/' + U().enc(pid) + '/import', form));
    if (!r.ok) {
      K.toast(r.missing ? 'Import needs the projects API, which is not available on this server.' : 'Import failed: ' + U().errText(r.error), 'danger');
      return;
    }
    const slide = r.data && (r.data.slide || r.data);
    K.toast('Imported as slide' + (slide && slide.number != null ? ' v' + slide.number : ''), 'ok');
    await app.refreshProject();
    await refresh();
  }

  function onClick(e) {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const n = b.getAttribute('data-n');
    if (b.getAttribute('data-act') === 'preview') {
      app.navigate('preview', 'ref=v' + encodeURIComponent(n));
    } else if (b.getAttribute('data-act') === 'modify') {
      app.prefillStudio({ kind: 'modify', parent: Number(n), message: 'Describe the change to apply on top of slide v' + n + '.' });
    }
  }

  const refreshSoon = (function () {
    let t = null;
    return function () { clearTimeout(t); t = setTimeout(() => { app.refreshProject(); refresh(); }, 300); };
  })();

  function onEvent(ev) {
    if (ev.type !== 'slide.created' && ev.type !== 'deck.updated' && ev.type !== 'run.completed') return;
    if (ev.project_id && ev.project_id !== state().projectId) return;
    refreshSoon();
  }

  function init(appApi) {
    app = appApi;
    const strip = document.getElementById('strip');
    if (strip) strip.addEventListener('click', onClick);
    const input = document.getElementById('import-input');
    if (input) input.addEventListener('change', () => {
      const f = input.files && input.files[0];
      input.value = '';
      importZip(f);
    });
    const wrap = document.getElementById('strip-wrap');
    if (wrap && typeof window.ResizeObserver === 'function') {
      let lastW = 0;
      new window.ResizeObserver(() => {
        window.requestAnimationFrame(() => {
          const w = wrap.clientWidth;
          if (w && w !== lastW) { lastW = w; drawConnectors(); }
        });
      }).observe(wrap);
    }
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('slides') >= 0 || keys.indexOf('project') >= 0 || keys.indexOf('slidesMissing') >= 0 || keys.indexOf('projectsMissing') >= 0 || keys.indexOf('settings') >= 0) render();
      if (keys.indexOf('projectId') >= 0 && s.projectId !== lastPid) { lastPid = s.projectId; render(); }
      if (keys.indexOf('view') >= 0 && s.view === 'slides') window.requestAnimationFrame(() => drawConnectors());
    });
    render();
  }

  (K.modules = K.modules || {}).slides = { init, refresh, render, onEvent, layout, gateBadge, engineErrorChip, cardHtml };
})();
