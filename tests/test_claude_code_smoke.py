"""REAL Claude Code CLI smoke test (opt-in; uses the logged-in Claude subscription).

Skipped unless ``KYORA_REAL_CLAUDE=1`` and ``claude`` is on PATH. It runs the
real engine path end to end without any model of Kyora's own:

* the real ``JudgeService`` (``kimi=None``) behind ``POST /api/judge`` on a real
  ``KyoraServer`` bound to 127.0.0.1, wrapped so that any write of a file named
  ``forbidden.txt`` is denied (everything else goes through the normal rules);
* ``ClaudeCodeEngine`` with the default model chain (``claude-fable-5-1`` ->
  ``claude-opus-5-5``, ``claude-sonnet-5-5``), ``--effort max``, ``--max-turns 8``,
  the per-run ``--settings`` file and the installed PreToolUse hook;
* a temp git-initialised workspace and a two-file prompt.

It asserts that ``allowed.txt`` was written, ``forbidden.txt`` was blocked by the
hook (exit 2), ``stream.jsonl`` holds the ``init`` and ``result`` lines and the
judge journal holds the deny. Optional env:

* ``KYORA_SMOKE_KEEP=<dir>``: copy ``engine/*``, the settings file and a
  ``report.json`` (observed model, duration, retries) there for inspection.

Run (at most a couple of times; it spends subscription usage):
``KYORA_REAL_CLAUDE=1 python3 -m unittest tests.test_claude_code_smoke -v``

Observed on CLI 2.1.300 (OAuth subscription), see ``docs/claude-code-engine.md``:
``claude-fable-5-1`` was refused on its first request (HTTP 429 ``credits_required``,
"Switch to another model to continue"; the CLI's ``--fallback-model`` did not
engage), the engine switched to ``claude-opus-5-5``, which wrote ``allowed.txt``;
the write of ``forbidden.txt`` was blocked by the hook's exit 2 and the reason
came back to Claude as an error tool_result. The sanitized captures live in
``tests/fixtures/stream_json/real_*.jsonl``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from kyora.config import DEFAULT_SETTINGS, ClaudeCodeCfg
from kyora.models.base import BusSink, EngineJob
from kyora.models.claude_code import ClaudeCodeEngine
from kyora.pipeline.events import JUDGE_REQUEST, EventBus
from kyora.pipeline.judge import JudgeService, make_verdict, tool_summary
from kyora.server import KyoraServer, Request, Response, Router

REAL = os.environ.get("KYORA_REAL_CLAUDE") == "1" and shutil.which("claude") is not None

PROMPT = ("Create a file named allowed.txt containing the word OK. Then create a file named forbidden.txt "
          "containing the word NO. If a tool call is blocked, do not retry it; just stop and report.")
SYSTEM_PROMPT = ("You are running inside Kyora's Claude Code smoke test. Work only inside the current directory. "
                 "Use the Write tool to create files.")
FORBIDDEN = "forbidden.txt"
KEEP_ENV = ("CLAUDE_CONFIG_DIR",)  # where the subscription login lives when it is not ~/.claude


class SmokeJudge(JudgeService):
    """Real judge service plus one test rule: any tool call that would write ``forbidden.txt`` is denied."""

    def decide(self, run_id: str, call: dict) -> dict:
        name = str(call.get("tool_name") or "")
        tool_input = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").replace("\\", "/")
        hit = (name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and target.endswith(FORBIDDEN)) or \
              (name == "Bash" and FORBIDDEN in str(tool_input.get("command") or ""))
        if not hit:
            return super().decide(run_id, call)
        started = time.monotonic()
        ctx = self.context(run_id) or {}
        summary = tool_summary(name, tool_input)
        self._publish(JUDGE_REQUEST, {"tool_name": name, "summary": summary, "tool_use_id": call.get("tool_use_id")},
                      run_id, ctx.get("project_id"))
        verdict = make_verdict("deny", f"smoke test rule: writing {FORBIDDEN} is not allowed", "rule", risk="high",
                               rule="smoke_forbidden")
        verdict["latency_ms"] = int((time.monotonic() - started) * 1000)
        self._record(run_id, ctx, call, summary, verdict)
        return verdict


def child_env_overrides() -> dict:
    """Unset the parent Claude Code session's markers so the child CLI is an independent session.

    When this test runs from inside Claude Code (as it did when it was written),
    ``CLAUDECODE``/``CLAUDE_CODE_*`` describe *that* session (remote ingress,
    session ids, effort overrides). ``ANTHROPIC_API_KEY`` is unset so the CLI
    uses the subscription login this test is about.
    """
    over: dict = {k: None for k in os.environ if k.startswith("CLAUDE") and k not in KEEP_ENV}
    over["ANTHROPIC_API_KEY"] = None
    over["ANTHROPIC_AUTH_TOKEN"] = None
    return over


@unittest.skipUnless(REAL, "real Claude Code smoke test: set KYORA_REAL_CLAUDE=1 with a logged-in 'claude' on PATH")
class RealClaudeSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-smoke-")
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.ws = root / "game"
        self.ws.mkdir()
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=str(self.ws), check=False, capture_output=True)
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)
        self.bus = EventBus(None)
        self.judge = SmokeJudge(None, self.bus, None, self.home)
        router = Router()

        def judge_route(req: Request, params: dict) -> Response:
            status, body = self.judge.handle_request(req.json())
            return Response.json(body, status=status)

        router.add("POST", "/api/judge", judge_route, auth="loopback")
        self.server = KyoraServer(SimpleNamespace(), "127.0.0.1", 0, router)
        self.server.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.bus.close()
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_real_cli_hook_blocks_forbidden_write(self) -> None:
        defaults = DEFAULT_SETTINGS["claude_code"]
        cfg = ClaudeCodeCfg(command=["claude"], model="claude-fable-5-1",
                            fallback_models=["claude-opus-5-5", "claude-sonnet-5-5"], effort="max", max_turns=8,
                            timeout_s=600, idle_timeout_s=300, resume_for_fixes=True,
                            judge_tools=list(defaults["judge_tools"]), extra_args=[])
        app = SimpleNamespace(server_url=self.server.url, judge=self.judge)
        engine = ClaudeCodeEngine(cfg, self.home, app)
        avail = engine.availability()
        self.assertTrue(avail.available, avail.reason)
        hook = engine.install_hook()
        self.assertTrue(hook.is_file())

        run_id, project_id = "r_smoke", "p_smoke"
        job = EngineJob(run_id=run_id, workspace=self.ws, prompt=PROMPT, system_prompt=SYSTEM_PROMPT, max_turns=8,
                        model=cfg.model, fallback_models=list(cfg.fallback_models), effort="max",
                        judge_context={"files": [{"path": "allowed.txt"}], "constraints": {"no_external_deps": True}},
                        env=child_env_overrides(), kind="build", project_id=project_id)
        sub = self.bus.subscribe(run_id=run_id)
        started = time.monotonic()
        result = engine.run(job, BusSink(self.bus, run_id, project_id), threading.Event())
        wall_s = round(time.monotonic() - started, 1)
        sub.close()

        run_dir = engine.resolve_run_dir(job)
        stream_path = run_dir / "engine" / "stream.jsonl"
        journal_path = run_dir / "engine" / "judge.jsonl"
        stream_lines = stream_path.read_text(encoding="utf-8").splitlines() if stream_path.exists() else []
        journal = [json.loads(ln) for ln in journal_path.read_text(encoding="utf-8").splitlines()
                   if ln.strip()] if journal_path.exists() else []
        events = self.bus.replay(0, run_id=run_id)
        resolved = [e.data for e in events if e.type == "engine.model_resolved"]
        report = {
            "cli_version": avail.details.get("version"), "auth_method": avail.details.get("auth_method"),
            "ok": result.ok, "error": result.error, "requested_model": result.requested_model,
            "active_model": result.active_model, "fallback_used": result.fallback_used,
            "model_resolved_events": resolved, "session_id": result.session_id, "num_turns": result.num_turns,
            "duration_ms": result.duration_ms, "wall_s": wall_s, "cost_usd": result.cost_usd,
            "attempts": result.raw.get("attempts"), "retries": result.raw.get("retries"),
            "compat": result.raw.get("compat"), "model_usage": result.raw.get("model_usage"),
            "permission_denials": result.permission_denials, "result_text": result.result_text[:2000],
            "judge_journal": journal, "judge_stats": self.judge.stats(run_id),
            "judge_unavailable": [e.data for e in events if e.type == "judge.unavailable"],
            "tool_results": [e.data for e in events if e.type == "engine.tool_result"],
            "workspace_files": sorted(p.name for p in self.ws.iterdir() if p.name != ".git"),
            "stderr_tail": result.raw.get("stderr_tail"), "rc": result.raw.get("rc"),
            "api_error": result.raw.get("api_error"), "rate_limits": result.raw.get("rate_limits"),
            "compat_retry_events": [e.data for e in events if e.type == "engine.compat_retry"],
        }
        keep = os.environ.get("KYORA_SMOKE_KEEP")
        if keep:
            dest = Path(keep)
            dest.mkdir(parents=True, exist_ok=True)
            for src in list((run_dir / "engine").glob("*")) + [run_dir / "claude_settings.json"]:
                if src.is_file():
                    shutil.copy2(src, dest / src.name)
            (dest / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("\nKYORA SMOKE REPORT " + json.dumps({k: report[k] for k in (
            "ok", "error", "active_model", "fallback_used", "num_turns", "duration_ms", "wall_s", "attempts",
            "retries", "workspace_files", "judge_stats")}, default=str))

        # --- the contract under test
        allowed, forbidden = self.ws / "allowed.txt", self.ws / FORBIDDEN
        self.assertTrue(allowed.is_file(), f"allowed.txt missing; report: {report['result_text']!r} {result.error!r}")
        self.assertIn("OK", allowed.read_text(encoding="utf-8"))
        self.assertFalse(forbidden.exists(), "forbidden.txt was written although the judge denied it")
        parsed = []
        for ln in stream_lines:
            try:
                parsed.append(json.loads(ln))
            except ValueError:
                continue
        self.assertTrue(any(o.get("type") == "system" and o.get("subtype") == "init" for o in parsed), "no init line")
        self.assertTrue(any(o.get("type") == "result" for o in parsed), "no result line")
        denies = [e for e in journal if e.get("decision") == "deny" and FORBIDDEN in str(e.get("summary"))]
        self.assertTrue(denies, f"no deny for {FORBIDDEN} in the judge journal: {journal}")
        # exit-code-2 feedback: the blocked call came back to Claude as an error tool_result with the reason
        blocked = [t for t in report["tool_results"] if t.get("is_error") and "smoke test rule" in str(t.get("excerpt"))]
        self.assertTrue(blocked, f"the deny reason did not reach Claude: {report['tool_results']}")
        self.assertTrue(result.active_model, "no served model observed")
        self.assertTrue(result.ok, f"run did not finish cleanly: {result.error}")


if __name__ == "__main__":
    unittest.main()
