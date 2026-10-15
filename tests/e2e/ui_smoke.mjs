// Kyora UI smoke test (Node 18+ and Playwright).
//
//   NODE_PATH=$(npm root -g) PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/e2e/ui_smoke.mjs
//
// Spawns `python3 tests/e2e/ui_driver.py` (a real Kyora server on 127.0.0.1 with a temporary
// config + KYORA_HOME), opens the UI at phone (390x844) and desktop (1280x800) sizes and checks:
// no page errors and no console errors except expected 404/405 resource messages (backend
// modules may be missing), every tab shows its section, the capability strip + engine banner
// match /api/status, the settings form masks secrets, the hub-link hash flow updates
// /api/settings, and after the driver publishes a scripted run the chart, metric tiles, gate
// gauge (94) and judge counters (1 allowed / 1 denied) reflect it.
// Optional: KYORA_SMOKE_SHOTS=<dir> saves a screenshot of every view.
import { spawn, execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync, existsSync, readdirSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const VIEWS = ['studio', 'pipeline', 'preview', 'slides', 'presenter', 'selfcheck', 'bench', 'settings'];
const ALLOWED_CONSOLE = /Failed to load resource: the server responded with a status of (404|405)\b/;
const SHOTS = process.env.KYORA_SMOKE_SHOTS || '';
const T0 = Date.now();

const failures = [];
let passes = 0;
function log(msg) { console.log(`[${((Date.now() - T0) / 1000).toFixed(1).padStart(5)}s] ${msg}`); }
async function check(name, fn) {
  try {
    await fn();
    passes += 1;
    log(`ok   ${name}`);
  } catch (err) {
    failures.push(`${name}: ${err && err.message ? err.message.split('\n')[0] : err}`);
    log(`FAIL ${name}: ${err && err.message ? err.message.split('\n').slice(0, 3).join(' | ') : err}`);
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

/* ------------------------------------------------------------------ playwright */
function loadPlaywright() {
  const req = createRequire(import.meta.url);
  try { return req('playwright'); } catch (_) { /* fall through to the global npm root */ }
  const root = execSync('npm root -g', { encoding: 'utf8' }).trim();
  return req(path.join(root, 'playwright'));
}

async function launch(chromium) {
  try {
    return await chromium.launch({ headless: true });
  } catch (err) {
    const base = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
    const dirs = existsSync(base) ? readdirSync(base).filter((d) => /^chromium-\d+/.test(d)) : [];
    for (const d of dirs) {
      const exe = path.join(base, d, 'chrome-linux', 'chrome');
      if (existsSync(exe)) return chromium.launch({ headless: true, executablePath: exe });
    }
    throw err;
  }
}

/* ------------------------------------------------------------------ driver */
function startDriver() {
  const child = spawn('python3', [path.join('tests', 'e2e', 'ui_driver.py'), '0'], { cwd: REPO, stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = [];
  const waiters = [];
  let buf = '';
  let stderr = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      lines.push(line);
      for (const w of waiters.slice()) if (w.re.test(line)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(line); }
    }
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (c) => { stderr += c; });
  function waitLine(re, ms) {
    const hit = lines.find((l) => re.test(l));
    if (hit) return Promise.resolve(hit);
    return new Promise((resolve, reject) => {
      const w = { re, resolve };
      waiters.push(w);
      setTimeout(() => {
        const i = waiters.indexOf(w);
        if (i >= 0) { waiters.splice(i, 1); reject(new Error(`driver did not print ${re} within ${ms} ms; stderr tail: ${stderr.slice(-600)}`)); }
      }, ms);
    });
  }
  return { child, lines, waitLine, stderr: () => stderr, send: (cmd) => child.stdin.write(cmd + '\n') };
}

/* ------------------------------------------------------------------ helpers */
function secretValues() {
  try {
    const keys = JSON.parse(readFileSync(path.join(REPO, 'config', 'keys.json'), 'utf8'));
    const out = [];
    const walk = (o, p) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (v && typeof v === 'object') walk(v, p + k + '.');
        else if (typeof v === 'string' && /(api_key|token)$/.test(k) && v.length > 8) out.push(v);
      }
    };
    walk(keys, '');
    return out;
  } catch (_) { return []; }
}

function watchPage(page, label) {
  const problems = [];
  page.on('pageerror', (err) => problems.push(`[${label}] pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const t = msg.text();
    if (ALLOWED_CONSOLE.test(t)) return;
    problems.push(`[${label}] console.error: ${t}`);
  });
  return problems;
}

async function gotoView(page, view) {
  await page.click(`#nav a[data-view="${view}"]`);
  await page.waitForFunction((v) => {
    const sec = document.querySelector(`section.view[data-view="${v}"]`);
    return sec && !sec.hidden && sec.offsetParent !== null && location.hash === '#/' + v;
  }, view, { timeout: 5000 });
}

async function assertOnlyVisible(page, view) {
  const res = await page.evaluate((v) => {
    const secs = Array.from(document.querySelectorAll('section.view[data-view]'));
    const visible = secs.filter((s) => !s.hidden && getComputedStyle(s).display !== 'none').map((s) => s.dataset.view);
    const current = Array.from(document.querySelectorAll('#nav a[aria-current="page"]')).map((a) => a.dataset.view);
    return { visible, current, overflow: document.documentElement.scrollWidth - window.innerWidth };
  }, view);
  assert(res.visible.length === 1 && res.visible[0] === view, `visible sections ${JSON.stringify(res.visible)} for ${view}`);
  assert(res.current.length === 1 && res.current[0] === view, `nav aria-current ${JSON.stringify(res.current)} for ${view}`);
  return res;
}

async function shot(page, name) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: false });
}

async function checkBoot(page, label) {
  await page.waitForFunction(() => {
    const b = document.getElementById('engine-banner');
    return b && b.getAttribute('data-tone') !== 'unknown' && document.querySelectorAll('#cap-strip .chip').length >= 5;
  }, null, { timeout: 30000 });
  const status = await page.evaluate(() => fetch('/api/status').then((r) => r.json()));
  const ui = await page.evaluate(() => ({
    tone: document.getElementById('engine-banner').getAttribute('data-tone'),
    title: document.querySelector('#engine-banner .eb-title').textContent,
    chips: Array.from(document.querySelectorAll('#cap-strip .chip')).map((c) => c.textContent)
  }));
  const sel = status.selected_engine;
  if (sel && sel.available) {
    assert(ui.tone === 'ok' && /^Code engine:/.test(ui.title), `[${label}] banner should show the engine, got ${ui.tone} "${ui.title}"`);
  } else {
    assert(ui.tone === 'danger' && ui.title === 'BLOCKED: no code engine', `[${label}] banner should be BLOCKED, got ${ui.tone} "${ui.title}"`);
  }
  const cc = status.engines && status.engines.claude_code;
  const ccChip = ui.chips.find((c) => /^Claude Code/.test(c)) || '';
  assert(cc && (cc.available ? /ready/.test(ccChip) : /unavailable|off/.test(ccChip)), `[${label}] Claude Code chip "${ccChip}" disagrees with /api/status`);
  const nem = ui.chips.find((c) => /^Nemotron-3-Ultra/.test(c)) || '';
  assert(status.components.nemotron ? /configured/.test(nem) : /missing/.test(nem), `[${label}] Nemotron chip "${nem}" disagrees with /api/status`);
  return status;
}

async function checkSettings(page, label, secrets) {
  await gotoView(page, 'settings');
  await page.waitForSelector('#settings-root input[data-p]', { timeout: 10000 });
  const info = await page.evaluate(() => ({
    fields: document.querySelectorAll('#settings-root [data-p]').length,
    secrets: Array.from(document.querySelectorAll('#settings-root input[data-type="secret"]')).map((i) => ({ p: i.dataset.p, type: i.type, value: i.value, ph: i.placeholder })),
    workersDisabled: !!(document.querySelector('[data-p="server.workers"]') || {}).disabled,
    portEditable: !(document.querySelector('[data-p="server.port"]') || { disabled: true }).disabled,
    html: document.documentElement.outerHTML,
    values: Array.from(document.querySelectorAll('input, textarea')).map((i) => i.value).join('\n')
  }));
  assert(info.fields >= 25, `[${label}] only ${info.fields} settings fields`);
  assert(info.secrets.length === 4, `[${label}] expected 4 secret fields, got ${info.secrets.length}`);
  for (const s of info.secrets) {
    assert(s.type === 'password' && s.value === '', `[${label}] secret ${s.p} is not an empty password input`);
    assert(s.ph === 'not set' || s.ph.indexOf('…') >= 0, `[${label}] secret ${s.p} placeholder is not masked: ${s.ph.slice(0, 12)}...`);
  }
  for (const sec of secrets) {
    assert(info.html.indexOf(sec) < 0 && info.values.indexOf(sec) < 0, `[${label}] a full secret from keys.json is visible in the DOM`);
  }
  assert(!/nvapi-[A-Za-z0-9_-]{16,}/.test(info.html), `[${label}] an unmasked nvapi- key appears in the DOM`);
  assert(info.workersDisabled, `[${label}] non-patchable server.workers should be read-only`);
  assert(info.portEditable, `[${label}] patchable server.port should be editable`);
}

async function checkHubLink(page, label, hashFor, hubUrl, token) {
  await page.evaluate((h) => { location.hash = h; }, hashFor(hubUrl, token));
  await page.waitForSelector('.modal', { timeout: 5000 });
  const modalText = await page.textContent('.modal');
  assert(modalText.indexOf(hubUrl) >= 0, `[${label}] confirm modal does not name the hub URL`);
  await page.click('.modal .modal-actions .btn.primary');
  await page.waitForFunction(() => Array.from(document.querySelectorAll('#toasts .toast')).some((t) => /Linked to the Kyora Hub/.test(t.textContent)), null, { timeout: 8000 });
  const s = await page.evaluate(() => fetch('/api/settings').then((r) => r.json()));
  assert(s.keys.hub.url === hubUrl, `[${label}] /api/settings hub.url is ${s.keys.hub.url}`);
  assert(s.keys.hub.token && s.keys.hub.token !== token && s.keys.hub.token.indexOf('…') >= 0, `[${label}] hub token not stored masked`);
  const hash = await page.evaluate(() => location.hash);
  assert(hash === '#/selfcheck', `[${label}] hash not cleared (${hash.slice(0, 20)})`);
  assert(await page.isVisible('#view-selfcheck'), `[${label}] self-check view not shown after the hub link`);
  // restore so later status calls do not try the fake hub
  await page.evaluate(() => fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keys: { hub: { url: '', token: '' } } }) }).then((r) => r.json()));
}

async function checkPipeline(page, label) {
  await gotoView(page, 'pipeline');
  await page.waitForFunction(() => {
    const n = document.querySelector('#pipe-chart [data-stage="finalize"]');
    return n && n.getAttribute('data-state') === 'running';
  }, null, { timeout: 20000 });
  const r = await page.evaluate(() => {
    const st = {};
    document.querySelectorAll('#pipe-chart [data-stage]').forEach((n) => { st[n.dataset.stage] = n.dataset.state; });
    const big = (sys) => (document.querySelector(`.tile[data-sys="${sys}"] .tile-big`) || {}).textContent || '';
    const counter = (k) => ((document.querySelector(`#judge-counters [data-counter="${k}"] b`) || {}).textContent || '').trim();
    return {
      stages: st,
      kimi: big('kimi'), nemotron: big('nemotron'), claude: big('claude'),
      claudeModel: (document.querySelector('.tile[data-sys="claude"] .tile-model') || {}).textContent || '',
      gauge: (document.querySelector('#gate-gauge .g-num') || {}).textContent || '',
      allowed: counter('allowed'), denied: counter('denied'), unavailable: counter('unavailable'),
      iter: (document.getElementById('iter-counter') || {}).textContent || '',
      logLines: document.querySelectorAll('#log .log-line').length,
      runSelect: (document.getElementById('run-select') || {}).value
    };
  });
  for (const s of ['intake', 'plan', 'design', 'build', 'test', 'gate']) assert(r.stages[s] === 'done', `[${label}] stage ${s} is ${r.stages[s]}`);
  assert(r.stages.review === 'idle' && r.stages.fix === 'idle', `[${label}] loop stages should be idle: ${r.stages.review}/${r.stages.fix}`);
  assert(/131/.test(r.kimi) && /tok\/s/.test(r.kimi), `[${label}] Kimi tile shows "${r.kimi}"`);
  assert(/88\.4/.test(r.nemotron), `[${label}] Nemotron tile shows "${r.nemotron}"`);
  assert(/31/.test(r.claude), `[${label}] Claude tile shows "${r.claude}"`);
  assert(/claude-opus-5-5/.test(r.claudeModel), `[${label}] Claude tile model "${r.claudeModel}"`);
  assert(/94/.test(r.gauge), `[${label}] gauge shows "${r.gauge}"`);
  assert(r.allowed === '1' && r.denied === '1' && r.unavailable === '0', `[${label}] judge counters ${r.allowed}/${r.denied}/${r.unavailable}`);
  assert(r.iter === 'iteration 1', `[${label}] iteration counter "${r.iter}"`);
  assert(r.logLines >= 15, `[${label}] only ${r.logLines} log lines`);
  assert(r.runSelect === 'r_demo', `[${label}] run select is "${r.runSelect}"`);
  // log filters hide and show categories
  await page.click('#log-filters button[data-cat="judge"]');
  const hidden = await page.evaluate(() => Array.from(document.querySelectorAll('#log .log-line[data-cat="judge"]')).every((n) => n.hidden));
  await page.click('#log-filters button[data-cat="judge"]');
  const shown = await page.evaluate(() => Array.from(document.querySelectorAll('#log .log-line[data-cat="judge"]')).some((n) => !n.hidden));
  assert(hidden && shown, `[${label}] judge log filter toggle failed`);
}

async function checkOtherViews(page, label, wantNarration) {
  await gotoView(page, 'presenter');
  await page.waitForFunction(() => /Tokkio not deployed|Tokkio/.test((document.getElementById('tokkio-main') || {}).textContent || '') &&
    !document.querySelector('#tokkio-main .spinner'), null, { timeout: 15000 });
  const pres = await page.evaluate(() => ({
    main: document.getElementById('tokkio-main').textContent,
    narr: Array.from(document.querySelectorAll('#tokkio-narrations .narration')).map((n) => n.textContent),
    views: document.querySelectorAll('#tokkio-views .tokkio-view').length,
    frames: document.querySelectorAll('#presenter-root iframe').length,
    canvases: document.querySelectorAll('#presenter-root canvas, #presenter-root video').length
  }));
  assert(/Tokkio not deployed/.test(pres.main), `[${label}] presenter should explain that Tokkio is not deployed`);
  assert(pres.frames === 0 && pres.canvases === 0, `[${label}] presenter must not draw an avatar without Tokkio`);
  if (wantNarration) {
    assert(pres.narr.some((t) => /Gate passed at 94 percent/.test(t)), `[${label}] narration card missing`);
    assert(pres.views >= 1, `[${label}] tokkio.view card missing`);
  }
  await gotoView(page, 'slides');
  await page.waitForFunction(() => !document.getElementById('slides-empty').hidden || document.querySelectorAll('#strip .slide-card').length > 0, null, { timeout: 8000 });
  await gotoView(page, 'preview');
  await page.waitForFunction(() => !!document.getElementById('preview-ref'), null, { timeout: 5000 });
  await gotoView(page, 'bench');
  await page.waitForFunction(() => (document.getElementById('bench-tasks').textContent || '').trim().length > 0 && !document.querySelector('#bench-tasks .spinner') &&
    !document.querySelector('#bench-score .spinner'), null, { timeout: 10000 });
  const benchText = await page.textContent('#bench-score');
  assert(!/certified/i.test(benchText) || /not certified|not available|no measurement/i.test(benchText), `[${label}] bench claims a certification without a measurement: ${benchText.slice(0, 80)}`);
  assert(await page.locator('#bench-score img[src*="badge-certified"]').count() === 0, `[${label}] certified badge shown without a measured certified run`);
  await gotoView(page, 'selfcheck');
  await page.waitForFunction(() => document.querySelector('#selfcheck-table table, #selfcheck-table .empty') && document.querySelector('#platform-kv dt'), null, { timeout: 30000 });
}

async function runViewport(browser, driver, opts) {
  const { label, viewport, mobile, publish, secrets } = opts;
  const context = await browser.newContext({ viewport, isMobile: !!mobile, hasTouch: !!mobile, deviceScaleFactor: mobile ? 2 : 1 });
  const page = await context.newPage();
  const problems = watchPage(page, label);
  await page.goto(driver.url, { waitUntil: 'domcontentloaded' });

  await check(`[${label}] boot: capability strip + engine banner from /api/status`, () => checkBoot(page, label));
  await check(`[${label}] every tab shows only its section`, async () => {
    for (const v of VIEWS) {
      await gotoView(page, v);
      const res = await assertOnlyVisible(page, v);
      if (mobile) assert(res.overflow <= 1, `horizontal page scroll of ${res.overflow}px on ${v}`);
      await shot(page, `${label}-${v}`);
    }
  });
  await check(`[${label}] settings form masks secrets`, () => checkSettings(page, label, secrets));
  if (opts.hub) await check(`[${label}] hub-link hash flow updates /api/settings`, () => checkHubLink(page, label, opts.hub.hash, opts.hub.url, opts.hub.token));
  if (publish) {
    await gotoView(page, 'pipeline');
    driver.send('PUBLISH');
    await driver.waitLine(/^PUBLISHED r_demo /, 15000);
  }
  await check(`[${label}] pipeline reflects the scripted run (chart, tiles, gauge 94, judge 1/1)`, () => checkPipeline(page, label));
  await shot(page, `${label}-pipeline-run`);
  await check(`[${label}] presenter, slides, preview, bench and self-check degrade gracefully`, () => checkOtherViews(page, label, publish));
  await shot(page, `${label}-selfcheck-final`);
  await check(`[${label}] no page errors and no unexpected console errors`, async () => {
    assert(problems.length === 0, problems.slice(0, 5).join(' || '));
  });
  await context.close();
}

async function main() {
  const { chromium } = loadPlaywright();
  const driver = startDriver();
  let browser = null;
  try {
    const urlLine = await driver.waitLine(/^URL /, 60000);
    driver.url = urlLine.slice(4).trim();
    const missing = driver.lines.find((l) => l.startsWith('MISSING ')) || 'MISSING none';
    log(`driver at ${driver.url} (${missing})`);
    browser = await launch(chromium);
    const secrets = secretValues();
    await runViewport(browser, driver, {
      label: 'desktop', viewport: { width: 1280, height: 800 }, publish: true, secrets,
      hub: { url: 'http://127.0.0.1:9', token: 'smoke-token-0123456789', hash: (u, t) => '#hub=' + encodeURIComponent(u) + '&token=' + encodeURIComponent(t) }
    });
    await runViewport(browser, driver, {
      label: 'phone', viewport: { width: 390, height: 844 }, mobile: true, publish: false, secrets,
      hub: { url: 'http://127.0.0.1:10', token: 'phone-token-abcdef123456', hash: (u, t) => '#/selfcheck?hub=' + encodeURIComponent(u) + '&token=' + encodeURIComponent(t) }
    });
  } catch (err) {
    failures.push(`fatal: ${err && err.stack ? err.stack.split('\n').slice(0, 4).join(' | ') : err}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
    try { driver.child.stdin.end(); } catch (_) { /* already closed */ }
    await new Promise((resolve) => {
      const t = setTimeout(() => { try { driver.child.kill('SIGKILL'); } catch (_) { /* gone */ } resolve(); }, 8000);
      driver.child.on('exit', () => { clearTimeout(t); resolve(); });
    });
  }
  log(`${passes} checks passed, ${failures.length} failed`);
  if (failures.length) {
    console.log('\nFAILURES:\n' + failures.map((f) => ' - ' + f).join('\n'));
    const tail = driver.stderr().split('\n').filter((l) => !/^WARNING kyora\.app: (component|routes)/.test(l)).slice(-15).join('\n');
    if (tail.trim()) console.log('\ndriver stderr tail:\n' + tail);
    process.exit(1);
  }
  console.log('UI SMOKE OK');
}

main();
