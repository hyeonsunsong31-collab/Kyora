"""Tests of ``kyora/runners/kyora_hook.py`` run as a real subprocess against a stub judge.

Protocol (mirrors Claude Code's ``examples/hooks/bash_command_validator_example.py``):
JSON on stdin; exit 0 = allow; exit 2 + stderr = block, the reason is fed back
to Claude; exit 1 = non-blocking error (bad input). Judge unreachable / non-200 /
garbage -> exit 0 (fail-open) and a line in ``KYORA_HOME/logs/hook.log``.

Run: ``python3 -m unittest tests.test_hook -v``
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kyora.models.claude_code import ClaudeCodeEngine
from kyora.config import DEFAULT_SETTINGS, ClaudeCodeCfg

HOOK = Path(__file__).resolve().parent.parent / "kyora" / "runners" / "kyora_hook.py"

CALL = {
    "session_id": "sess-1",
    "transcript_path": "/tmp/t.jsonl",
    "cwd": "/work/game",
    "hook_event_name": "PreToolUse",
    "tool_name": "Write",
    "tool_use_id": "toolu_77",
    "tool_input": {"file_path": "/work/game/forbidden.txt", "content": "NO"},
}


class StubJudge:
    """A tiny HTTP server standing in for ``POST /api/judge``."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.body: bytes = b'{"decision":"allow"}'
        self.delay_s = 0.0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(raw.decode("utf-8"))})
                if stub.delay_s:
                    time.sleep(stub.delay_s)
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(stub.body)))
                self.end_headers()
                try:
                    self.wfile.write(stub.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/judge"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def respond(self, obj, status: int = 200) -> None:
        self.status = status
        self.body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = StubJudge()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.close()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kyora-hook-")
        self.home = Path(self._tmp.name)
        self.stub.requests.clear()
        self.stub.delay_s = 0.0
        self.stub.respond({"decision": "allow"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **over: str | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("KYORA_", "NO_PROXY"))}
        # a bogus proxy proves the hook talks to the loopback judge directly (ProxyHandler({}))
        env.update({"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                    "KYORA_HOME": str(self.home), "KYORA_RUN_ID": "r_hook", "KYORA_HOOK_TOKEN": "tok-abc",
                    "KYORA_JUDGE_URL": self.stub.url, "PYTHONIOENCODING": "utf-8"})
        for k, v in over.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def run_hook(self, stdin: str | bytes, hook: Path = HOOK, isolated: bool = False, **env_over: str | None):
        data = stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
        args = [sys.executable] + (["-I"] if isolated else []) + [str(hook)]
        proc = subprocess.run(args, input=data, env=self.env(**env_over), capture_output=True, timeout=60,
                              cwd=str(self.home))
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def hook_log(self) -> str:
        path = self.home / "logs" / "hook.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ------------------------------------------------------------------ verdicts
    def test_allow_exits_0_and_posts_the_call(self):
        rc, out, err = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(len(self.stub.requests), 1)
        req = self.stub.requests[0]
        self.assertEqual(req["path"], "/api/judge")
        self.assertIn("application/json", req["headers"].get("Content-Type", ""))
        self.assertEqual(req["json"], {
            "hook_token": "tok-abc", "run_id": "r_hook", "session_id": "sess-1", "cwd": "/work/game",
            "hook_event_name": "PreToolUse", "tool_name": "Write", "tool_use_id": "toolu_77",
            "tool_input": {"file_path": "/work/game/forbidden.txt", "content": "NO"}})
        self.assertEqual(self.hook_log(), "")

    def test_deny_exits_2_with_reason_on_stderr(self):
        self.stub.respond({"decision": "deny", "reason": "forbidden.txt is not part of the plan", "source": "kimi"})
        rc, out, err = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 2)
        self.assertIn("forbidden.txt is not part of the plan", err)
        self.assertIn("Kyora judge (kimi) denied Write", err)
        self.assertEqual(out, "")

    def test_deny_reason_keeps_unicode(self):
        self.stub.respond({"decision": "DENY", "reason": "Schreiben außerhalb des Projekts ✗", "source": "rule"})
        rc, _, err = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 2)
        self.assertIn("außerhalb des Projekts ✗", err)

    def test_deny_without_reason_has_default_text(self):
        self.stub.respond({"decision": "deny"})
        rc, _, err = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 2)
        self.assertIn("the Kyora judge denied this tool call", err)

    def test_unknown_decision_allows(self):
        self.stub.respond({"decision": "ask"})
        rc, _, _ = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0)

    # ------------------------------------------------------------------ fail-open paths
    def test_unreachable_judge_fails_open_and_logs(self):
        url = f"http://127.0.0.1:{free_port()}/api/judge"
        rc, _, err = self.run_hook(json.dumps(CALL), KYORA_JUDGE_URL=url)
        self.assertEqual(rc, 0, err)
        log = self.hook_log()
        self.assertIn("judge unreachable", log)
        self.assertIn("fail-open", log)
        self.assertIn("run=r_hook", log)
        self.assertIn("tool=Write", log)
        self.assertIn("id=toolu_77", log)
        self.assertEqual(len(log.strip().splitlines()), 1)

    def test_http_500_fails_open(self):
        self.stub.respond({"error": "boom"}, status=500)
        rc, _, _ = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0)
        self.assertIn("HTTP 500", self.hook_log())

    def test_http_403_bad_token_fails_open(self):
        self.stub.respond({"error": "invalid or unknown hook token", "decision": "deny"}, status=403)
        rc, _, _ = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0)  # a deny body on a non-200 answer is not a verdict
        self.assertIn("HTTP 403", self.hook_log())

    def test_non_json_body_fails_open(self):
        self.stub.respond(b"<html>oops</html>")
        rc, _, _ = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0)
        self.assertIn("not JSON", self.hook_log())
        self.stub.respond([1, 2])
        rc, _, _ = self.run_hook(json.dumps(CALL))
        self.assertEqual(rc, 0)
        self.assertIn("not an object", self.hook_log())

    def test_timeout_fails_open(self):
        self.stub.delay_s = 3.0
        started = time.monotonic()
        rc, _, _ = self.run_hook(json.dumps(CALL), KYORA_HOOK_TIMEOUT_S="1")
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("judge unreachable", self.hook_log())

    def test_judge_url_unset_allows_and_logs(self):
        rc, _, err = self.run_hook(json.dumps(CALL), KYORA_JUDGE_URL=None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.stub.requests, [])
        self.assertIn("KYORA_JUDGE_URL not set", self.hook_log())
        rc, _, _ = self.run_hook(json.dumps(CALL), KYORA_JUDGE_URL="   ")
        self.assertEqual(rc, 0)

    # ------------------------------------------------------------------ bad input
    def test_bad_stdin_json_exits_1(self):
        rc, _, err = self.run_hook("{not json")
        self.assertEqual(rc, 1)
        self.assertIn("invalid JSON on stdin", err)
        self.assertEqual(self.stub.requests, [])
        rc, _, err = self.run_hook("")
        self.assertEqual(rc, 1)
        rc, _, err = self.run_hook("[1, 2]")
        self.assertEqual(rc, 1)
        self.assertIn("expected a JSON object", err)

    def test_non_dict_tool_input_is_sent_as_empty_object(self):
        call = dict(CALL, tool_input="rm -rf /", tool_name="Bash")
        rc, _, _ = self.run_hook(json.dumps(call))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.requests[0]["json"]["tool_input"], {})
        self.assertEqual(self.stub.requests[0]["json"]["tool_name"], "Bash")

    def test_utf8_stdin(self):
        call = dict(CALL, tool_input={"file_path": "/work/game/spiel-ü.txt", "content": "日本語"})
        rc, _, _ = self.run_hook(json.dumps(call, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stub.requests[0]["json"]["tool_input"]["content"], "日本語")

    # ------------------------------------------------------------------ installed copy
    def test_installed_copy_is_standalone(self):
        cfg = ClaudeCodeCfg(**DEFAULT_SETTINGS["claude_code"])
        installed = ClaudeCodeEngine(cfg, self.home, None).install_hook()
        self.assertEqual(installed.read_bytes(), HOOK.read_bytes())
        self.stub.respond({"decision": "deny", "reason": "nope", "source": "rule"})
        # -I: isolated mode, no PYTHONPATH and no script-dir/cwd on sys.path -> proves no kyora import
        rc, _, err = self.run_hook(json.dumps(CALL), hook=installed, isolated=True)
        self.assertEqual(rc, 2, err)
        self.assertIn("nope", err)


if __name__ == "__main__":
    unittest.main()
