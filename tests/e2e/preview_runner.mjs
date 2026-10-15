#!/usr/bin/env node
/*
 * End-to-end test of the Kyora browser test harness (kyora/web/runner/*) in headless Chromium.
 *
 *   NODE_PATH=$(npm root -g) PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/e2e/preview_runner.mjs
 *
 * Self-contained: a tiny node:http server mimics the Kyora routes the harness needs
 *   /runner/p1/<ref>                    -> kyora/web/runner/harness.html
 *   /static/runner/*                    -> kyora/web/runner/*
 *   /preview/p1/<ref>/*                 -> a fixture game (see REFS below)
 *   POST /api/runs/<rid>/browser-results, POST /api/projects/p1/slides/<n>/browser-results -> recorded
 * and every scenario asserts on window.__kyoraResult and on the recorded POST body. The posted
 * payload of the good game is also fed to kyora.pipeline.quality (python3) to prove the JS
 * message and the Python collector agree. Exit code 0 = all scenarios passed.
 */
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { launchChromium, loadPlaywright } from '../../kyora/runners/headless_browser.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const RUNNER_DIR = path.join(REPO, 'kyora', 'web', 'runner');
const FIXTURES = path.join(REPO, 'tests', 'fixtures');
const HARNESS_GAME = path.join(FIXTURES, 'harness-game');

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.jpg': 'image/jpeg', '.wav': 'audio/wav', '.mp3': 'audio/mpeg',
};

/* ------------------------------------------------------------------ fixture variants */
function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isDirectory()) copyDir(s, d); else fs.copyFileSync(s, d);
  }
}

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'kyora-e2e-'));
const BROKEN = path.join(TMP, 'broken');
copyDir(HARNESS_GAME, BROKEN);
fs.appendFileSync(path.join(BROKEN, 'src', 'game.js'), '\nfunction broken( {\n');
fs.writeFileSync(path.join(BROKEN, 'src', 'util.mjs'), 'export const clamp = ;\n');
const NO_TESTS = path.join(TMP, 'no-tests');
copyDir(HARNESS_GAME, NO_TESTS);
fs.rmSync(path.join(NO_TESTS, 'tests', 'browser-tests.js'));

const REFS = { v1: HARNESS_GAME, v2: BROKEN, v3: NO_TESTS };
const GAME_OK = path.join(FIXTURES, 'game-ok');
const GAME_BROKEN = path.join(FIXTURES, 'game-broken');
if (fs.existsSync(path.join(GAME_OK, 'index.html'))) REFS.v4 = GAME_OK;
if (fs.existsSync(path.join(GAME_BROKEN, 'index.html'))) REFS.v5 = GAME_BROKEN;

/* ------------------------------------------------------------------ server */
const posted = []; // {url, body}

function sendFile(res, file) {
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('not found'); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
    res.end(data);
  });
}

function under(root, rel) {
  const p = path.resolve(root, rel);
  return p === root || p.startsWith(root + path.sep) ? p : null;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = decodeURIComponent(url.pathname);
  if (req.method === 'POST') {
    const ok = /^\/api\/runs\/[^/]+\/browser-results$/.test(p) || /^\/api\/projects\/p1\/slides\/\d+\/browser-results$/.test(p);
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      if (!ok) { res.writeHead(404); res.end('{}'); return; }
      let body = null;
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (e) { body = { parse_error: String(e) }; }
      posted.push({ url: p, body });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    return;
  }
  let m = /^\/runner\/p1\/([^/]+)\/?$/.exec(p);
  if (m) return sendFile(res, path.join(RUNNER_DIR, 'harness.html'));
  m = /^\/static\/runner\/(.+)$/.exec(p);
  if (m) { const f = under(RUNNER_DIR, m[1]); return f ? sendFile(res, f) : (res.writeHead(403), res.end()); }
  m = /^\/preview\/p1\/([^/]+)\/(.+)$/.exec(p);
  if (m) {
    const root = REFS[m[1]];
    const f = root && under(root, m[2]);
    if (f) return sendFile(res, f);
  }
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('not found');
});

/* ------------------------------------------------------------------ helpers */
let failures = 0;
async function scenario(name, fn) {
  const t0 = Date.now();
  try {
    await fn();
    console.log(`ok   - ${name} (${Date.now() - t0} ms)`);
  } catch (e) {
    failures += 1;
    console.log(`FAIL - ${name}: ${e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n       ') : e}`);
  }
}

async function runHarness(context, base, pathAndQuery, { timeout = 60000 } = {}) {
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + pathAndQuery, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__kyoraDone === true, null, { timeout, polling: 100 });
  const out = await page.evaluate(() => ({ result: window.__kyoraResult, post: window.__kyoraPost, status: document.getElementById('status').textContent }));
  return { page, errors, ...out };
}

function spawnAsync(cmd, args) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { env: process.env });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    const timer = setTimeout(() => child.kill('SIGKILL'), 120000);
    child.on('close', (status) => { clearTimeout(timer); resolve({ status, stdout, stderr }); });
  });
}

function lastPost(urlPath) {
  for (let i = posted.length - 1; i >= 0; i -= 1) if (posted[i].url === urlPath) return posted[i].body;
  return null;
}

function assertSchema(r) {
  assert.equal(r.type, 'kyora:test-result');
  assert.ok(Array.isArray(r.results), 'results array');
  for (const x of r.results) {
    assert.equal(typeof x.id, 'string');
    assert.ok(['pass', 'fail', 'skip'].includes(x.status), `status ${x.status}`);
    assert.ok(x.ms === null || typeof x.ms === 'number');
    assert.ok(x.error === null || typeof x.error === 'string');
  }
  for (const k of ['passed', 'failed', 'skipped', 'total']) assert.equal(typeof r.summary[k], 'number');
  assert.ok(Array.isArray(r.syntax_checks));
  assert.ok(Array.isArray(r.screenshots));
  assert.equal(typeof r.env.ua, 'string');
  assert.ok(Array.isArray(r.env.viewport) && r.env.viewport.length === 2);
  assert.equal(typeof r.env.touch, 'boolean');
  assert.equal(typeof r.env.started_at, 'string');
  assert.equal(typeof r.env.duration_ms, 'number');
}

/* ------------------------------------------------------------------ main */
async function main() {
  const pw = loadPlaywright();
  if (!pw) throw new Error('playwright not found (set NODE_PATH=$(npm root -g))');
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  console.log(`# fixture server ${base}; refs ${Object.keys(REFS).join(', ')}`);
  const browser = await launchChromium(pw);
  const context = await browser.newContext({ viewport: { width: 390, height: 760 }, hasTouch: true });
  try {
    let goodResult = null;
    await scenario('good game: tests pass, screenshots, syntax checks, POST equals __kyoraResult', async () => {
      const { result, post, errors, page } = await runHarness(context, base, '/runner/p1/v1?run=r1&iter=1&auto=1&post=1');
      assertSchema(result);
      assert.equal(result.run_id, 'r1');
      assert.equal(result.iteration, 1);
      assert.equal(result.ref, 'v1');
      assert.ok(result.summary.total > 0, 'summary.total > 0');
      assert.ok(result.summary.passed >= 1, 'passed >= 1');
      const failed = result.results.filter((x) => x.status !== 'pass');
      assert.deepEqual(failed, [], 'every fixture test passes');
      assert.equal(result.summary.total, 9);
      assert.equal(result.screenshots.length, 2);
      assert.deepEqual(result.screenshots.map((s) => s.name), ['start', 'end']);
      for (const s of result.screenshots) assert.ok(/^data:image\/png;base64,/.test(s.data_url || ''), `screenshot ${s.name} has a data URL`);
      assert.deepEqual(result.syntax_checks.map((s) => [s.file, s.ok, s.method]),
        [['src/game.js', true, 'new Function'], ['src/util.mjs', true, 'import()'], ['src/main.mjs', true, 'import()']]);
      assert.ok(post && post.ok && post.status === 200, 'POST succeeded');
      const body = lastPost('/api/runs/r1/browser-results');
      assert.deepEqual(body, result, 'posted body equals window.__kyoraResult');
      assert.deepEqual(errors, [], 'no page errors');
      assert.equal(await page.locator('#results li .badge.pass').count(), 9);
      goodResult = result;
      await page.close();
    });

    await scenario('python collector accepts the real browser payload', async () => {
      assert.ok(goodResult, 'needs the good-game result');
      const home = path.join(TMP, 'home');
      const runDir = path.join(home, 'projects', 'p1', 'runs', 'r1');
      fs.mkdirSync(runDir, { recursive: true });
      const payloadFile = path.join(TMP, 'payload.json');
      fs.writeFileSync(payloadFile, JSON.stringify(goodResult));
      const code = [
        'import json, sys',
        'from pathlib import Path',
        'from kyora.pipeline.quality import BrowserResultsCollector, validate_browser_payload',
        'payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))',
        'c = BrowserResultsCollector(Path(sys.argv[2]))',
        'c.expect("r1", 1, Path(sys.argv[3]))',
        'ok = c.submit("r1", payload)',
        'stored = c.wait("r1", 1, 1)',
        'print(json.dumps({"problems": validate_browser_payload(payload), "ok": ok, "error": c.last_error("r1"),',
        '  "shots": [str(p) for p in c.screenshots("r1", 1)], "checks": [x.to_dict() for x in c.to_checks(stored)]}))',
      ].join('\n');
      const r = spawnSync('python3', ['-c', code, payloadFile, home, runDir], { cwd: REPO, encoding: 'utf8' });
      assert.equal(r.status, 0, `python failed: ${r.stderr}`);
      const out = JSON.parse(r.stdout.trim().split('\n').pop());
      assert.deepEqual(out.problems, []);
      assert.equal(out.ok, true, out.error);
      assert.equal(out.shots.length, 2);
      for (const s of out.shots) assert.equal(fs.readFileSync(s).subarray(1, 4).toString(), 'PNG');
      assert.ok(fs.existsSync(path.join(runDir, 'iter-1', 'browser.json')));
      const ids = out.checks.map((c) => c.id);
      assert.ok(ids.includes('at-01') && ids.includes('js_syntax:src/util.mjs'));
      assert.ok(out.checks.every((c) => c.status === 'pass'));
    });

    await scenario('broken game: failed syntax checks reported, harness does not crash', async () => {
      const { result, post, page } = await runHarness(context, base, '/runner/p1/v2?run=r2&iter=1&auto=1&post=1');
      assertSchema(result);
      const byFile = Object.fromEntries(result.syntax_checks.map((s) => [s.file, s]));
      assert.equal(byFile['src/game.js'].ok, false);
      assert.match(byFile['src/game.js'].error, /SyntaxError|Unexpected/);
      assert.equal(byFile['src/util.mjs'].ok, false);
      assert.match(byFile['src/util.mjs'].error, /SyntaxError|Unexpected/);
      assert.ok(result.summary.failed > 0, 'tests fail without window.Game');
      assert.equal(result.screenshots.length, 2);
      assert.ok(post && post.ok);
      assert.deepEqual(lastPost('/api/runs/r2/browser-results'), result);
      await page.close();
    });

    await scenario('missing tests file: harness:no-tests with summary total 0, posted to the slide endpoint', async () => {
      const { result, post, page } = await runHarness(context, base, '/runner/p1/v3?iter=2&auto=1&post=1');
      assertSchema(result);
      assert.equal(result.run_id, null);
      assert.equal(result.summary.total, 0);
      const nt = result.results.find((x) => x.id === 'harness:no-tests');
      assert.ok(nt && nt.status === 'fail' && /missing/.test(nt.error), JSON.stringify(result.results));
      assert.ok(post && post.ok && post.url === '/api/projects/p1/slides/3/browser-results');
      assert.deepEqual(lastPost('/api/projects/p1/slides/3/browser-results'), result);
      await page.close();
    });

    await scenario('preview not found: harness:load failure, still a complete message', async () => {
      const { result, page } = await runHarness(context, base, '/runner/p1/v9?run=r9&iter=0&auto=1&post=1');
      assertSchema(result);
      assert.equal(result.iteration, 0);
      assert.equal(result.summary.total, 0);
      const ids = result.results.map((x) => x.id);
      assert.ok(ids.includes('harness:load') && ids.includes('harness:no-tests'), ids.join(','));
      assert.ok(/404/.test(result.results.find((x) => x.id === 'harness:load').error));
      assert.deepEqual(lastPost('/api/runs/r9/browser-results'), result);
      await page.close();
    });

    await scenario('manual mode: no auto-run, "Run tests" button runs, post=0 posts nothing', async () => {
      const page = await context.newPage();
      await page.goto(base + '/runner/p1/v1?run=r6&iter=1', { waitUntil: 'load' });
      await page.waitForTimeout(400);
      assert.equal(await page.evaluate(() => window.__kyoraDone), undefined);
      await page.click('#run-again');
      await page.waitForFunction(() => window.__kyoraDone === true, null, { timeout: 60000, polling: 100 });
      const r = await page.evaluate(() => ({ result: window.__kyoraResult, post: window.__kyoraPost }));
      assert.equal(r.result.summary.failed, 0);
      assert.equal(r.post, null);
      assert.equal(lastPost('/api/runs/r6/browser-results'), null);
      assert.equal(await page.textContent('#run-again'), 'Run again');
      await page.close();
    });

    await scenario('headless_browser.mjs prints the result JSON and exits 0', async () => {
      const script = path.join(REPO, 'kyora', 'runners', 'headless_browser.mjs');
      // async spawn: the fixture server lives in this process and must keep answering
      const r = await spawnAsync(process.execPath, [script, `${base}/runner/p1/v1?run=r5&iter=3&auto=1&post=1`, '--timeout', '60000']);
      if (r.status !== 0) throw new Error(`exit ${r.status}: ${r.stderr}`);
      const result = JSON.parse(r.stdout.trim().split('\n').pop());
      assert.equal(result.type, 'kyora:test-result');
      assert.equal(result.iteration, 3);
      assert.ok(result.summary.passed >= 1);
    });

    if (REFS.v4) {
      await scenario('tests/fixtures/game-ok: all tests and syntax checks pass', async () => {
        const { result, page } = await runHarness(context, base, '/runner/p1/v4?run=r4&iter=1&auto=1&post=1');
        assertSchema(result);
        assert.ok(result.summary.total > 0, 'summary.total > 0');
        assert.ok(result.summary.passed >= 1);
        assert.deepEqual(result.results.filter((x) => x.status !== 'pass'), []);
        assert.ok(result.syntax_checks.every((s) => s.ok), JSON.stringify(result.syntax_checks));
        await page.close();
      });
    }
    if (REFS.v5) {
      await scenario('tests/fixtures/game-broken: failures reported without crashing', async () => {
        const { result, page } = await runHarness(context, base, '/runner/p1/v5?run=r7&iter=1&auto=1&post=1');
        assertSchema(result);
        const problems = result.results.filter((x) => x.status === 'fail').length + result.syntax_checks.filter((s) => !s.ok).length;
        assert.ok(problems > 0, 'something fails for the broken game');
        await page.close();
      });
    }
  } finally {
    await browser.close().catch(() => {});
    server.close();
    fs.rmSync(TMP, { recursive: true, force: true });
  }
  console.log(failures ? `# ${failures} scenario(s) FAILED` : '# all scenarios passed');
  return failures ? 1 : 0;
}

main().then((code) => { process.exitCode = code; }, (e) => {
  console.error(e && e.stack ? e.stack : e);
  try { server.close(); fs.rmSync(TMP, { recursive: true, force: true }); } catch (_) { /* ignore */ }
  process.exitCode = 1;
});
