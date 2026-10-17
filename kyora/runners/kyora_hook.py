#!/usr/bin/env python3
"""Kyora PreToolUse hook for Claude Code (standalone, standard library only).

Claude Code runs this script before every tool call matched by the
``PreToolUse`` entry of the per-run ``claude_settings.json`` that Kyora writes.
The protocol mirrors ``examples/hooks/bash_command_validator_example.py`` from
the Claude Code repository:

* stdin: one JSON object with ``tool_name``, ``tool_input``, ``tool_use_id``,
  ``session_id``, ``cwd`` and ``hook_event_name``.
* exit 0  -> allow the tool call.
* exit 2  -> block the tool call; whatever was printed to stderr is fed back to
  Claude as the reason (this is the path Kyora uses for a ``deny`` verdict).
* exit 1  -> non-blocking error; stderr is shown to the user, the tool proceeds.

The script POSTs ``{hook_token, run_id, session_id, cwd, hook_event_name,
tool_name, tool_use_id, tool_input}`` to ``KYORA_JUDGE_URL`` (Kyora's
``POST /api/judge``, loopback only) and maps the verdict:

* ``{"decision": "allow"}`` -> exit 0
* ``{"decision": "deny", "reason": ...}`` -> reason on stderr, exit 2

Fail-open: when the judge is unreachable, times out, answers with a 5xx (or any
other non-200) status or an unparsable body, a line is appended to
``KYORA_HOME/logs/hook.log`` and the tool call is allowed (exit 0). Kyora's
engine notices tool calls that have no journaled verdict and emits
``judge.unavailable`` so a silent bypass is never possible. A 401/403 is the one
exception: it means the server REJECTED the request (an invalid or unknown hook
token), not that the judge is down, so the hook fails CLOSED (exit 2) instead of
letting an unauthenticated call run.

Environment: ``KYORA_JUDGE_URL``, ``KYORA_RUN_ID``, ``KYORA_HOOK_TOKEN``,
``KYORA_HOME`` (log location, default ``~/.kyora``), optional
``KYORA_HOOK_TIMEOUT_S`` (default 280; the hook entry itself has a 300 s
timeout in the settings file).

This file is copied verbatim to ``KYORA_HOME/bin/kyora_hook.py``; it must not
import anything from the ``kyora`` package and must run on Windows (Git Bash),
Linux, macOS, Termux and iSH with any Python 3.8+.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT_S = 280.0
EXIT_ALLOW = 0
EXIT_ERROR = 1
EXIT_BLOCK = 2


def _utf8_stdio() -> None:
    """Make stderr UTF-8 so non-ASCII reasons survive on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - best effort only
                pass


def _home() -> Path:
    env = os.environ.get("KYORA_HOME")
    return Path(env).expanduser() if env else Path.home() / ".kyora"


def _log(message: str, run_id: str = "", tool_name: str = "", tool_use_id: str = "") -> None:
    """Append one line to ``KYORA_HOME/logs/hook.log`` (never raises)."""
    try:
        log_dir = _home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        line = f"{stamp} run={run_id or '-'} tool={tool_name or '-'} id={tool_use_id or '-'} {message}\n"
        with open(log_dir / "hook.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # pragma: no cover - logging must never break the hook
        pass


def _read_stdin() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8", "replace")
    return sys.stdin.read()


def _post_json(url: str, payload: dict, timeout_s: float) -> tuple[int, bytes]:
    """POST ``payload`` as JSON, bypassing any system/HTTP(S)_PROXY settings.

    Returns ``(status, body)``; raises on connection errors and timeouts.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": "kyora-hook/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout_s) as resp:
        return int(getattr(resp, "status", 200) or 200), resp.read()


def main() -> int:
    _utf8_stdio()
    raw = _read_stdin()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        print(f"kyora_hook: invalid JSON on stdin: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not isinstance(data, dict):
        print("kyora_hook: expected a JSON object on stdin", file=sys.stderr)
        return EXIT_ERROR

    tool_name = str(data.get("tool_name") or "")
    tool_use_id = str(data.get("tool_use_id") or "")
    run_id = os.environ.get("KYORA_RUN_ID", "")
    url = os.environ.get("KYORA_JUDGE_URL", "").strip()
    if not url:
        _log("KYORA_JUDGE_URL not set; allowing (fail-open)", run_id, tool_name, tool_use_id)
        return EXIT_ALLOW

    try:
        timeout_s = float(os.environ.get("KYORA_HOOK_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S

    payload = {
        "hook_token": os.environ.get("KYORA_HOOK_TOKEN", ""),
        "run_id": run_id,
        "session_id": data.get("session_id"),
        "cwd": data.get("cwd"),
        "hook_event_name": data.get("hook_event_name") or "PreToolUse",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {},
    }

    started = time.monotonic()
    try:
        status, body = _post_json(url, payload, timeout_s)
    except urllib.error.HTTPError as exc:  # non-2xx answered by the server
        detail = ""
        try:
            detail = exc.read()[:300].decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code in (401, 403):
            # The server positively REJECTED this request (an invalid or unknown hook token), which is a
            # misconfiguration or tampering, not an unreachable judge: fail CLOSED so the call cannot slip
            # through unjudged. Exit 2 blocks the tool and shows the reason to Claude.
            _log(f"judge rejected the request HTTP {exc.code}; BLOCKING (fail-closed) {detail}".rstrip(),
                 run_id, tool_name, tool_use_id)
            print(f"Kyora judge rejected the hook request (HTTP {exc.code}); blocking "
                  f"{tool_name or 'this tool call'}", file=sys.stderr)
            return EXIT_BLOCK
        _log(f"judge answered HTTP {exc.code}; allowing (fail-open) {detail}".rstrip(), run_id, tool_name, tool_use_id)
        return EXIT_ALLOW
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        _log(f"judge unreachable after {elapsed} ms ({type(exc).__name__}: {exc}); allowing (fail-open)",
             run_id, tool_name, tool_use_id)
        return EXIT_ALLOW

    if status != 200:
        _log(f"judge answered HTTP {status}; allowing (fail-open)", run_id, tool_name, tool_use_id)
        return EXIT_ALLOW
    try:
        verdict = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        _log(f"judge body not JSON ({exc}); allowing (fail-open)", run_id, tool_name, tool_use_id)
        return EXIT_ALLOW
    if not isinstance(verdict, dict):
        _log("judge body not an object; allowing (fail-open)", run_id, tool_name, tool_use_id)
        return EXIT_ALLOW

    decision = str(verdict.get("decision") or "allow").strip().lower()
    if decision == "deny":
        reason = str(verdict.get("reason") or "the Kyora judge denied this tool call").strip()
        source = verdict.get("source") or "judge"
        # stderr + exit 2: Claude Code blocks the tool and shows this text to Claude.
        print(f"Kyora judge ({source}) denied {tool_name or 'this tool call'}: {reason}", file=sys.stderr)
        return EXIT_BLOCK
    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
