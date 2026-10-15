#!/usr/bin/env node
/*
 * Kyora Node helper (optional; needs Node 18+). Standalone, no dependencies.
 *
 *   node node_check.mjs check <file...>
 *       one JSON line per file: {"file", "ok", "error"} (node --check; .mjs parsed as a module)
 *   node node_check.mjs test <workspace>
 *       runs `node --test --test-reporter=tap tests/logic-tests.mjs` in <workspace> and prints
 *       {"checks": [{"id", "name", "ok", "status", "detail"}], "exit_code", "skipped"?}
 *
 * Always exits 0 when the command itself was understood (results are in the JSON);
 * exits 2 on a usage error.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import path from 'node:path';

const LOGIC_TESTS = 'tests/logic-tests.mjs';
const ACCEPTANCE_ID = /^([A-Za-z]{1,8}-\d+)(?=$|[\s:.,)\]-])/;

function condense(stderr, file) {
  const lines = String(stderr || '').split(/\r?\n/);
  let where = '';
  for (let i = 0; i < lines.length; i += 1) {
    const m = /:(\d+)\s*$/.exec(lines[i]);
    if (m && lines[i].indexOf(file) !== -1) {
      where = ` (line ${m[1]}: ${(lines[i + 1] || '').trim().slice(0, 120)})`;
      break;
    }
  }
  const msg = lines.find((l) => /^\s*\w*Error\b.*:/.test(l));
  const text = (msg ? msg.trim() : lines.filter((l) => l.trim() && !l.startsWith('Node.js')).join(' ').trim()) || 'node --check failed';
  return (text + where).slice(0, 600);
}

function check(files) {
  for (const file of files) {
    let rec;
    if (!existsSync(file) || !statSync(file).isFile()) {
      rec = { file, ok: false, error: 'file not found' };
    } else {
      const r = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8', timeout: 60000 });
      if (r.error) rec = { file, ok: false, error: `could not run node --check: ${r.error.message}` };
      else if (r.status === 0) rec = { file, ok: true, error: null };
      else rec = { file, ok: false, error: condense(r.stderr, file) };
    }
    process.stdout.write(JSON.stringify(rec) + '\n');
  }
}

function yamlField(lines, key) {
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const t = line.trim();
    if (!t.startsWith(key + ':')) continue;
    const indent = line.length - line.trimStart().length;
    let value = t.slice(key.length + 1).trim();
    if (['|-', '|', '>-', '>'].includes(value)) {
      const block = [];
      for (const nxt of lines.slice(i + 1)) {
        if (nxt.trim() && nxt.length - nxt.trimStart().length <= indent) break;
        block.push(nxt.trim());
      }
      return block.join('\n').trim();
    }
    if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === "'" || value[0] === '"')) {
      value = value.slice(1, -1).replace(/''/g, "'");
    }
    return value;
  }
  return null;
}

/** Parse node's TAP output into leaf tests (same rules as kyora/pipeline/quality.py parse_tap). */
export function parseTap(text) {
  const lines = String(text || '').split(/\r?\n/);
  const entries = [];
  const re = /^(\s*)(not ok|ok)\s+(\d+)(?:\s*-\s*(.*))?$/;
  let i = 0;
  while (i < lines.length) {
    const m = re.exec(lines[i]);
    if (!m) { i += 1; continue; }
    let rest = m[4] || '';
    let directive = '';
    const d = /(?<!\\)#\s*(SKIP|TODO)\b.*$/i.exec(rest);
    if (d) { directive = d[1].toUpperCase(); rest = rest.slice(0, d.index).trimEnd(); }
    const name = rest.trim().replace(/\\#/g, '#').replace(/\\\\/g, '\\') || `test ${m[3]}`;
    const ok = m[2] === 'ok';
    const yaml = [];
    let j = i + 1;
    if (j < lines.length && lines[j].trim() === '---') {
      j += 1;
      while (j < lines.length && lines[j].trim() !== '...') { yaml.push(lines[j]); j += 1; }
      j += 1;
    }
    const status = directive ? 'skip' : ok ? 'pass' : 'fail';
    const dur = parseFloat(yamlField(yaml, 'duration_ms'));
    entries.push({ name, status, indent: m[1].replace(/\t/g, '    ').length, error: ok ? null : yamlField(yaml, 'error'),
      failureType: yamlField(yaml, 'failureType'), durationMs: Number.isFinite(dur) ? dur : null });
    i = j > i + 1 ? j : i + 1;
  }
  return entries.filter((e, k) => {
    const isParent = k > 0 && entries[k - 1].indent > e.indent;
    return !isParent || (e.status === 'fail' && e.failureType && e.failureType !== 'subtestsFailed');
  });
}

function logicTests(workspace) {
  const ws = path.resolve(workspace);
  if (!existsSync(path.join(ws, LOGIC_TESTS))) {
    process.stdout.write(JSON.stringify({ checks: [], skipped: `${LOGIC_TESTS} not present` }) + '\n');
    return;
  }
  const r = spawnSync(process.execPath, ['--test', '--test-reporter=tap', LOGIC_TESTS], {
    cwd: ws, encoding: 'utf8', timeout: 120000, env: { ...process.env, NO_COLOR: '1', FORCE_COLOR: '0' },
  });
  const leaves = parseTap(r.stdout);
  const seen = Object.create(null);
  const checks = leaves.map((e, n) => {
    const m = ACCEPTANCE_ID.exec(e.name);
    let id = m ? m[1] : `lt-${n + 1}`;
    if (seen[id]) { seen[id] += 1; id = `${id}#${seen[id]}`; } else seen[id] = 1;
    const detail = e.status === 'fail' ? String(e.error || 'failed').slice(0, 600)
      : e.status === 'skip' ? 'skipped/todo' : e.durationMs != null ? `${e.durationMs.toFixed(1)} ms` : 'ok';
    return { id, name: e.name, ok: e.status === 'pass', status: e.status, detail };
  });
  const out = { checks, exit_code: r.status };
  if (r.error) out.error = `could not run node --test: ${r.error.message}`;
  else if (r.signal) out.error = `node --test ended by ${r.signal} (timeout 120 s?)`;
  else if (!leaves.length) out.error = `no TAP results: ${String(r.stderr || r.stdout).trim().split(/\r?\n/).slice(-8).join(' ').slice(0, 600)}`;
  process.stdout.write(JSON.stringify(out) + '\n');
}

function main(argv) {
  const [cmd, ...rest] = argv;
  if (cmd === 'check' && rest.length) { check(rest); return 0; }
  if (cmd === 'test' && rest.length === 1) { logicTests(rest[0]); return 0; }
  process.stderr.write('usage: node node_check.mjs check <file...> | test <workspace>\n');
  return 2;
}

if (process.argv[1] && path.basename(process.argv[1]) === 'node_check.mjs') {
  process.exitCode = main(process.argv.slice(2));
}
