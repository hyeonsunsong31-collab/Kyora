#!/usr/bin/env node
/*
 * OPTIONAL headless driver for the Kyora browser test harness.
 *
 *   node headless_browser.mjs <runnerUrl> [--timeout 120000] [--viewport 390x760] [--verbose]
 *
 * Opens <runnerUrl> (e.g. http://127.0.0.1:8765/runner/p_1/run-r_1?run=r_1&iter=1&auto=1&post=1)
 * in Playwright Chromium, waits until the harness finished (window.__kyoraDone, including
 * the POST when post=1) and prints window.__kyoraResult as one JSON line on stdout
 * (screenshots included). Exit 0 when a result arrived, 1 otherwise (reason on stderr and
 * as {"error": ...} on stdout), 2 on a usage error.
 *
 * Kyora never requires this script: any phone or desktop browser that opens the runner
 * URL produces the same result. It needs Node 18+ and the `playwright` package, resolved
 * from the script directory, NODE_PATH or the global npm root; browsers come from
 * PLAYWRIGHT_BROWSERS_PATH (default /opt/pw-browsers when that exists).
 */
import { execSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

function parseArgs(argv) {
  const out = { url: null, timeout: 120000, viewport: [390, 760], verbose: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--timeout') out.timeout = Number(argv[++i]);
    else if (a.startsWith('--timeout=')) out.timeout = Number(a.slice(10));
    else if (a === '--viewport' || a.startsWith('--viewport=')) {
      const v = a.includes('=') ? a.slice(11) : argv[++i];
      const m = /^(\d+)x(\d+)$/.exec(v || '');
      if (m) out.viewport = [Number(m[1]), Number(m[2])];
    } else if (a === '--verbose') out.verbose = true;
    else if (!out.url) out.url = a;
  }
  if (!Number.isFinite(out.timeout) || out.timeout <= 0) out.timeout = 120000;
  return out;
}

export function loadPlaywright() {
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && existsSync('/opt/pw-browsers')) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
  }
  const req = createRequire(import.meta.url);
  try { return req('playwright'); } catch (e) { /* try other roots */ }
  const roots = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
  try {
    roots.push(execSync('npm root -g', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000 }).trim());
  } catch (e) { /* npm not available */ }
  for (const root of roots) {
    for (const name of ['playwright', 'playwright-core']) {
      try { return req(path.join(root, name)); } catch (e) { /* next */ }
    }
  }
  return null;
}

export async function launchChromium(pw) {
  const opts = { headless: true, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'] };
  let firstError;
  try {
    return await pw.chromium.launch(opts);
  } catch (e) {
    firstError = e;
  }
  const bases = [process.env.PLAYWRIGHT_BROWSERS_PATH, '/opt/pw-browsers'].filter(Boolean);
  for (const base of bases) {
    let dirs = [];
    try { dirs = readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse(); } catch (e) { continue; }
    for (const d of dirs) {
      for (const sub of ['chrome-linux/chrome', 'chrome-linux64/chrome']) {
        const exe = path.join(base, d, sub);
        if (!existsSync(exe)) continue;
        try { return await pw.chromium.launch({ ...opts, executablePath: exe }); } catch (e) { /* next */ }
      }
    }
  }
  throw firstError;
}

function fail(reason, code = 1) {
  process.stderr.write(`headless_browser: ${reason}\n`);
  process.stdout.write(JSON.stringify({ error: reason }) + '\n');
  return code;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url) {
    process.stderr.write('usage: node headless_browser.mjs <runnerUrl> [--timeout 120000] [--viewport 390x760] [--verbose]\n');
    return 2;
  }
  const pw = loadPlaywright();
  if (!pw) return fail('the playwright package is not installed (npm i -g playwright); open the runner URL in any browser instead');
  let browser;
  try {
    browser = await launchChromium(pw);
  } catch (e) {
    return fail(`could not launch Chromium: ${e && e.message ? e.message.split('\n')[0] : e}`);
  }
  try {
    const context = await browser.newContext({ viewport: { width: args.viewport[0], height: args.viewport[1] }, hasTouch: true });
    const page = await context.newPage();
    if (args.verbose) {
      page.on('console', (m) => process.stderr.write(`[console.${m.type()}] ${m.text()}\n`));
      page.on('pageerror', (e) => process.stderr.write(`[pageerror] ${e.message}\n`));
    }
    const resp = await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: Math.min(args.timeout, 60000) });
    if (resp && !resp.ok()) return fail(`runner page answered HTTP ${resp.status()}`);
    try {
      await page.waitForFunction(() => window.__kyoraDone === true, null, { timeout: args.timeout, polling: 250 });
    } catch (e) {
      const partial = await page.evaluate(() => window.__kyoraResult || null).catch(() => null);
      if (!partial) return fail(`no result within ${args.timeout} ms`);
    }
    const out = await page.evaluate(() => ({ result: window.__kyoraResult || null, post: window.__kyoraPost || null }));
    if (!out.result) return fail('the harness finished without a result (see the runner page)');
    if (out.post && !out.post.ok) process.stderr.write(`headless_browser: posting results failed: ${out.post.error}\n`);
    process.stdout.write(JSON.stringify(out.result) + '\n');
    return 0;
  } catch (e) {
    return fail(e && e.message ? e.message.split('\n')[0] : String(e));
  } finally {
    await browser.close().catch(() => {});
  }
}

if (process.argv[1] && path.basename(process.argv[1]) === 'headless_browser.mjs') {
  main().then((code) => { process.exitCode = code; }, (e) => { process.exitCode = fail(String(e)); });
}
