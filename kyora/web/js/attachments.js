/* Attachments: drop zone + file input, client-side image previews, multipart upload to
   POST /api/projects/{id}/attachments (parts 'file', text part 'role'), the tile grid built
   from project.attachments, and the per-run selection (tap a tile to include/exclude it).
   Exposes Kyora.modules.attachments.selectedIds() and .zipAttachments() for app.js. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  const MAX_BYTES = 64 * 1024 * 1024;

  let app = null;
  let lastPid = '';
  const pending = [];            // uploads in flight: {key, name, kind, objectUrl}
  const previews = {};           // attachment id -> object URL of the local file
  let selected = {};             // attachment id -> true
  let importZips = false;

  function U() { return K.util; }
  function state() { return K.store.state; }

  function isZip(a) {
    const name = String((a && a.name) || '').toLowerCase();
    const mime = String((a && a.mime) || '').toLowerCase();
    return mime.indexOf('zip') >= 0 || /\.zip$/.test(name);
  }
  function isImage(a) {
    const mime = String((a && (a.mime || a.type)) || '').toLowerCase();
    return mime.indexOf('image/') === 0 || /\.(png|jpe?g|gif|webp|svg|bmp|avif)$/i.test(String((a && a.name) || ''));
  }
  function kindLabel(name) {
    const m = /\.([a-z0-9]{1,5})$/i.exec(String(name || ''));
    return m ? m[1].toUpperCase() : 'FILE';
  }
  function fmtSize(n) {
    if (n == null || isNaN(n)) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function list() { return Array.isArray(state().attachments) ? state().attachments : []; }

  function loadSelection(pid) {
    const v = U().lsGet('kyora.att.' + pid, {});
    selected = v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  }
  function saveSelection() {
    if (state().projectId) U().lsSet('kyora.att.' + state().projectId, selected);
  }

  /* Ids of the attachments included in the next run (only ids the project still has). */
  function selectedIds() {
    return list().filter((a) => selected[a.id]).map((a) => a.id);
  }
  function zipAttachments() { return list().filter(isZip); }

  function parseUploaded(body) {
    if (Array.isArray(body)) return body;
    if (!body || typeof body !== 'object') return [];
    if (Array.isArray(body.attachments)) return body.attachments;
    if (body.attachment) return [body.attachment];
    if (body.id) return [body];
    return [];
  }

  async function upload(files) {
    files = Array.prototype.slice.call(files || []);
    if (!files.length) return;
    const tooBig = files.filter((f) => f.size > MAX_BYTES);
    if (tooBig.length) K.toast(tooBig.map((f) => f.name).join(', ') + ': larger than 64 MB, not uploaded.', 'warn');
    files = files.filter((f) => f.size <= MAX_BYTES);
    if (!files.length) return;
    const pid = await app.ensureProject('');
    if (!pid) return;
    // one request per role: zips can be uploaded as the base game ("import")
    const groups = {};
    files.forEach((f) => {
      const role = (isZip(f) && importZips) ? 'import' : 'reference';
      (groups[role] = groups[role] || []).push(f);
    });
    const before = {};
    list().forEach((a) => { before[a.id] = true; });
    const roles = Object.keys(groups);
    for (let gi = 0; gi < roles.length; gi++) {
      const role = roles[gi];
      const group = groups[role];
      const entries = group.map((f, i) => ({
        key: 'up-' + Date.now() + '-' + gi + '-' + i, name: f.name, kind: kindLabel(f.name), file: f,
        objectUrl: isImage(f) && window.URL && URL.createObjectURL ? URL.createObjectURL(f) : ''
      }));
      Array.prototype.push.apply(pending, entries);
      render();
      const form = new FormData();
      group.forEach((f) => form.append('file', f, f.name));
      form.append('role', role);
      const r = await K.api.maybe(K.api.postForm('/api/projects/' + U().enc(pid) + '/attachments', form));
      entries.forEach((e) => { const i = pending.indexOf(e); if (i >= 0) pending.splice(i, 1); });
      if (!r.ok) {
        entries.forEach((e) => { if (e.objectUrl) URL.revokeObjectURL(e.objectUrl); });
        K.toast(r.missing ? 'Uploads need the projects API, which is not available on this server.' : 'Upload failed: ' + U().errText(r.error), 'danger');
        render();
        continue;
      }
      const added = parseUploaded(r.data).filter((a) => a && a.id && !before[a.id]);
      added.forEach((a) => {
        selected[a.id] = true;
        const match = entries.find((e) => e.name === a.name || (a.name && a.name.indexOf(e.name) >= 0));
        if (match && match.objectUrl) previews[a.id] = match.objectUrl;
      });
      entries.forEach((e) => { if (e.objectUrl && !Object.keys(previews).some((k) => previews[k] === e.objectUrl)) URL.revokeObjectURL(e.objectUrl); });
      saveSelection();
      K.toast(group.length + ' file' + (group.length > 1 ? 's' : '') + ' attached' + (role === 'import' ? ' as base game' : ''), 'ok');
    }
    await app.refreshProject();
    render();
  }

  function tileHtml(a) {
    const sel = !!selected[a.id];
    const pid = state().projectId;
    let visual;
    if (isImage(a)) {
      const src = previews[a.id] || ('/api/projects/' + U().enc(pid) + '/attachments/' + U().enc(a.id));
      visual = '<img src="' + U().esc(src) + '" alt="" loading="lazy" decoding="async" data-fallback="' + U().esc(kindLabel(a.name)) + '">';
    } else {
      visual = '<span class="att-kind">' + U().esc(isZip(a) ? 'ZIP' : kindLabel(a.name)) + '</span>';
    }
    const meta = [a.name, fmtSize(a.size), a.role].filter(Boolean).join(' · ');
    return '<div class="att" data-id="' + U().esc(a.id) + '" data-selected="' + sel + '">' + visual +
      '<span class="att-name" title="' + U().esc(meta) + '">' + (a.role === 'import' ? '⬆ ' : '') + U().esc(a.name || a.id) + '</span>' +
      '<span class="att-check" aria-hidden="true">' + (sel ? '✓' : '') + '</span>' +
      '<button class="att-hit" type="button" aria-pressed="' + sel + '" aria-label="' + U().esc((a.name || a.id) + (sel ? ': included in the next run, tap to exclude' : ': not included, tap to include')) + '"></button></div>';
  }

  function pendingHtml(p) {
    return '<div class="att" aria-busy="true">' + (p.objectUrl ? '<img src="' + U().esc(p.objectUrl) + '" alt="">' : '<span class="att-kind">' + U().esc(p.kind) + '</span>') +
      '<span class="att-name">' + U().esc(p.name) + '</span><span class="att-busy"><span class="spinner" aria-label="Uploading"></span></span></div>';
  }

  function render() {
    const grid = document.getElementById('att-grid');
    const help = document.getElementById('att-help');
    if (!grid) return;
    const items = list();
    grid.innerHTML = pending.map(pendingHtml).join('') + items.map(tileHtml).join('');
    Array.prototype.forEach.call(grid.querySelectorAll('img[data-fallback]'), (img) => {
      img.addEventListener('error', () => {
        const span = document.createElement('span');
        span.className = 'att-kind';
        span.textContent = img.getAttribute('data-fallback') || 'IMG';
        if (img.parentNode) img.parentNode.replaceChild(span, img);
      }, { once: true });
    });
    if (help) {
      const st = state();
      if (st.projectsMissing) help.textContent = 'Attachments need the projects API, which is not available on this server.';
      else if (!st.projectId) help.textContent = 'Files are stored in the selected project. Choosing files without a project asks for a project name first.';
      else if (!items.length && !pending.length) help.textContent = 'No attachments yet. Images become visual references for the Kimi role (when its answering model takes images); text and code files are quoted in the intake.';
      else {
        const n = selectedIds().length;
        help.textContent = n + ' of ' + items.length + ' attached file' + (items.length === 1 ? '' : 's') + ' included in the next run. Tap a tile to include or exclude it.';
      }
    }
  }

  function injectImportToggle() {
    const grid = document.getElementById('att-grid');
    if (!grid || document.getElementById('att-import-zip')) return;
    const label = document.createElement('label');
    label.className = 'check';
    label.style.marginTop = '8px';
    label.innerHTML = '<input type="checkbox" id="att-import-zip"><span>Upload game ZIPs as the base game (role <code>import</code>; pick it under "Import ZIP as base")</span>';
    grid.parentNode.insertBefore(label, grid);
    const box = label.querySelector('input');
    importZips = !!U().lsGet('kyora.importZips', false);
    box.checked = importZips;
    box.addEventListener('change', () => { importZips = box.checked; U().lsSet('kyora.importZips', importZips || ''); });
  }

  function init(appApi) {
    app = appApi;
    injectImportToggle();
    const input = document.getElementById('file-input');
    const drop = document.getElementById('drop');
    if (input) input.addEventListener('change', () => {
      const files = Array.prototype.slice.call(input.files || []);
      input.value = '';
      upload(files);
    });
    if (drop) {
      const on = (e) => { e.preventDefault(); drop.classList.add('over'); };
      const off = () => drop.classList.remove('over');
      drop.addEventListener('dragenter', on);
      drop.addEventListener('dragover', on);
      drop.addEventListener('dragleave', off);
      drop.addEventListener('drop', (e) => {
        e.preventDefault();
        off();
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) upload(e.dataTransfer.files);
      });
    }
    const grid = document.getElementById('att-grid');
    if (grid) grid.addEventListener('click', (e) => {
      const hit = e.target.closest('.att-hit');
      if (!hit) return;
      const tile = hit.closest('.att');
      const id = tile && tile.getAttribute('data-id');
      if (!id) return;
      if (selected[id]) delete selected[id]; else selected[id] = true;
      saveSelection();
      render();
      const again = grid.querySelector('.att[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"] .att-hit');
      if (again) again.focus();
    });
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('projectId') >= 0 && s.projectId !== lastPid) {
        lastPid = s.projectId;
        loadSelection(s.projectId);
        render();
      }
      if (keys.indexOf('attachments') >= 0 || keys.indexOf('projectsMissing') >= 0) render();
    });
    render();
  }

  (K.modules = K.modules || {}).attachments = { init, selectedIds, zipAttachments, upload, render };
})();
