/*
 * Kyora browser test harness (served with /runner/<pid>/<ref>?run=<rid>&iter=<n>&auto=1&post=1).
 *
 * 1. loads /preview/<pid>/<ref>/index.html?kyora=1 in a same-origin iframe;
 * 2. injects /static/runner/kyora-test-api.js (window.KyoraTest) into it, takes the
 *    'start' screenshot, injects /preview/<pid>/<ref>/tests/browser-tests.js and runs
 *    the registered tests sequentially (per-test timeout, default 10 s);
 * 3. syntax-checks the manifest's scripts (new Function) and modules (import() via a
 *    <script type=module> trampoline inside the iframe), takes the 'end' screenshot;
 * 4. builds the kyora:test-result message (docs/architecture.md), stores it in
 *    window.__kyoraResult, posts it to the parent window when framed and, with post=1,
 *    POSTs it to /api/runs/<rid>/browser-results (or the slide endpoint for ref v<n>).
 *
 * summary counts the game's registered tests; harness diagnostics use ids starting
 * with 'harness:' (e.g. 'harness:no-tests') and appear only in results.
 * Vanilla ES2017, no dependencies (Safari iOS 15+, Chrome Android, desktop).
 */
(function () {
  'use strict';

  var STATIC = '/static/runner/';
  var LOAD_TIMEOUT_MS = 30000;
  var SCRIPT_TIMEOUT_MS = 15000;
  var IMPORT_TIMEOUT_MS = 10000;

  var $ = function (id) { return document.getElementById(id); };
  var cfg = parseConfig();
  var running = false;
  var frame = null;
  var harnessLog = [];

  function parseConfig() {
    var parts = location.pathname.split('/').filter(Boolean).map(function (p) {
      try { return decodeURIComponent(p); } catch (e) { return p; }
    });
    var i = parts.indexOf('runner');
    var q = new URLSearchParams(location.search);
    var pid = q.get('pid') || (i >= 0 ? parts[i + 1] : null) || null;
    var ref = q.get('ref') || (i >= 0 ? parts[i + 2] : null) || null;
    var iterRaw = q.get('iter');
    var iter = iterRaw != null && /^\d+$/.test(iterRaw) ? parseInt(iterRaw, 10) : null;
    var timeout = parseInt(q.get('timeout') || '', 10);
    return {
      pid: pid, ref: ref, run: q.get('run') || null, iter: iter,
      auto: q.get('auto') === '1', post: q.get('post') === '1',
      timeoutMs: timeout > 0 ? timeout : 10000
    };
  }

  function enc(s) { return encodeURIComponent(String(s)); }
  function encPath(p) { return String(p).split('/').map(enc).join('/'); }
  function previewBase() { return '/preview/' + enc(cfg.pid) + '/' + enc(cfg.ref) + '/'; }
  function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }
  function hlog(msg) { harnessLog.push('[harness] ' + msg); renderLogs([]); }
  function errMsg(e) { return e && e.message ? (e.name && e.name !== 'Error' ? e.name + ': ' : '') + e.message : String(e); }
  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* ------------------------------------------------------------------ rendering */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function setStatus(text, tone) {
    var s = $('status');
    s.textContent = text;
    if (tone) s.setAttribute('data-tone', tone); else s.removeAttribute('data-tone');
  }

  function renderMeta() {
    var bits = ['project ' + (cfg.pid || '?'), 'ref ' + (cfg.ref || '?')];
    if (cfg.run) bits.push('run ' + cfg.run);
    if (cfg.iter != null) bits.push('iteration ' + cfg.iter);
    $('meta').textContent = bits.join(' · ');
  }

  function clearList(id, emptyText) {
    var ul = $(id);
    ul.innerHTML = '';
    if (emptyText) {
      var li = el('li', 'empty', emptyText);
      li.style.display = 'block';
      ul.appendChild(li);
    }
  }

  function addResult(r) {
    var ul = $('results');
    var empty = ul.querySelector('.empty');
    if (empty) ul.removeChild(empty);
    var li = el('li');
    li.appendChild(el('span', 'badge ' + r.status, r.status));
    var body = el('div');
    body.appendChild(el('div', null, r.name || r.id));
    body.appendChild(el('div', 'rid', r.id));
    if (r.error) body.appendChild(el('div', 'err', r.error));
    li.appendChild(body);
    li.appendChild(el('span', 'ms', r.ms != null ? r.ms + ' ms' : ''));
    ul.appendChild(li);
  }

  function renderSyntax(list) {
    clearList('syntax', list.length ? null : 'No scripts or modules listed.');
    list.forEach(function (s) {
      var li = el('li');
      li.appendChild(el('span', 'badge ' + (s.ok ? 'pass' : 'fail'), s.ok ? 'ok' : 'error'));
      var body = el('div');
      body.appendChild(el('div', null, s.file));
      body.appendChild(el('div', 'rid', s.method));
      if (s.error) body.appendChild(el('div', 'err', s.error));
      li.appendChild(body);
      li.appendChild(el('span', 'ms', ''));
      $('syntax').appendChild(li);
    });
  }

  function renderShots(shots) {
    var box = $('shots');
    box.innerHTML = '';
    if (!shots.length) { box.appendChild(el('span', 'empty', 'None.')); return; }
    shots.forEach(function (s) {
      var fig = el('figure');
      if (s.data_url) {
        var img = el('img');
        img.alt = 'screenshot ' + s.name;
        img.src = s.data_url;
        fig.appendChild(img);
      } else {
        fig.appendChild(el('div', 'empty', 'no canvas image'));
      }
      fig.appendChild(el('figcaption', null, s.name));
      box.appendChild(fig);
    });
  }

  function renderTotals(summary, extraFailures) {
    var t = $('totals');
    t.innerHTML = '';
    t.appendChild(el('span', 'pill ok', summary.passed + ' passed'));
    t.appendChild(el('span', 'pill bad', summary.failed + ' failed'));
    if (summary.skipped) t.appendChild(el('span', 'pill skip', summary.skipped + ' skipped'));
    t.appendChild(el('span', 'pill', summary.total + ' total'));
    if (extraFailures) t.appendChild(el('span', 'pill bad', extraFailures + ' harness/syntax problem' + (extraFailures > 1 ? 's' : '')));
  }

  function renderLogs(gameLogs) {
    $('logs').textContent = harnessLog.concat(gameLogs || []).join('\n');
  }

  /* ------------------------------------------------------------------ loading helpers */
  function fetchStatus(url) {
    return fetch(url, { cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
      return r.text().then(function (text) {
        return { ok: r.ok, status: r.status, text: text, reason: r.ok ? null : 'HTTP ' + r.status };
      });
    }, function (e) {
      return { ok: false, status: 0, text: '', reason: 'network error: ' + errMsg(e) };
    });
  }

  function loadFrame(src) {
    return new Promise(function (resolve, reject) {
      var wrap = $('frame-wrap');
      wrap.innerHTML = '';
      var f = document.createElement('iframe');
      f.title = 'Game under test';
      f.setAttribute('allow', 'autoplay; fullscreen; gamepad');
      var done = false;
      var timer = setTimeout(function () {
        if (done) return;
        done = true;
        reject(new Error('the game page did not finish loading within ' + (LOAD_TIMEOUT_MS / 1000) + ' s'));
      }, LOAD_TIMEOUT_MS);
      f.addEventListener('load', function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(f);
      });
      f.addEventListener('error', function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        reject(new Error('the game page failed to load'));
      });
      f.src = src;
      wrap.appendChild(f);
    });
  }

  function injectScript(doc, src) {
    return new Promise(function (resolve) {
      var s = doc.createElement('script');
      var done = false;
      var timer = setTimeout(function () { finish(false, 'timed out after ' + (SCRIPT_TIMEOUT_MS / 1000) + ' s'); }, SCRIPT_TIMEOUT_MS);
      function finish(ok, error) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve({ ok: ok, error: error || null });
      }
      s.onload = function () { finish(true); };
      s.onerror = function () { finish(false, 'could not load ' + src); };
      s.async = false;
      s.src = src;
      (doc.head || doc.documentElement).appendChild(s);
    });
  }

  // Capture in a rAF callback (after the game's own frame) so WebGL canvases without
  // preserveDrawingBuffer still have content; falls back to a direct capture.
  function screenshotInFrame(win, name) {
    return new Promise(function (resolve) {
      var K = win && win.KyoraTest;
      if (!K) { resolve(null); return; }
      var done = false;
      function take() {
        if (done) return;
        done = true;
        var url = null;
        try { url = K.screenshot(name); } catch (e) { hlog('screenshot ' + name + ' failed: ' + errMsg(e)); }
        resolve(url || null);
      }
      setTimeout(take, 250);
      try { win.requestAnimationFrame(take); } catch (e) { take(); }
    });
  }

  /* ------------------------------------------------------------------ syntax checks */
  function scriptListFromDocument(doc) {
    var base = new URL(previewBase(), location.href).href;
    var scripts = [];
    var modules = [];
    if (!doc) return { scripts: scripts, modules: modules };
    Array.prototype.forEach.call(doc.querySelectorAll('script[src]'), function (s) {
      var abs = s.src;
      if (!abs || abs.indexOf(base) !== 0 || abs.indexOf(STATIC) !== -1) return;
      var rel = decodeURIComponent(abs.slice(base.length).split('?')[0].split('#')[0]);
      if (!rel || rel === 'tests/browser-tests.js') return;
      if ((s.getAttribute('type') || '').toLowerCase() === 'module') modules.push(rel); else scripts.push(rel);
    });
    return { scripts: scripts, modules: modules };
  }

  function importCheck(doc, file) {
    return new Promise(function (resolve) {
      var method = 'import()';
      if (!doc || !doc.head && !doc.documentElement) {
        resolve({ file: file, ok: false, error: 'preview frame unavailable', method: method });
        return;
      }
      var key = 'k' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      var url = new URL(previewBase() + encPath(file), location.href).href;
      var done = false;
      var timer = setTimeout(function () { finish(false, 'import() did not settle within ' + (IMPORT_TIMEOUT_MS / 1000) + ' s'); }, IMPORT_TIMEOUT_MS);
      function onMsg(ev) {
        var d = ev.data;
        if (d && d.type === 'kyora:import-check' && d.key === key) finish(!!d.ok, d.error);
      }
      function finish(ok, error) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        window.removeEventListener('message', onMsg);
        resolve({ file: file, ok: ok, error: ok ? null : String(error || 'import failed'), method: method });
      }
      window.addEventListener('message', onMsg);
      var k = JSON.stringify(key);
      var s = doc.createElement('script');
      s.type = 'module';
      s.textContent =
        'import(' + JSON.stringify(url) + ').then(function () {' +
        ' parent.postMessage({type: "kyora:import-check", key: ' + k + ', ok: true}, "*");' +
        '}, function (e) {' +
        ' parent.postMessage({type: "kyora:import-check", key: ' + k + ', ok: false,' +
        ' error: (e && e.name ? e.name + ": " : "") + (e && e.message ? e.message : String(e))}, "*");' +
        '});';
      (doc.head || doc.documentElement).appendChild(s);
    });
  }

  function syntaxChecks(doc) {
    return fetchStatus(previewBase() + '.kyora/manifest.json').then(function (res) {
      var scripts = [];
      var modules = [];
      var manifest = null;
      if (res.ok) {
        try { manifest = JSON.parse(res.text); } catch (e) { hlog('manifest is not valid JSON: ' + errMsg(e)); }
      } else {
        hlog('manifest not available (' + res.reason + '); using <script src> of the page');
      }
      if (manifest && (Array.isArray(manifest.scripts) || Array.isArray(manifest.modules))) {
        scripts = (manifest.scripts || []).filter(function (x) { return typeof x === 'string' && x; });
        modules = (manifest.modules || []).filter(function (x) { return typeof x === 'string' && x; });
      } else {
        var found = scriptListFromDocument(doc);
        scripts = found.scripts;
        modules = found.modules;
      }
      var out = [];
      var chain = Promise.resolve();
      scripts.forEach(function (file) {
        chain = chain.then(function () {
          return fetchStatus(previewBase() + encPath(file)).then(function (src) {
            if (!src.ok) {
              out.push({ file: file, ok: false, error: 'could not fetch the file (' + src.reason + ')', method: 'new Function' });
              return;
            }
            try {
              // Parses the source as a function body without running it.
              new Function(src.text); // eslint-disable-line no-new-func
              out.push({ file: file, ok: true, error: null, method: 'new Function' });
            } catch (e) {
              out.push({ file: file, ok: false, error: errMsg(e), method: 'new Function' });
            }
          });
        });
      });
      modules.forEach(function (file) {
        chain = chain.then(function () {
          return importCheck(doc, file).then(function (r) { out.push(r); });
        });
      });
      return chain.then(function () { return out; });
    });
  }

  /* ------------------------------------------------------------------ posting */
  function postTarget() {
    if (cfg.run) return '/api/runs/' + enc(cfg.run) + '/browser-results';
    var m = /^v(\d+)$/.exec(cfg.ref || '');
    if (m) return '/api/projects/' + enc(cfg.pid) + '/slides/' + m[1] + '/browser-results';
    return null;
  }

  function postResult(message) {
    var url = postTarget();
    if (!url) return Promise.resolve({ ok: false, status: 0, url: null, error: 'no run id and ref is not a slide (v<n>): nothing to post to' });
    var body = JSON.stringify(message);
    var attempt = 0;
    function once() {
      attempt += 1;
      return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, credentials: 'same-origin' })
        .then(function (r) {
          return r.text().then(function (text) {
            if (!r.ok && r.status >= 500 && attempt < 3) return delay(800 * attempt).then(once);
            return { ok: r.ok, status: r.status, url: url, error: r.ok ? null : (text || ('HTTP ' + r.status)).slice(0, 400) };
          });
        }, function (e) {
          if (attempt < 3) return delay(800 * attempt).then(once);
          return { ok: false, status: 0, url: url, error: 'network error: ' + errMsg(e) };
        });
    }
    return once();
  }

  /* ------------------------------------------------------------------ main */
  function summarize(results) {
    var s = { passed: 0, failed: 0, skipped: 0, total: 0 };
    results.forEach(function (r) {
      if (String(r.id).indexOf('harness:') === 0) return;
      s.total += 1;
      if (r.status === 'pass') s.passed += 1; else if (r.status === 'skip') s.skipped += 1; else s.failed += 1;
    });
    return s;
  }

  function run() {
    if (running) return Promise.resolve(window.__kyoraResult || null);
    if (!cfg.pid || !cfg.ref) {
      setStatus('Cannot run: the URL must look like /runner/<project>/<ref>', 'bad');
      return Promise.resolve(null);
    }
    running = true;
    window.__kyoraDone = false;
    window.__kyoraResult = undefined;
    window.__kyoraPost = undefined;
    harnessLog = [];
    $('run-again').disabled = true;
    $('post').textContent = '';
    $('totals').innerHTML = '';
    clearList('results', 'Running…');
    clearList('syntax', 'Waiting for tests…');
    renderShots([]);
    setStatus('Loading the game…');

    var startedAt = new Date().toISOString();
    var t0 = now();
    var results = [];
    var gameLogs = [];
    var shots = [];
    var win = null;
    var doc = null;
    var base = previewBase();

    function harnessFail(id, name, error) {
      var r = { id: id, name: name, status: 'fail', ms: 0, error: error };
      results.push(r);
      addResult(r);
      hlog(id + ': ' + error);
    }

    var flow = fetchStatus(base + 'index.html').then(function (pre) {
      if (!pre.ok) throw new Error('the game page ' + base + 'index.html is not available (' + pre.reason + ')');
      return loadFrame(base + 'index.html?kyora=1');
    }).then(function (f) {
      frame = f;
      win = f.contentWindow;
      try { doc = f.contentDocument; } catch (e) { doc = null; }
      if (!doc || !win) throw new Error('the game frame is not accessible (not same-origin?)');
      setStatus('Injecting the test API…');
      return injectScript(doc, STATIC + 'kyora-test-api.js');
    }).then(function (res) {
      if (!res.ok || !win.KyoraTest) throw new Error('could not inject the Kyora test API: ' + (res.error || 'KyoraTest missing'));
      return win.KyoraTest.frames(2).then(function () { return win.KyoraTest.wait(150); });
    }).then(function () {
      return screenshotInFrame(win, 'start').then(function (url) { shots.push({ name: 'start', data_url: url }); });
    }).then(function () {
      setStatus('Loading tests/browser-tests.js…');
      return fetchStatus(base + 'tests/browser-tests.js');
    }).then(function (tf) {
      if (!tf.ok) return { problem: 'tests/browser-tests.js is missing (' + tf.reason + ')', evalErrors: [] };
      var evalErrors = [];
      var onErr = function (ev) {
        var file = ev && ev.filename ? String(ev.filename) : '';
        if (file.indexOf('tests/browser-tests.js') !== -1) {
          evalErrors.push((ev.message || 'error') + (ev.lineno ? ' (line ' + ev.lineno + ')' : ''));
        }
      };
      win.addEventListener('error', onErr);
      return injectScript(doc, base + 'tests/browser-tests.js?t=' + Date.now()).then(function (r) {
        win.removeEventListener('error', onErr);
        return { problem: r.ok ? null : 'tests/browser-tests.js failed to load: ' + r.error, evalErrors: evalErrors };
      });
    }).then(function (load) {
      var K = win.KyoraTest;
      var registered = K.__tests.length;
      hlog(registered + ' test(s) registered');
      if (load.evalErrors.length) {
        harnessFail('harness:tests-file-error', 'tests/browser-tests.js evaluated without errors', load.evalErrors.join('; '));
      }
      if (!registered) {
        harnessFail('harness:no-tests', 'Browser tests registered',
          load.problem || 'tests/browser-tests.js registered no KyoraTest.test(...) cases');
        return null;
      }
      if (load.problem) harnessFail('harness:tests-file', 'tests/browser-tests.js loaded', load.problem);
      clearList('results');
      results.forEach(addResult);
      setStatus('Running ' + registered + ' test(s)…');
      return K.__run({ timeoutMs: cfg.timeoutMs, onResult: function (r) { addResult(r); } }).then(function (out) {
        out.results.forEach(function (r) { results.push(r); });
        gameLogs = out.logs || [];
      });
    }).catch(function (e) {
      clearList('results');
      results.forEach(addResult);
      harnessFail('harness:load', 'Game page loaded and test API injected', errMsg(e));
      if (!results.some(function (r) { return r.id === 'harness:no-tests'; })) {
        harnessFail('harness:no-tests', 'Browser tests registered', 'no tests ran because the harness could not start: ' + errMsg(e));
      }
    }).then(function () {
      setStatus('Checking JavaScript syntax…');
      return syntaxChecks(doc).catch(function (e) {
        hlog('syntax checks failed: ' + errMsg(e));
        return [];
      });
    }).then(function (syntax) {
      renderSyntax(syntax);
      return screenshotInFrame(win, 'end').then(function (url) {
        shots.push({ name: 'end', data_url: url });
        return syntax;
      });
    }).then(function (syntax) {
      var summary = summarize(results);
      var vw = window.innerWidth, vh = window.innerHeight;
      try { if (win && win.innerWidth) { vw = win.innerWidth; vh = win.innerHeight; } } catch (e) { /* keep top window size */ }
      var message = {
        type: 'kyora:test-result',
        run_id: cfg.run,
        iteration: cfg.iter,
        ref: cfg.ref,
        results: results,
        summary: summary,
        syntax_checks: syntax,
        screenshots: shots,
        env: {
          ua: navigator.userAgent,
          viewport: [vw, vh],
          touch: ('ontouchstart' in window) || (navigator.maxTouchPoints || 0) > 0,
          started_at: startedAt,
          duration_ms: Math.round(now() - t0)
        }
      };
      window.__kyoraResult = message;
      renderShots(shots);
      renderLogs(gameLogs);
      var extra = results.filter(function (r) { return String(r.id).indexOf('harness:') === 0; }).length +
        syntax.filter(function (s) { return !s.ok; }).length;
      renderTotals(summary, extra);
      var allOk = summary.total > 0 && summary.failed === 0 && extra === 0;
      setStatus(allOk ? 'All ' + summary.total + ' tests passed' :
        summary.total === 0 ? 'No tests ran' : summary.failed + ' of ' + summary.total + ' tests failed' + (extra ? ' (+ ' + extra + ' other problem(s))' : ''),
        allOk ? 'ok' : 'bad');
      if (window.parent && window.parent !== window) {
        try { window.parent.postMessage(message, '*'); } catch (e) { hlog('postMessage to parent failed: ' + errMsg(e)); }
      }
      if (!cfg.post) {
        window.__kyoraPost = null;
        $('post').textContent = 'Results not posted (post=1 not set).';
        return null;
      }
      $('post').textContent = 'Posting results…';
      return postResult(message).then(function (p) {
        window.__kyoraPost = p;
        $('post').textContent = p.ok ? 'Results posted to Kyora (' + p.url + ', HTTP ' + p.status + ').'
          : 'Posting results failed' + (p.url ? ' (' + p.url + ')' : '') + ': ' + p.error;
        if (!p.ok) $('post').style.color = 'var(--bad)'; else $('post').style.color = '';
      });
    }).catch(function (e) {
      // Last-resort guard: the harness must always finish with a visible state.
      setStatus('Harness error: ' + errMsg(e), 'bad');
      hlog('fatal: ' + errMsg(e));
    }).then(function () {
      running = false;
      window.__kyoraDone = true;
      $('run-again').disabled = false;
      $('run-again').textContent = 'Run again';
      return window.__kyoraResult || null;
    });
    return flow;
  }

  window.KyoraHarness = { run: run, config: cfg };

  function init() {
    renderMeta();
    var btn = $('run-again');
    btn.disabled = !(cfg.pid && cfg.ref);
    btn.addEventListener('click', function () { run(); });
    if (!cfg.pid || !cfg.ref) {
      setStatus('Cannot run: the URL must look like /runner/<project>/<ref>', 'bad');
      return;
    }
    if (cfg.auto) run();
    else setStatus('Ready. Press "Run tests".');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
