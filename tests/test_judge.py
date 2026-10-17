"""Tests of ``kyora.pipeline.judge``: local rules, Kimi verdict cache, fail-open, HTTP entry point, journal, stats.

No model is called: Kimi is replaced by small test doubles defined here.

Run: ``python3 -m unittest tests.test_judge -v``
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from kyora.pipeline.events import EventBus
from kyora.pipeline.judge import JudgeService, input_hash, resolve_in_workspace, rule_verdict, tool_summary


class ScriptedKimi:
    """Stand-in for ``Kimi.judge(ctx, call, run_id)``: returns a fixed verdict and counts calls."""

    model = "moonshotai/kimi-k3"

    def __init__(self, verdict=None, exc: Exception | None = None) -> None:
        self.verdict = verdict if verdict is not None else {"decision": "allow", "reason": "fine", "risk": "low"}
        self.exc = exc
        self.calls: list[tuple[dict, dict, str]] = []
        self._lock = threading.Lock()

    def judge(self, ctx: dict, call: dict, run_id: str):
        with self._lock:
            self.calls.append((ctx, call, run_id))
        if self.exc is not None:
            raise self.exc
        return self.verdict


def write(path: str, content: str = "x") -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def bash(cmd: str, cwd: str | None = None) -> dict:
    call = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd:
        call["cwd"] = cwd
    return call


class RuleTests(unittest.TestCase):
    """``rule_verdict`` alone: deterministic, no service state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-judge-")
        self.ws = Path(self._tmp.name) / "repo"
        (self.ws / "src").mkdir(parents=True)
        self.ctx = {"workspace": str(self.ws), "files": [{"path": "./.kyora/manifest.json"}, "game/main.js",
                                                          {"path": "styles/app.css"}],
                    "constraints": {"no_external_deps": True}}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def v(self, call: dict, ctx: dict | None = None) -> tuple[str, str] | None:
        out = rule_verdict(self.ctx if ctx is None else ctx, call)
        return (out["decision"], out.get("rule")) if out else None

    def p(self, rel: str) -> str:
        return str(self.ws / rel)

    # ------------------------------------------------------------------ tools
    def test_read_only_tools_allowed(self):
        for name in ("Read", "Glob", "Grep", "LS", "TodoWrite", "Task"):
            self.assertEqual(self.v({"tool_name": name, "tool_input": {"file_path": "/etc/passwd"}}),
                             ("allow", "read_only_tool"), name)

    def test_network_tools_denied(self):
        self.assertEqual(self.v({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}), ("deny", "network_tool"))
        self.assertEqual(self.v({"tool_name": "WebSearch", "tool_input": {"query": "x"}}), ("deny", "network_tool"))

    def test_unknown_tool_defers(self):
        self.assertIsNone(self.v({"tool_name": "mcp__thing__do", "tool_input": {}}))

    # ------------------------------------------------------------------ writes
    def test_write_path_escapes_denied(self):
        for raw in ("/etc/passwd", self.p("../outside.txt"), "../outside.txt", "src/../../outside.txt",
                    "C:\\Windows\\system.ini", "D:/x.js", "\\\\server\\share\\x.js", "~/x.js", "$HOME/x.js",
                    "%APPDATA%\\x.js"):
            self.assertEqual(self.v(write(raw)), ("deny", "path_escape"), raw)
        for tool, key in (("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            self.assertEqual(self.v({"tool_name": tool, "tool_input": {key: "/etc/hosts"}}), ("deny", "path_escape"), tool)

    def test_write_symlink_escape_denied(self):
        try:
            os.symlink("/", str(self.ws / "rootlink"), target_is_directory=True)
            os.symlink(str(Path(self._tmp.name) / "nowhere" / "evil.txt"), str(self.ws / "dangling.txt"))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self.v(write(self.p("rootlink/etc/passwd"))), ("deny", "path_escape"))
        self.assertEqual(self.v(write(self.p("dangling.txt"))), ("deny", "path_escape"))  # dangling link target outside

    def test_symlink_into_git_is_protected(self):
        (self.ws / ".git").mkdir()
        try:
            os.symlink(str(self.ws / ".git"), str(self.ws / "gitlink"), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self.v(write(self.p("gitlink/config"))), ("deny", "protected_path"))

    def test_workspace_spelled_through_a_symlinked_prefix_is_inside(self):
        alias = Path(self._tmp.name) / "alias"
        try:
            os.symlink(str(self.ws), str(alias), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        # like macOS /var -> /private/var: same directory, different spelling
        self.assertEqual(resolve_in_workspace(str(alias / "src" / "a.js"), str(self.ws)), "src/a.js")
        self.assertEqual(self.v(write(str(alias / "src" / "a.js"))), ("allow", "standard_path"))

    def test_protected_paths_denied(self):
        for rel in (".git/config", ".git/hooks/pre-commit", ".claude/settings.json", ".GIT/HEAD", ".git"):
            self.assertEqual(self.v(write(self.p(rel))), ("deny", "protected_path"), rel)
        self.assertEqual(self.v(write(str(self.ws))), ("deny", "protected_path"))  # the root itself

    def test_planned_and_standard_paths_allowed(self):
        self.assertEqual(self.v(write(self.p("game/main.js"))), ("allow", "planned_file"))
        self.assertEqual(self.v(write(self.p(".kyora/manifest.json"))), ("allow", "planned_file"))  # leading dot kept
        self.assertEqual(self.v(write("styles/app.css")), ("allow", "planned_file"))
        for rel in ("index.html", "src/game.js", "assets/sprite.png", "tests/browser-tests.js", "README.md"):
            self.assertEqual(self.v(write(self.p(rel))), ("allow", "standard_path"), rel)
        self.assertEqual(self.v({"tool_name": "Edit", "tool_input": {"file_path": self.p("src/a.js")}}),
                         ("allow", "standard_path"))

    def test_other_in_workspace_writes_defer_to_kimi(self):
        self.assertIsNone(self.v(write(self.p("notes/todo.txt"))))
        self.assertIsNone(self.v(write(self.p("package.json"))))

    def test_missing_path_and_missing_workspace(self):
        self.assertEqual(self.v({"tool_name": "Write", "tool_input": {"content": "x"}}), ("deny", "missing_path"))
        self.assertEqual(self.v({"tool_name": "Write", "tool_input": "garbage"}), ("deny", "missing_path"))
        self.assertIsNone(self.v(write("/etc/passwd"), ctx={}))  # no workspace: path rules cannot apply

    def test_relative_path_uses_cwd(self):
        self.assertEqual(self.v(dict(write("src/a.js"), cwd=str(self.ws))), ("allow", "standard_path"))
        self.assertEqual(resolve_in_workspace("game/main.js", str(self.ws), str(self.ws / "src")), "src/game/main.js")
        self.assertIsNone(self.v(dict(write("notes.txt"), cwd=str(self.ws / "docs"))))  # docs/notes.txt: Kimi decides
        self.assertEqual(self.v(dict(write("a.js"), cwd="/tmp")), ("deny", "path_escape"))  # cwd left the workspace

    def test_windows_workspace_string_logic(self):
        ctx = {"workspace": "C:\\Users\\me\\.kyora\\projects\\p1\\repo"}
        self.assertEqual(self.v(write("c:/users/me/.kyora/projects/p1/repo/src/a.js"), ctx), ("allow", "standard_path"))
        self.assertEqual(self.v(write("C:\\Users\\me\\.kyora\\projects\\p1\\repo\\index.html"), ctx), ("allow", "standard_path"))
        self.assertEqual(self.v(write("C:\\Users\\me\\.kyora\\projects\\p1\\repo\\..\\other\\x.js"), ctx), ("deny", "path_escape"))
        self.assertEqual(self.v(write("D:\\repo\\src\\a.js"), ctx), ("deny", "path_escape"))
        self.assertEqual(self.v(write("C:\\Users\\me\\.kyora\\projects\\p1\\repo\\.git\\config"), ctx), ("deny", "protected_path"))

    # ------------------------------------------------------------------ bash
    def test_dangerous_commands_denied(self):
        for cmd in ("sudo rm x", "ls && sudo apt-get install foo", "git push origin main", "git -C repo push",
                    ":(){ :|:& };:", "chmod -R 777 .", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda",
                    "shutdown -h now", "git remote add origin https://x", "git tag -d slide/v1",
                    "git branch -D lineage/main", "git tag -f slide/v2 HEAD", "git update-ref -d refs/tags/slide/v1"):
            self.assertEqual(self.v(bash(cmd, cwd=str(self.ws))), ("deny", "dangerous_command"), cmd)

    def test_rm_rf_outside_or_of_workspace_denied(self):
        for cmd in ("rm -rf /", "rm -rf /tmp/elsewhere", "rm -rf .", "rm -rf ./", "rm -rf ..", "rm -rf ~/stuff",
                    "rm -rf *", "rm -fr $HOME", "rm -r ../sibling", "cd .. && rm -rf repo", "( cd .. && rm -rf x )",
                    "cd /tmp; rm -rf build", "rm -rf src/../..", "rm --recursive -f .git", "rm -rf " + str(self.ws)):
            self.assertEqual(self.v(bash(cmd, cwd=str(self.ws))), ("deny", "rm_outside_workspace"), cmd)

    def test_rm_inside_workspace_defers(self):
        self.assertIsNone(self.v(bash("rm -rf dist", cwd=str(self.ws))))
        self.assertIsNone(self.v(bash("rm src/old.js", cwd=str(self.ws))))  # not recursive
        self.assertIsNone(self.v(bash("cd src && rm -rf gen", cwd=str(self.ws))))

    def test_network_installs_depend_on_constraints(self):
        for cmd in ("curl -O https://x/y.js", "wget https://x", "npm install phaser", "npm i -D vite", "npx serve",
                    "pip install requests", "python3 -m pip install x", "yarn add three", "git clone https://x",
                    "apt-get install -y foo"):
            self.assertEqual(self.v(bash(cmd, cwd=str(self.ws))), ("deny", "external_deps"), cmd)
        free = {"workspace": str(self.ws), "constraints": {"no_external_deps": False}}
        for cmd in ("curl -O https://x/y.js", "npm install phaser"):
            self.assertIsNone(self.v(bash(cmd, cwd=str(self.ws)), ctx=free), cmd)
        plan_ctx = {"workspace": str(self.ws), "plan": {"constraints": {"no_external_deps": True}}}
        self.assertEqual(self.v(bash("npm install x"), ctx=plan_ctx), ("deny", "external_deps"))

    def test_read_only_commands_allowed(self):
        for cmd in ("ls", "ls -la src", "cat index.html", "cat src/a.js | grep foo", "node --check src/game.js",
                    "node -c src/a.js", "git status", "git diff HEAD", "git log --oneline -5", "git branch",
                    "git tag -l 'slide/*'", "git remote -v", "find . -name '*.js'", "wc -l src/*.js",
                    "python3 -m py_compile tool.py", "ls 2>&1", "grep -rn TODO src 2>/dev/null", "cd src && ls",
                    "head -n 20 src/game.js; tail -n 5 src/game.js", "env", "printenv PATH", "sort -u list.txt",
                    "uniq -c data.txt", "tree -L 2"):
            self.assertEqual(self.v(bash(cmd, cwd=str(self.ws))), ("allow", "read_only_command"), cmd)

    def test_commands_that_only_look_read_only_defer(self):
        for cmd in ("ls & python3 evil.py", "env python3 x.py", "env FOO=1 node x.js", "sort -o out.txt in.txt",
                    "sort --output=x a", "uniq in.txt out.txt", "xxd -r hex.txt out.bin", "tree -o listing.txt",
                    "cat <(node x.js)", "echo $(node x.js)", "echo `id`", "cat a > b", "cat <<EOF", "find . -delete",
                    "find . -exec rm {} ;", "git branch new-feature", "git tag v9", "git remote remove origin",
                    "git diff --output=patch.txt", "node build.js", "npm test", "cd /tmp && ls"):
            self.assertIsNone(self.v(bash(cmd, cwd=str(self.ws))), cmd)

    def test_env_assignment_segments_are_not_read_only(self):
        # Regression for security-judge-readonly-env-bypass: an assignment (export or bare, and leading env
        # assignments on a command) can steer an otherwise read-only tool into running an arbitrary program,
        # so the read-only fast path must never allow it; it defers to Kimi instead.
        for cmd in (
            "export GIT_CONFIG_COUNT=1; export GIT_CONFIG_KEY_0=core.fsmonitor; "
            "export GIT_CONFIG_VALUE_0=touch${IFS}../PWNED; git status",
            "export GIT_EXTERNAL_DIFF=touch${IFS}../x; git diff",
            "GIT_PAGER=sh git log", "LD_PRELOAD=/tmp/e.so ls", "PATH=/tmp ls", "IFS=; ls",
            "BASH_ENV=/tmp/e.sh cat index.html", "NODE_OPTIONS=--require=/tmp/e.js node --check src/a.js",
            "PYTHONSTARTUP=/tmp/e.py python3 --version", "FOO=bar", "export FOO=bar", "x=1 y=2 ls",
        ):
            self.assertIsNone(self.v(bash(cmd, cwd=str(self.ws))), cmd)

    def test_tool_subprocess_flags_are_not_read_only(self):
        # Regression for security-judge-readonly-env-bypass: read-only tools whose flags can run another
        # program or write a file must leave the read-only class (defer to Kimi).
        for cmd in (
            "rg --pre=sh pat src", "rg --pre sh pat src", "rg --hostname-bin /tmp/h pat",
            "sort --compress-program=sh big.txt", "sort -S 1 --compress-program=sh big.txt",
            "sort -o out.txt in.txt", "node --check src/a.js --require ./evil.cjs",
            "node -c --require=./evil.cjs src/a.js", "node -c -e \"require('cp').execSync('id')\"",
            "python3 -m py_compile a.py -c 'import os'", "git -c core.pager=sh log", "git diff --ext-diff",
            "git log --textconv", "git --config-env=core.pager=X log", "find . -fprint0 /tmp/x",
            "find . -fprintf /tmp/x '%p'", "find . -okdir rm {} ;", "date -s 2020-01-01", "date --set=now",
            "less +!id src/a.js", "more src/a.js", "man ls", "xargs rm", "awk 'BEGIN{system(\"id\")}'",
            "sed 'e id' src/a.js", "tar --to-command=sh -xf a.tar", "tar --use-compress-program=sh -cf a.tar .",
        ):
            self.assertIsNone(self.v(bash(cmd, cwd=str(self.ws))), cmd)

    def test_empty_bash_denied(self):
        self.assertEqual(self.v(bash("   ")), ("deny", "empty_command"))
        self.assertEqual(self.v({"tool_name": "Bash", "tool_input": {}}), ("deny", "empty_command"))

    def test_helpers(self):
        self.assertEqual(tool_summary("Write", {"file_path": "/a/b"}), "/a/b")
        self.assertEqual(tool_summary("Bash", {"command": "ls\n-la"}), "ls -la")
        self.assertEqual(input_hash("Write", {"file_path": str(self.ws / "a.js")}, str(self.ws)),
                         input_hash("Write", {"file_path": "a.js"}, str(self.ws)))  # same file, same key
        self.assertNotEqual(input_hash("Write", {"file_path": "a.js", "content": "1" * 300}, str(self.ws)),
                            input_hash("Write", {"file_path": "a.js", "content": "2" * 300}, str(self.ws)))
        self.assertEqual(input_hash("Bash", {"command": "ls   -la"}), input_hash("Bash", {"command": "ls -la"}))


class ServiceTests(unittest.TestCase):
    """``JudgeService`` with a real in-memory ``EventBus``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-judgesvc-")
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.ws = root / "repo"
        self.ws.mkdir()
        self.run_dir = root / "runs" / "r_1"
        self.bus = EventBus(None)
        self._old_home = os.environ.get("KYORA_HOME")
        os.environ["KYORA_HOME"] = str(self.home)

    def tearDown(self) -> None:
        self.bus.close()
        if self._old_home is None:
            os.environ.pop("KYORA_HOME", None)
        else:
            os.environ["KYORA_HOME"] = self._old_home
        self._tmp.cleanup()

    def service(self, kimi=None) -> JudgeService:
        return JudgeService(kimi, self.bus, None, self.home)

    def register(self, svc: JudgeService, run_id: str = "r_1", **extra) -> str:
        ctx = {"workspace": str(self.ws), "run_dir": str(self.run_dir), "project_id": "p_1",
               "files": ["src/game.js"], "constraints": {"no_external_deps": True}}
        ctx.update(extra)
        return svc.register_context(run_id, ctx)

    def call(self, path: str, tool_use_id: str = "t1", content: str = "x") -> dict:
        return {"tool_name": "Write", "tool_use_id": tool_use_id, "session_id": "s1",
                "tool_input": {"file_path": str(self.ws / path), "content": content}}

    def events(self, type_: str) -> list[dict]:
        return [e.data for e in self.bus.replay(0, types=[type_])]

    def journal(self, path: Path | None = None) -> list[dict]:
        path = path or (self.run_dir / "engine" / "judge.jsonl")
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ------------------------------------------------------------------ decisions
    def test_rule_verdict_does_not_call_kimi(self):
        kimi = ScriptedKimi()
        svc = self.service(kimi)
        self.register(svc)
        v = svc.decide("r_1", self.call("src/game.js"))
        self.assertEqual((v["decision"], v["source"], v["rule"]), ("allow", "rule", "planned_file"))
        v = svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "sudo ls"}})
        self.assertEqual((v["decision"], v["source"]), ("deny", "rule"))
        self.assertEqual(kimi.calls, [])
        self.assertIn("latency_ms", v)
        self.assertIsNone(v["judge_model"])

    def test_kimi_verdict_and_cache_hit(self):
        kimi = ScriptedKimi({"decision": "Deny", "reason": "not in plan", "risk": "medium"})
        svc = self.service(kimi)
        self.register(svc)
        v1 = svc.decide("r_1", self.call("notes/a.txt", "t1"))
        self.assertEqual((v1["decision"], v1["source"], v1["reason"], v1["risk"]), ("deny", "kimi", "not in plan", "medium"))
        self.assertEqual(v1["judge_model"], "moonshotai/kimi-k3")
        v2 = svc.decide("r_1", self.call("notes/a.txt", "t2"))
        self.assertEqual((v2["decision"], v2["source"], v2["reason"]), ("deny", "cache", "not in plan"))
        self.assertEqual(v2["cached_from"], "kimi")
        self.assertEqual(len(kimi.calls), 1)
        ctx, call, run_id = kimi.calls[0]
        self.assertEqual(run_id, "r_1")
        self.assertEqual(ctx["workspace"], str(self.ws))
        self.assertEqual(call["tool_name"], "Write")
        # different content -> different key -> Kimi again; other run -> own cache
        svc.decide("r_1", self.call("notes/a.txt", "t3", content="other"))
        self.assertEqual(len(kimi.calls), 2)
        self.register(svc, "r_2")
        svc.decide("r_2", self.call("notes/a.txt", "t4"))
        self.assertEqual(len(kimi.calls), 3)
        st = svc.stats("r_1")
        self.assertEqual((st["kimi"], st["cached"], st["denied"]), (2, 1, 3))

    def test_fail_open_without_kimi_publishes_unavailable(self):
        svc = self.service(None)
        self.register(svc)
        sub = self.bus.subscribe(run_id="r_1", types=["judge.*"])
        v = svc.decide("r_1", self.call("notes/a.txt"))
        self.assertEqual((v["decision"], v["source"], v["risk"]), ("allow", "unavailable", "unknown"))
        self.assertIn("not configured", v["reason"])
        got = []
        while True:
            try:
                got.append(sub.queue.get(timeout=1))
            except queue.Empty:
                break
            if got[-1].type == "judge.verdict":
                break
        types = [e.type for e in got]
        self.assertEqual(types, ["judge.request", "judge.unavailable", "judge.verdict"])
        self.assertEqual(got[1].data["tool_name"], "Write")
        self.assertEqual(got[1].project_id, "p_1")
        self.assertEqual(got[2].data["source"], "unavailable")
        sub.close()
        # unavailable verdicts are never cached
        svc.decide("r_1", self.call("notes/a.txt", "t9"))
        self.assertEqual(len(self.events("judge.unavailable")), 2)
        self.assertEqual(svc.stats("r_1")["unavailable"], 2)
        self.assertEqual(svc.stats("r_1")["cached"], 0)

    def test_kimi_exception_fails_open(self):
        svc = self.service(ScriptedKimi(exc=TimeoutError("read timed out")))
        self.register(svc)
        with self.assertLogs("kyora.judge", level="WARNING") as logs:
            v = svc.decide("r_1", self.call("notes/a.txt"))
        self.assertIn("Kimi judge failed", logs.output[0])
        self.assertEqual((v["decision"], v["source"]), ("allow", "unavailable"))
        self.assertIn("TimeoutError", v["reason"])
        self.assertIn("read timed out", self.events("judge.unavailable")[0]["reason"])

    def test_invalid_kimi_verdict_fails_open(self):
        for bad in ({"decision": "maybe"}, {"reason": "no decision"}, "allow", ["allow"], 42):
            svc = self.service(ScriptedKimi(bad))
            self.register(svc)
            v = svc.decide("r_1", self.call("notes/a.txt"))
            self.assertEqual((v["decision"], v["source"]), ("allow", "unavailable"), repr(bad))
            self.assertIn("no usable verdict", v["reason"])

    # ------------------------------------------------------------------ HTTP entry
    def test_handle_request_token_and_shapes(self):
        svc = self.service(ScriptedKimi())
        token = self.register(svc)
        self.assertGreaterEqual(len(token), 32)
        body = {"hook_token": token, "run_id": "r_1", "session_id": "s", "cwd": str(self.ws),
                "hook_event_name": "PreToolUse", "tool_name": "Write", "tool_use_id": "t1",
                "tool_input": {"file_path": str(self.ws / "src" / "game.js"), "content": "x"}}
        status, verdict = svc.handle_request(body)
        self.assertEqual(status, 200)
        self.assertEqual(verdict["decision"], "allow")
        json.dumps(verdict)
        for bad_token in ("wrong", "", None, 123, "tökén-ünicode"):
            status, out = svc.handle_request(dict(body, hook_token=bad_token))
            self.assertEqual(status, 403, repr(bad_token))
            self.assertEqual(out["decision"], "deny")
        self.assertEqual(svc.handle_request(dict(body, run_id="r_unknown"))[0], 403)
        self.assertEqual(svc.handle_request(dict(body, run_id=""))[0], 403)
        status, out = svc.handle_request(dict(body, tool_name=""))
        self.assertEqual((status, out["decision"]), (400, "deny"))
        self.assertEqual(svc.handle_request(dict(body, tool_name=None))[0], 400)
        for payload in (None, [], "x", 5):
            status, out = svc.handle_request(payload)
            self.assertEqual(status, 400)
            self.assertIn("error", out)
        # tokens are per run and die with forget()
        other = self.register(svc, "r_2")
        self.assertNotEqual(other, token)
        self.assertEqual(svc.handle_request(dict(body, hook_token=other))[0], 403)
        svc.forget("r_1")
        self.assertEqual(svc.handle_request(body)[0], 403)
        self.assertIsNone(svc.context("r_1"))

    def test_handle_request_non_dict_tool_input(self):
        svc = self.service(None)
        token = self.register(svc)
        status, v = svc.handle_request({"hook_token": token, "run_id": "r_1", "tool_name": "Write", "tool_input": "x"})
        self.assertEqual(status, 200)
        self.assertEqual((v["decision"], v["rule"]), ("deny", "missing_path"))

    def test_judge_route_is_token_authenticated_not_loopback(self):
        # Regression for protocol-judge-lan-host-fail-open: the /api/judge route must NOT be gated by a
        # loopback peer (which 403s the hook when Kyora binds a specific LAN IP, making every tool call run
        # unjudged). It is auth="any" so the request reaches handle_request, which authenticates the per-run
        # hook token with a constant-time hmac compare.
        from types import SimpleNamespace
        from kyora.pipeline import routes as proutes
        from kyora.server import Router
        svc = self.service(ScriptedKimi())
        token = self.register(svc)
        app = SimpleNamespace(config=SimpleNamespace(hub=SimpleNamespace(token="", expose_ui=False)),
                              judge=svc, missing={}, store=None, run_manager=None, bus=self.bus)
        router = Router()
        proutes.register_routes(router, app)
        judge_routes = [r for r in router.routes if r.pattern == "/api/judge"]
        self.assertEqual([r.auth for r in judge_routes], ["any"])
        # a request carrying the valid token is judged (as it would be from a LAN peer), a bad token is 403ed
        self.assertEqual(svc.handle_request({"hook_token": token, "run_id": "r_1", "tool_name": "Write",
                                             "tool_input": {"file_path": str(self.ws / "src" / "game.js")},
                                             "tool_use_id": "t1"})[0], 200)
        self.assertEqual(svc.handle_request({"hook_token": "nope", "run_id": "r_1", "tool_name": "Write",
                                             "tool_input": {}})[0], 403)

    def test_late_kimi_verdict_does_not_govern_the_call(self):
        # Regression for protocol-judge-late-verdict-recorded: a Kimi verdict that arrives after the hook
        # deadline must NOT govern the call that already ran. It is journaled as late only: never counted in
        # the run stats, its tool_use_id never registered (so has_verdict stays False and the engine counts
        # the call as unavailable). An in-time verdict still governs, and the direct (no-deadline) path is
        # unaffected. The cache is still seeded, so an identical later in-time call is judged instantly.
        class SleepyKimi:
            model = "moonshotai/kimi-k3"

            def judge(self, ctx, call, run_id):
                time.sleep(0.06)  # longer than the tiny deadline below, shorter than the governing one
                return {"decision": "deny", "reason": "would delete user data", "risk": "high"}

        svc = self.service(SleepyKimi())
        self.register(svc)
        late = svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "late1", "session_id": "s1",
                                  "tool_input": {"command": "python3 cleanup.py --all"}}, deadline_ms=5)
        self.assertEqual((late["decision"], late["source"]), ("allow", "timeout"))  # non-governing allow
        self.assertFalse(svc.has_verdict("r_1", "late1"))  # id not registered -> engine counts it unavailable
        self.assertEqual(svc.stats("r_1"), self.service(None)._empty_stats())  # nothing counted
        entries = self.journal()
        self.assertEqual(len(entries), 1)  # the late Kimi verdict is journaled, for audit
        self.assertEqual((entries[0]["source"], entries[0]["decision"]), ("kimi", "deny"))
        self.assertTrue(entries[0]["late"])
        self.assertFalse(entries[0]["governing"])
        self.assertEqual(entries[0]["deadline_ms"], 5)
        # an identical call now hits the cache the late verdict seeded (in-time this time): it governs
        hit = svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "cached1",
                                 "tool_input": {"command": "python3 cleanup.py --all"}}, deadline_ms=60000)
        self.assertEqual((hit["decision"], hit["source"], hit.get("cached_from")), ("deny", "cache", "kimi"))
        self.assertTrue(svc.has_verdict("r_1", "cached1"))
        self.assertEqual((svc.stats("r_1")["denied"], svc.stats("r_1")["cached"]), (1, 1))
        # an in-time Kimi verdict for a different call governs and is counted
        intime = svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "ok1",
                                    "tool_input": {"command": "python3 other.py"}}, deadline_ms=60000)
        self.assertEqual((intime["decision"], intime["source"]), ("deny", "kimi"))
        self.assertTrue(svc.has_verdict("r_1", "ok1"))
        # the direct Messages-API path (no deadline) is never treated as late, even for a slow verdict
        direct = svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "api1",
                                    "tool_input": {"command": "python3 third.py"}})
        self.assertEqual((direct["decision"], direct["source"]), ("deny", "kimi"))
        self.assertTrue(svc.has_verdict("r_1", "api1"))

    def test_handle_request_passes_hook_deadline(self):
        # Regression for protocol-judge-late-verdict-recorded: handle_request forwards a positive deadline_ms
        # from the hook payload to decide(); a bad/absent/non-positive value means "no deadline".
        class SleepyKimi:
            model = "moonshotai/kimi-k3"

            def judge(self, ctx, call, run_id):
                time.sleep(0.06)
                return {"decision": "deny", "reason": "nope", "risk": "high"}

        svc = self.service(SleepyKimi())
        token = self.register(svc)
        base = {"hook_token": token, "run_id": "r_1", "tool_name": "Bash",
                "tool_input": {"command": "python3 cleanup.py"}}
        status, v = svc.handle_request(dict(base, tool_use_id="h1", deadline_ms=5))
        self.assertEqual((status, v["decision"], v["source"]), (200, "allow", "timeout"))
        self.assertFalse(svc.has_verdict("r_1", "h1"))
        for bad in (None, 0, -10, "x", True):  # no finite deadline -> the verdict governs
            status, v = svc.handle_request(dict(base, tool_use_id="h2", deadline_ms=bad,
                                                tool_input={"command": "python3 x-%s.py" % bad}))
            self.assertEqual((status, v["decision"], v["source"]), (200, "deny", "kimi"), repr(bad))
            self.assertTrue(svc.has_verdict("r_1", "h2"), repr(bad))

    # ------------------------------------------------------------------ bookkeeping
    def test_journal_events_stats_and_has_verdict(self):
        svc = self.service(ScriptedKimi({"decision": "allow", "reason": "ok"}))
        self.register(svc)
        svc.decide("r_1", self.call("src/game.js", "t1"))  # rule allow
        svc.decide("r_1", {"tool_name": "Bash", "tool_use_id": "t2", "session_id": "s1",
                           "tool_input": {"command": "git push"}})  # rule deny
        svc.decide("r_1", self.call("notes/x.md", "t3"))  # kimi allow
        svc.decide("r_1", self.call("notes/x.md", "t4"))  # cache allow
        svc.decide("r_1", {"tool_name": "Read", "tool_input": {"file_path": "a"}})  # no tool_use_id
        entries = self.journal()
        self.assertEqual(len(entries), 5)
        self.assertEqual([e["source"] for e in entries], ["rule", "rule", "kimi", "cache", "rule"])
        self.assertEqual([e["decision"] for e in entries], ["allow", "deny", "allow", "allow", "allow"])
        first = entries[0]
        for key in ("ts", "run_id", "tool_use_id", "tool_name", "summary", "decision", "reason", "risk", "source",
                    "rule", "judge_model", "latency_ms", "session_id", "tool_input_excerpt"):
            self.assertIn(key, first)
        self.assertEqual(first["summary"], str(self.ws / "src" / "game.js"))
        self.assertEqual(first["session_id"], "s1")
        self.assertEqual(entries[1]["rule"], "dangerous_command")
        self.assertEqual(svc.stats("r_1"), {"allowed": 4, "denied": 1, "unavailable": 0, "cached": 1, "rule": 3, "kimi": 1})
        self.assertEqual(svc.stats("r_none"), {"allowed": 0, "denied": 0, "unavailable": 0, "cached": 0, "rule": 0, "kimi": 0})
        self.assertTrue(svc.has_verdict("r_1", "t1"))
        self.assertTrue(svc.has_verdict("r_1", "t4"))
        self.assertFalse(svc.has_verdict("r_1", "t99"))
        self.assertFalse(svc.has_verdict("r_other", "t1"))
        verdicts = self.events("judge.verdict")
        self.assertEqual(len(verdicts), 5)
        self.assertEqual(verdicts[1]["decision"], "deny")
        self.assertEqual(verdicts[1]["tool_name"], "Bash")
        self.assertEqual(verdicts[1]["tool_use_id"], "t2")
        reqs = self.events("judge.request")
        self.assertEqual(reqs[1]["summary"], "git push")
        evs = self.bus.replay(0, run_id="r_1", project_id="p_1")
        self.assertEqual(len(evs), 10)
        # forget keeps stats and verdict ids (the engine checks them after the run)
        svc.forget("r_1")
        self.assertTrue(svc.has_verdict("r_1", "t1"))
        self.assertEqual(svc.stats("r_1")["allowed"], 4)

    def test_journal_falls_back_to_home_logs(self):
        svc = self.service(None)
        svc.register_context("r_x", {"workspace": str(self.ws)})
        svc.decide("r_x", self.call("src/a.js"))
        entries = self.journal(self.home / "logs" / "judge.jsonl")
        self.assertEqual(entries[0]["run_id"], "r_x")

    def test_register_context_validation_and_without_bus(self):
        svc = JudgeService(None, None, None, self.home)
        with self.assertRaises(ValueError):
            svc.register_context("", {})
        svc.register_context("r_1", {"workspace": str(self.ws), "run_dir": str(self.run_dir)})
        v = svc.decide("r_1", self.call("notes/a.txt"))  # no bus: still a verdict, still journaled
        self.assertEqual(v["source"], "unavailable")
        self.assertEqual(len(self.journal()), 1)

    def test_record_unavailable_counts_unjudged_calls(self):
        # Regression for honesty-run-judge-stats-miss-unjudged-calls: a tool call that never reached the
        # judge (hook unreachable) must still raise the run's unavailable counter.
        svc = self.service(ScriptedKimi())
        self.register(svc)
        svc.decide("r_1", self.call("src/game.js", "t1"))  # rule allow
        svc.record_unavailable("r_1")  # hook could not reach the judge; the call ran unjudged
        svc.record_unavailable("r_1")
        st = svc.stats("r_1")
        self.assertEqual((st["allowed"], st["unavailable"]), (3, 2))
        svc.record_unavailable("r_1", allowed=False)
        self.assertEqual(svc.stats("r_1")["denied"], 1)
        svc.record_unavailable("")  # no run id: no-op

    def test_prime_stats_restores_persisted_counters_on_resume(self):
        # Regression for honesty-run-judge-stats-miss-unjudged-calls: a resumed run (fresh process, empty
        # in-memory counters) restores the counts saved with run.json, then keeps counting from there.
        svc = self.service(ScriptedKimi())
        svc.prime_stats("r_1", {"allowed": 5, "denied": 1, "unavailable": 2, "rule": 3, "kimi": 2})
        self.assertEqual(svc.stats("r_1"), {"allowed": 5, "denied": 1, "unavailable": 2, "cached": 0,
                                            "rule": 3, "kimi": 2})
        self.register(svc)  # register_context must keep the primed counters (setdefault)
        svc.decide("r_1", self.call("src/game.js", "t1"))  # rule allow -> builds on the restored total
        self.assertEqual((svc.stats("r_1")["allowed"], svc.stats("r_1")["rule"]), (6, 4))
        # once counting has begun this process, prime_stats is a no-op (no double-count)
        svc.prime_stats("r_1", {"allowed": 100})
        self.assertEqual(svc.stats("r_1")["allowed"], 6)
        # all-zero / non-dict counters never create or clobber an entry
        svc.prime_stats("r_2", {"allowed": 0})
        self.assertEqual(svc.stats("r_2"), svc._empty_stats())
        svc.prime_stats("r_3", "garbage")
        self.assertEqual(svc.stats("r_3"), svc._empty_stats())

    def test_concurrent_decisions(self):
        kimi = ScriptedKimi()
        svc = self.service(kimi)
        self.register(svc)
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    svc.decide("r_1", self.call(f"notes/{n}-{i}.txt", f"t{n}-{i}"))
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        st = svc.stats("r_1")
        self.assertEqual(st["allowed"], 120)
        self.assertEqual(st["kimi"], 120)
        self.assertEqual(len(self.journal()), 120)
        self.assertTrue(all(svc.has_verdict("r_1", f"t{n}-{i}") for n in range(6) for i in range(20)))


if __name__ == "__main__":
    unittest.main()
