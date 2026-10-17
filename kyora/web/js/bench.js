/* Kyora Bench: fixed tasks run through the real pipeline. Task list from GET /api/bench/tasks,
   a confirm modal with the cost/time warning before POST /api/bench/run {task_ids, confirm:true},
   progress from bench.progress events (plus polling as a backstop), results from
   GET /api/bench/results and the score from GET /api/bench/badge. The certified badge and the
   pass rate are shown only when the API says certified; otherwise "not certified" + reason. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});
  /* What the code engine spends per task: named from the engine the server would select
     (estimate.engine) and the configured claude_code.effort, never a fixed "max effort". */
  const ENGINE_USAGE = {
    claude_code: 'Claude subscription usage (Claude Code CLI)',
    anthropic_api: 'Anthropic API tokens on your Anthropic key',
    hub: 'code-engine usage on the linked Kyora Hub'
  };
  function benchWarning(effort, engine) {
    const usage = (engine && ENGINE_USAGE[engine]) || (engine ? 'code-engine usage (' + engine + ')' : 'code-engine usage');
    const at = typeof effort === 'string' && effort ? ' at effort ' + effort : '';
    return 'Each task runs the full pipeline: Nemotron + Kimi K3 tokens on your NVIDIA keys and ' + usage + at + '; typically 10-30 min per task.';
  }

  let tasks = null;
  let estimate = null;
  let tasksError = null;
  let results = null;
  let badge = null;
  let checked = {};
  let running = false;
  let progress = null;
  let pollTimer = null;
  let loadedOnce = false;

  function U() { return K.util; }
  function $(id) { return document.getElementById(id); }

  async function loadTasks() {
    const r = await K.api.maybe(K.api.get('/api/bench/tasks'));
    if (r.ok) {
      tasks = U().unwrapList(r.data, 'tasks');
      estimate = (r.data && r.data.estimate) || null;
      tasksError = null;
      if (r.data && typeof r.data.running === 'boolean') setRunning(r.data.running);
    } else {
      tasks = null;
      tasksError = r;
    }
    renderTasks();
  }

  async function loadScore() {
    const [res, bad] = await Promise.all([K.api.maybe(K.api.get('/api/bench/results')), K.api.maybe(K.api.get('/api/bench/badge'))]);
    results = res.ok ? res.data : { error: res };
    badge = bad.ok ? bad.data : { error: bad };
    if (res.ok && res.data && typeof res.data.running === 'boolean') setRunning(res.data.running);
    renderResults();
    renderScore();
    renderTasks();
  }

  function setRunning(on) {
    running = !!on;
    const cancel = $('btn-bench-cancel');
    const run = $('btn-bench-run');
    if (cancel) cancel.hidden = !running;
    if (run) run.disabled = running || !tasks || !tasks.length;
    if (running && !pollTimer) {
      pollTimer = setInterval(() => { if (K.store.state.view === 'bench') loadScore(); }, 10000);
    } else if (!running && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    renderProgress();
  }

  /* ---------------------------------------------------------------- rendering */
  function resultRows() {
    if (!results || results.error) return [];
    if (Array.isArray(results)) return results;
    if (Array.isArray(results.results)) return results.results;
    if (results.results && Array.isArray(results.results.results)) return results.results.results;
    if (Array.isArray(results.tasks)) return results.tasks;
    return [];
  }
  function lastByTask() {
    const m = {};
    resultRows().forEach((r) => { if (r && r.task_id) m[r.task_id] = r; });
    return m;
  }
  function gateRate(r) {
    const g = r && r.gate;
    if (!g) return null;
    const f = g.final || g;
    return typeof f.pass_rate === 'number' ? f.pass_rate : null;
  }

  function renderTasks() {
    const host = $('bench-tasks');
    if (!host) return;
    if (!tasks) {
      if (tasksError) {
        host.innerHTML = '<div class="empty"><strong>' + (tasksError.missing ? 'Bench not available' : 'Could not load bench tasks') + '</strong><span class="why">' +
          U().esc(tasksError.missing ? 'This server did not load kyora/bench (routes /api/bench/*). Nothing is measured, so no score is shown.' : U().errText(tasksError.error)) + '</span></div>';
      } else {
        host.innerHTML = '<div class="row"><span class="spinner" aria-hidden="true"></span><span>Loading tasks…</span></div>';
      }
      const run = $('btn-bench-run');
      if (run) run.disabled = true;
      return;
    }
    if (!tasks.length) { host.innerHTML = '<div class="empty"><strong>No bench tasks</strong><span class="why">The task set is empty in this build.</span></div>'; return; }
    const last = lastByTask();
    host.innerHTML = tasks.map((t) => {
      const r = last[t.id];
      let mark = '';
      if (r) {
        const rate = gateRate(r);
        mark = r.passed ? '<span class="badge" data-tone="ok" title="Passed in the last bench run">pass' + (rate != null ? ' · ' + U().esc(U().pctText(rate)) : '') + '</span>'
          : '<span class="badge" data-tone="warn" title="' + U().esc(r.error || r.state || 'did not pass') + '">' + U().esc(r.state || 'fail') + (rate != null ? ' · ' + U().esc(U().pctText(rate)) : '') + '</span>';
      }
      const id = 'bench-cb-' + String(t.id).replace(/[^\w-]/g, '_');
      return '<div class="bench-task"><input type="checkbox" id="' + id + '" data-task="' + U().esc(t.id) + '"' + (checked[t.id] ? ' checked' : '') + (running ? ' disabled' : '') + '>' +
        '<label for="' + id + '" style="min-width:0;cursor:pointer"><span class="bt-title" style="display:block">' + U().esc(t.title || t.id) + '</span>' +
        '<span class="bt-sub">' + U().esc([t.id, t.category, t.kind, t.timeout_min ? 'limit ' + t.timeout_min + ' min' : ''].filter(Boolean).join(' · ')) + '</span></label>' + mark + '</div>';
    }).join('');
    const run = $('btn-bench-run');
    if (run) run.disabled = running;
    const all = $('btn-bench-all');
    if (all) all.textContent = tasks.every((t) => checked[t.id]) ? 'Clear' : 'Select all';
  }

  function renderProgress() {
    const host = $('bench-progress');
    if (!host) return;
    if (!running && !progress) { host.innerHTML = ''; return; }
    const p = progress || {};
    const pct = p.total ? Math.round((p.done || 0) / p.total * 100) : 0;
    host.innerHTML = '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><i style="width:' + pct + '%"></i></div>' +
      '<p class="help" style="margin:6px 0 0;font-size:var(--fs-1);color:var(--fg-muted)">' +
      (p.total ? U().esc((p.done || 0) + ' / ' + p.total + ' tasks done') + (p.task_id ? ' · last: ' + U().esc(p.task_id) + (p.passed === true ? ' passed' : p.passed === false ? ' did not pass' : '') : '') : (running ? 'Bench running; waiting for the first task to finish…' : '')) + '</p>';
  }

  function renderScore() {
    const host = $('bench-score');
    if (!host) return;
    if (!badge) { host.innerHTML = '<div class="row"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>'; return; }
    if (badge.error) {
      const e = badge.error;
      host.innerHTML = '<div class="empty"><strong>' + (e.missing ? 'Bench not available' : 'Score unavailable') + '</strong><span class="why">' +
        U().esc(e.missing ? 'No bench module on this server, so there is no measurement and no badge.' : U().errText(e.error)) + '</span></div>';
      return;
    }
    const b = badge;
    const when = b.timestamp ? U().fmtDate(b.timestamp) : '';
    const who = [b.engine, b.model].filter(Boolean).join(' / ');
    if (b.certified === true && typeof b.pass_rate === 'number') {
      host.innerHTML = '<div class="bench-score"><img src="/static/assets/badge-certified.svg" alt="Kyora Bench certified: measured pass rate at or above 94 percent" width="120" height="120">' +
        '<div><div class="bs-num">' + U().esc((b.pass_rate * 100).toFixed(1)) + '<small> %</small></div><div class="bs-meta">' +
        '<div><b>Certified</b> by your own full-suite run</div>' +
        (b.passed != null && b.run != null ? '<div>' + U().esc(b.passed) + ' of ' + U().esc(b.run) + ' tasks passed</div>' : '') +
        (who ? '<div>' + U().esc(who) + '</div>' : '') + (when ? '<div>measured ' + U().esc(when) + '</div>' : '') + '</div></div></div>';
      return;
    }
    const ran = b.run != null && Number(b.run) > 0;
    const below = b.suite_complete === true && typeof b.pass_rate === 'number' && b.pass_rate < 0.94;
    host.innerHTML = '<div class="bench-score">' + (below ? '<img src="/static/assets/badge-below.svg" alt="Not certified: measured pass rate below 94 percent" width="120" height="120">' : '<div class="bs-num" aria-hidden="true">-</div>') +
      '<div class="bs-meta"><div><b>Not certified</b></div><div>' + U().esc(b.reason || (ran ? 'the last run did not meet the certification rule' : 'no bench run yet')) + '</div>' +
      (ran && b.passed != null ? '<div>Last run: ' + U().esc(b.passed) + ' of ' + U().esc(b.run) + ' tasks passed' + (b.suite_complete ? '' : ' (partial suite)') + '</div>' : '') +
      (who ? '<div>' + U().esc(who) + '</div>' : '') + (when ? '<div>' + U().esc(when) + '</div>' : '') +
      '<div style="color:var(--fg-subtle)">A badge is granted only when all tasks ran in one bench run on one engine/model and at least 94 % passed.</div></div></div>';
  }

  function renderResults() {
    const host = $('bench-results');
    if (!host) return;
    if (!results) { host.innerHTML = ''; return; }
    if (results.error) {
      host.innerHTML = '<div class="empty"><strong>No results</strong><span class="why">' + U().esc(results.error.missing ? 'The bench results API is not available on this server.' : U().errText(results.error.error)) + '</span></div>';
      return;
    }
    const rows = resultRows();
    if (!rows.length) { host.innerHTML = '<div class="empty"><strong>No bench run yet</strong><span class="why">Select tasks and run them. Results stay on this device under KYORA_HOME/bench/.</span></div>'; return; }
    const ts = results.timestamp || results.generated_at || results.finished_at;
    host.innerHTML = (ts ? '<p class="help" style="font-size:var(--fs-1);color:var(--fg-muted);margin:0 0 8px">Run of ' + U().esc(U().fmtDate(ts)) + '</p>' : '') +
      '<div class="table-wrap"><table class="data"><thead><tr><th scope="col">Task</th><th scope="col">Result</th><th scope="col">Gate</th><th scope="col">Time</th><th scope="col">Model</th></tr></thead><tbody>' +
      rows.map((r) => {
        const rate = gateRate(r);
        return '<tr><td class="mono">' + U().esc(r.task_id || '') + '</td><td>' + (r.passed ? '<span class="badge" data-tone="ok">pass</span>' : '<span class="badge" data-tone="warn" title="' + U().esc(r.error || '') + '">' + U().esc(r.state || 'fail') + '</span>') + '</td>' +
          '<td class="num">' + U().esc(U().pctText(rate)) + '</td><td class="num">' + (r.duration_s != null ? U().esc(K.metrics.fmtMs(r.duration_s * 1000)) : '-') + '</td>' +
          '<td class="mono" style="font-size:var(--fs-0)">' + U().esc(r.active_model || r.engine || '-') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  /* ---------------------------------------------------------------- actions */
  async function startRun() {
    const ids = (tasks || []).filter((t) => checked[t.id]).map((t) => t.id);
    if (!ids.length) { K.toast('Select at least one task.', 'warn'); return; }
    const effort = K.app && K.app.settingValue ? K.app.settingValue('claude_code.effort', null) : null;
    const warning = benchWarning(effort, estimate && estimate.engine);
    const noEngine = estimate && estimate.engine_available === false;
    const body = '<p>' + U().esc(warning) + '</p><p><b>' + ids.length + ' task' + (ids.length > 1 ? 's' : '') + '</b> = ' + ids.length + ' full pipeline run' + (ids.length > 1 ? 's' : '') +
      ', roughly ' + (ids.length * 10) + '-' + (ids.length * 30) + ' minutes in sequence.</p>' +
      (estimate && estimate.engine ? '<p class="help">Code engine: ' + U().esc(estimate.engine) + '</p>' : '') +
      (noEngine ? '<div class="notice" data-tone="danger"><strong>BLOCKED:</strong> no code engine is available, so the server will refuse this run.</div>' : '') +
      '<p class="help">The certified badge needs every task in one run; a subset is measured but never certified.</p>';
    const ok = await K.confirm('Run Kyora Bench?', body, { okText: 'Run ' + ids.length + ' task' + (ids.length > 1 ? 's' : '') });
    if (!ok) return;
    const r = await K.api.maybe(K.api.post('/api/bench/run', { task_ids: ids, confirm: true }));
    if (!r.ok) {
      const st = r.error && r.error.status;
      K.toast(r.missing ? 'The bench API is not available on this server.' : st === 409 ? 'A bench run is already in progress.' : 'Bench not started: ' + U().errText(r.error), 'danger');
      if (st === 409) setRunning(true);
      return;
    }
    progress = { done: 0, total: ids.length };
    setRunning(true);
    K.toast('Bench started: ' + ids.length + ' task' + (ids.length > 1 ? 's' : ''), 'ok');
    renderTasks();
  }

  async function cancelRun() {
    const ok = await K.confirm('Cancel the bench run?', '<p>The current task is cancelled; finished tasks keep their results. A cancelled run is never certified.</p>', { okText: 'Cancel bench', cancelText: 'Keep running', tone: 'danger' });
    if (!ok) return;
    const r = await K.api.maybe(K.api.post('/api/bench/cancel', {}));
    if (!r.ok) { K.toast('Cancel failed: ' + U().errText(r.error), 'danger'); return; }
    K.toast('Bench cancel requested', 'ok');
  }

  function onEvent(ev) {
    if (ev.type === 'bench.progress') {
      progress = ev.data || {};
      if (!running) setRunning(true); else renderProgress();
    } else if (ev.type === 'bench.done') {
      setRunning(false);
      progress = null;
      renderProgress();
      loadScore();
      K.toast('Bench finished', 'ok');
    }
  }

  function init() {
    const host = $('bench-tasks');
    if (host) host.addEventListener('change', (e) => {
      const cb = e.target.closest('input[data-task]');
      if (!cb) return;
      if (cb.checked) checked[cb.getAttribute('data-task')] = true; else delete checked[cb.getAttribute('data-task')];
      const all = $('btn-bench-all');
      if (all && tasks) all.textContent = tasks.every((t) => checked[t.id]) ? 'Clear' : 'Select all';
    });
    const all = $('btn-bench-all');
    if (all) all.addEventListener('click', () => {
      if (!tasks || !tasks.length || running) return;
      const every = tasks.every((t) => checked[t.id]);
      checked = {};
      if (!every) tasks.forEach((t) => { checked[t.id] = true; });
      renderTasks();
    });
    const run = $('btn-bench-run');
    if (run) run.addEventListener('click', startRun);
    const cancel = $('btn-bench-cancel');
    if (cancel) cancel.addEventListener('click', cancelRun);
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('view') >= 0 && s.view === 'bench') {
        if (!loadedOnce || !tasks) { loadedOnce = true; loadTasks(); }
        loadScore();
      }
    });
  }

  (K.modules = K.modules || {}).bench = { init, onEvent, warning: benchWarning };
})();
