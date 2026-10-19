#!/usr/bin/env python3
"""A real run of the whole Kyora pipeline WITHOUT Claude: live NVIDIA models + the Kyora agent engine.

Starts a real Kyora (temporary KYORA_HOME, both listeners) with the repository's config/ (the
committed NVIDIA keys), restricted to ``settings.engines.order = ["openai_agent"]`` so no Claude
engine can take part, and runs one project end to end:

  INTAKE/PLAN (Kimi role)  -> DESIGN (Nemotron role) -> BUILD (Kyora agent engine)
  -> TEST (static + Node + the browser harness, driven headless by Node Playwright when available)
  -> GATE (>= the configured pass rate) -> REVIEW/FIX ... -> FINALIZE (slide + deck)

and prints a compact live log: state changes, which model answered each role (and every model or
key fallback), engine turns, judge verdicts, test results and the gate. Nothing is invented: the
final state, the gate numbers and the models are read from Kyora's own events and run record.

  python scripts/live_pipeline_check.py --out live-pipeline [--timeout-min 75]

Writes report.json, events.jsonl (every event of the run), and when a slide exists the slide ZIP
and deck.html into --out. Exit code 0 only when the run ends ``complete`` (gate passed), else 1.
``--mock-nim`` swaps the NVIDIA endpoint for tests/mock_nim (plumbing check only: the mock does not
script an agent, so that run ends blocked or failed and the exit code is 1 by design).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from kyora.util.fs import atomic_write_json  # noqa: E402

PROMPT = ("A small arcade game: blocks fall from the top of a canvas and the player moves a paddle "
          "with arrow keys or touch-drag to catch them. One point per catch, three lives, a game-over "
          "screen with restart, and a high-score table kept in localStorage.")
TERMINAL = {"complete", "failed_gate", "failed", "cancelled"}
QUIET = {"model.delta", "engine.text", "engine.thinking", "engine.stderr", "stage.progress", "judge.request"}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(base: str, method: str, path: str, body=None, timeout: float = 60, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with _NO_PROXY.open(req, timeout=timeout) as resp:
        payload = resp.read()
    return payload if raw else json.loads(payload)


def headless_status() -> str:
    """Whether the TEST stage can drive the browser harness itself (Node + Playwright resolvable)."""
    from kyora.pipeline.quality import find_node
    node = find_node()
    if not node:
        return "no Node.js: browser tests need a browser to open the runner URL"
    import subprocess
    probe = ("const {createRequire}=require('module');const path=require('path');const cp=require('child_process');"
             "let roots=[process.env.NODE_PATH||''];try{roots.push(cp.execSync('npm root -g').toString().trim())}catch(e){};"
             "for(const r of roots.join(path.delimiter).split(path.delimiter).filter(Boolean)){try{"
             "createRequire(path.join(r,'x'))('playwright');console.log('ok '+r);process.exit(0)}catch(e){}}"
             "console.log('missing');")
    try:
        out = subprocess.run([node, "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        line = (out.stdout or "").strip().splitlines()[-1:] or ["missing"]
    except (OSError, subprocess.SubprocessError) as exc:
        return f"node probe failed: {exc}"
    return (f"Node + Playwright found ({line[0][3:]})" if line[0].startswith("ok")
            else "Node found but no Playwright: browser tests need a browser to open the runner URL")


def _failing_checks(home: Path, rid: str, out: Path) -> dict[str, list[dict]]:
    """Failing checks per iteration from the run's own gate.json files (copied to --out with the agent journal)."""
    found: dict[str, list[dict]] = {}
    for run_dir in home.glob(f"projects/*/runs/{rid}"):
        for gate_file in sorted(run_dir.glob("iter-*/gate.json")):
            try:
                gate = json.loads(gate_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            it = gate_file.parent.name.split("-", 1)[-1]
            shutil.copyfile(gate_file, out / f"gate-iter-{it}.json")
            found[it] = [{"id": c.get("id"), "group": c.get("group"), "name": str(c.get("name") or "")[:80],
                          "detail": " ".join(str(c.get("detail") or "").split())[:240]}
                         for c in gate.get("checks") or [] if c.get("status") == "fail"]
        for journal in run_dir.rglob("agent_turns.jsonl"):
            shutil.copyfile(journal, out / journal.name)
    return found


def _turn_stats(turns: list[dict]) -> dict:
    """Measured per-turn model latency of the agent engine (seconds): count, mean, median, max, total."""
    vals = sorted(float(t["total_ms"]) / 1000.0 for t in turns if isinstance(t.get("total_ms"), (int, float)))
    if not vals:
        return {}
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {"count": len(vals), "mean": round(sum(vals) / len(vals), 1), "median": round(median, 1),
            "max": round(vals[-1], 1), "total": round(sum(vals), 1)}


def prepare_config(dest: Path, mock_url: str | None, agent_model: str | None = None,
                   agent_fallbacks: list[str] | None = None, agent_thinking: str = "default") -> Path:
    cfg = dest / "config"
    shutil.copytree(REPO / "config", cfg)
    settings = json.loads((cfg / "settings.json").read_text(encoding="utf-8"))
    settings.setdefault("engines", {})["order"] = ["openai_agent"]  # no Claude engine can run here
    agent = settings.setdefault("agent_engine", {})
    agent["enabled"] = True
    if agent_model:
        agent["model"] = agent_model
    if agent_fallbacks is not None:
        agent["fallback_models"] = [m for m in agent_fallbacks if m and m != agent.get("model")]
    if agent_thinking in ("on", "off"):
        agent["thinking"] = agent_thinking == "on"
    settings.setdefault("quality", {}).update(browser_wait_s=900, auto_run_browser_tests=True)
    settings.setdefault("server", {})["workers"] = 1
    atomic_write_json(cfg / "settings.json", settings)
    if mock_url:
        keys = json.loads((cfg / "keys.json").read_text(encoding="utf-8"))
        keys["nvidia"]["base_url"] = mock_url
        keys["nvidia"]["nemotron"]["api_key"] = keys["nvidia"]["kimi"]["api_key"] = "test-key"
        atomic_write_json(cfg / "keys.json", keys)
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="live-pipeline", help="directory for report.json, events.jsonl, slide ZIP, deck")
    ap.add_argument("--timeout-min", type=float, default=75.0)
    ap.add_argument("--prompt", default=PROMPT)
    ap.add_argument("--mock-nim", action="store_true", help="plumbing check against tests/mock_nim (no network)")
    ap.add_argument("--agent-model", default=None, help="agent_engine.model for this run (default: settings.json)")
    ap.add_argument("--agent-fallbacks", default=None,
                    help="comma-separated agent_engine.fallback_models for this run ('' for none)")
    ap.add_argument("--agent-thinking", choices=("default", "on", "off"), default="default",
                    help="agent_engine.thinking for this run")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="kyora-live-pipeline-"))
    mock = None
    if args.mock_nim:
        from tests.mock_nim import MockNIM
        from tests.test_pipeline_e2e import make_handler
        mock = MockNIM(make_handler()).start()
    fallbacks = None if args.agent_fallbacks is None else [m.strip() for m in args.agent_fallbacks.split(",")]
    cfg_dir = prepare_config(tmp, mock.url if mock else None, args.agent_model, fallbacks, args.agent_thinking)
    os.environ["KYORA_HOME"] = str(tmp / "home")

    from kyora.app import App
    from kyora.config import Config
    from kyora.server import KyoraServer, Router

    app = App(Config.load(cfg_dir), home=tmp / "home")
    router = Router()
    app.register_routes(router)
    server = KyoraServer(app, "127.0.0.1", 0, router, preview_port=0)
    server.start()
    app.start(server.url, server.preview_url)
    agent = app.config.agent_engine
    print(f"Kyora {server.url} (games {server.preview_url}); engines.order={app.config.engines.order}")
    print(f"agent engine: {agent.model} (fallbacks {', '.join(agent.fallback_models) or 'none'}, thinking "
          f"{agent.thinking}) at {agent.base_url} with the {agent.key_label}; available: "
          f"{app.agent_engine.availability().to_dict()['available']}")
    print(f"headless browser tests: {headless_status()}")
    sys.stdout.flush()

    sub = app.bus.subscribe()
    events: list[dict] = []
    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mock": bool(mock),
                    "engines_order": app.config.engines.order, "agent_model": agent.model,
                    "agent_fallbacks": list(agent.fallback_models), "agent_thinking": agent.thinking}
    stage_ms: dict[str, float] = defaultdict(float)
    turn_log: list[dict] = []
    models_by_role: dict[str, Counter] = defaultdict(Counter)
    fallbacks: list[dict] = []
    judge = Counter()
    turns = 0
    final: dict = {}
    rid = None
    t0 = time.monotonic()
    try:
        pid = api(server.url, "POST", "/api/projects", {"name": "Live pipeline without Claude"})["project"]["id"]
        rid = api(server.url, "POST", f"/api/projects/{pid}/runs",
                  {"prompt": args.prompt, "options": {"engine_pref": "openai_agent"}})["run"]["id"]
        report.update(project_id=pid, run_id=rid)
        print(f"project {pid}, run {rid}")
        deadline = t0 + args.timeout_min * 60
        state = "created"
        while time.monotonic() < deadline:
            try:
                ev = sub.queue.get(timeout=5)
            except Exception:  # noqa: BLE001 - queue.Empty: check the clock and keep waiting
                continue
            if ev is None:
                break
            if ev.run_id not in (rid, None):
                continue
            d = ev.data or {}
            events.append(ev.to_dict())
            t = ev.type
            stamp = f"[{time.monotonic() - t0:7.1f}s]"
            if t == "model.metrics":
                models_by_role[str(d.get("role"))][str(d.get("model"))] += 1
            if t == "model.fallback":
                fallbacks.append(d)
            if t == "judge.verdict":
                judge[str(d.get("decision"))] += 1
            if t == "engine.tool_use":
                turns += 1
                print(f"{stamp} engine tool call #{turns}: {d.get('name')} {str(d.get('summary') or '')[:120]}")
            if t == "stage.progress" and isinstance(d.get("turn_metrics"), dict):
                tm = d["turn_metrics"]
                turn_log.append(tm)
                print(f"{stamp} {d.get('message')} [ttft {tm.get('ttft_ms')} ms, prompt {tm.get('prompt_tokens')} "
                      f"tokens, reasoning {tm.get('reasoning_chars')} chars, finish {tm.get('finish_reason')}]")
            if t == "stage.finished" and isinstance(d.get("ms"), (int, float)):
                stage_ms[str(d.get("stage"))] += float(d["ms"])
            if t in QUIET or t in ("engine.tool_use", "engine.tool_result", "judge.verdict", "model.request",
                                   "model.done"):
                continue
            brief = json.dumps(d, ensure_ascii=False)
            if t == "model.metrics":
                brief = (f"{d.get('role')} <- {d.get('model')}{' (fallback for ' + str(d.get('requested_model')) + ')' if d.get('fallback') else ''}"
                         f" {d.get('purpose') or ''} {d.get('total_ms')} ms, {d.get('completion_tokens')} tokens")
            print(f"{stamp} {t} {brief[:400]}")
            sys.stdout.flush()
            if t == "run.state":
                state = str(d.get("state"))
                if state in TERMINAL or state == "blocked":
                    time.sleep(2)
                    break
        else:
            print(f"TIMEOUT after {args.timeout_min} min in state {state}")
            report["timeout"] = True
            try:
                api(server.url, "POST", f"/api/runs/{rid}/cancel", {})
            except (urllib.error.URLError, OSError) as exc:
                print(f"cancel failed: {exc}")
        final = api(server.url, "GET", f"/api/runs/{rid}")["run"]
    except Exception as exc:  # noqa: BLE001 - report the plumbing failure, never a fake result
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"ERROR {report['error']}")
    finally:
        report["seconds"] = round(time.monotonic() - t0, 1)

    gate = (final.get("gate") or {}).get("final") or final.get("gate") or {}
    extra = final.get("extra") or {}
    report.update(
        final_state=final.get("state"), blocked_reason=final.get("blocked_reason"), error_state=final.get("error"),
        iteration=final.get("iteration"), result_slide=final.get("result_slide"), gate=gate,
        models_by_role={r: dict(c) for r, c in models_by_role.items()}, fallbacks=fallbacks,
        judge_verdicts=dict(judge), engine_tool_calls=turns, stage_seconds={k: round(v / 1000.0, 1)
                                                                            for k, v in stage_ms.items()},
        agent_turns=len(turn_log), agent_turn_seconds=_turn_stats(turn_log),
        engine_runs=extra.get("engine_runs"), engine_failovers=extra.get("engine_failovers"))
    with open(out / "events.jsonl", "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    if final.get("result_slide"):
        try:
            (out / f"slide-v{final['result_slide']}.zip").write_bytes(api(
                server.url, "GET", f"/api/projects/{report['project_id']}/slides/{final['result_slide']}/download", raw=True))
            (out / "deck.html").write_bytes(api(server.url, "GET", f"/projects/{report['project_id']}/deck", raw=True))
        except (urllib.error.URLError, OSError) as exc:
            print(f"artifact download failed: {exc}")
    failing = _failing_checks(tmp / "home", rid, out) if rid else {}
    report["failing_checks"] = failing
    atomic_write_json(out / "report.json", report)
    server.shutdown()
    app.stop()
    if mock:
        mock.stop()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n==== RESULT ====")
    print(f"final state: {report['final_state']}" + (f" ({report['blocked_reason']})" if report.get("blocked_reason") else ""))
    if gate:
        print(f"gate: {gate.get('passed')}/{gate.get('total')} = {gate.get('pass_rate')} (threshold {gate.get('threshold')}), "
              f"ok={gate.get('ok')}")
    for it, fails in sorted(failing.items()):
        print(f"iteration {it}: {len(fails)} failing check(s)")
        for f in fails[:25]:
            print(f"  - [{f['group']}] {f['id']}: {f['name']} - {f['detail']}")
    for role, counter in sorted(models_by_role.items()):
        print(f"{role} answered by: " + ", ".join(f"{m} x{n}" for m, n in counter.most_common()))
    print(f"model/key fallbacks: {len(fallbacks)}; judge verdicts: {dict(judge)}; agent tool calls: {turns}; "
          f"duration {report['seconds']} s")
    print(f"stage seconds: {report['stage_seconds']}")
    print(f"agent turns: {report['agent_turns']}; per-turn seconds: {report['agent_turn_seconds']}")
    ok = report.get("final_state") == "complete"
    print("LIVE PIPELINE WITHOUT CLAUDE: " + ("COMPLETE (gate passed)" if ok else "NOT COMPLETE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
