"""Claude Code CLI code engine (subscription login, ``--effort max``) with the Kyora judge hook.

The engine runs ``claude -p`` as a subprocess per BUILD/FIX job, streams its
``stream-json`` output into engine events, enforces Kyora's PreToolUse judge
hook through a per-run ``--settings`` file and bounds the run with its own
watchdog (cancel, wall timeout, idle timeout). It never fabricates output: a
missing or logged-out CLI is reported as unavailable, process failures surface
as ``EngineResult.error`` with the real stderr tail.

Compatibility: ``--max-turns`` and ``--append-system-prompt-file`` are hidden
flags on current CLIs; when a CLI rejects them (``unknown option``) the run is
retried once without ``--max-turns`` and with ``--append-system-prompt <text>``
(``engine.compat_retry``). A model that is restricted or unknown before the
``init`` line, or that the API refuses on the first request after ``init``
before any work (e.g. HTTP 429 ``credits_required`` "Switch to another model to
continue", which the CLI's own ``--fallback-model`` does not cover), triggers a
retry with the next model of ``[model] + fallbacks``.
``--bare`` is never used (it disables OAuth).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from importlib import resources
from pathlib import Path
from typing import Any

from ..config import ClaudeCodeCfg
from ..pipeline.events import (
    ENGINE_COMPAT_RETRY, ENGINE_ERROR, ENGINE_MODEL_RESOLVED, ENGINE_RESULT, ENGINE_SESSION, ENGINE_STDERR,
    ENGINE_TEXT, ENGINE_THINKING, ENGINE_TOOL_RESULT, ENGINE_TOOL_USE, JUDGE_UNAVAILABLE, STAGE_PROGRESS,
)
from ..util.fs import atomic_write_bytes, atomic_write_json, atomic_write_text, which
from ..util.procs import kill_tree, popen, run as run_cmd
from .base import Availability, CodeEngine, EngineEvent, EngineJob, EngineResult, EngineSink

log = logging.getLogger("kyora.claude_code")

ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep")
DENIED_PERMISSIONS: tuple[str, ...] = ("WebFetch", "WebSearch")
HOOK_TIMEOUT_S = 300
AVAILABILITY_TTL_S = 60.0
PROBE_PROMPT = "Reply with exactly OK"
PROMPT_ARGV_LIMIT = 24000  # longer prompts go through stdin (Windows command-line limit is 32 K)
# Variables that describe a *parent* Claude Code session when Kyora itself is started from one (a Claude
# Code terminal, desktop or cloud session). Inherited, they make the child CLI think it is nested, adopt the
# parent's session id (observed in a real run: the child's init carried the parent's session_id), talk to
# the parent's messaging/ingress channels, or override ``--effort``. Auth and provider settings
# (CLAUDE_CONFIG_DIR, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_BEDROCK, ...) are kept.
STRIP_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID", "CLAUDE_CODE_WORKER_EPOCH", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_SESSION_INGRESS_TOKEN_FILE", "CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2",
    "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_CODE_REMOTE_SDK_URL", "CLAUDE_EFFORT",
)
UNKNOWN_OPTION_RE = re.compile(r"unknown option '?(--[A-Za-z0-9-]+)", re.IGNORECASE)
MODEL_ERROR_RE = re.compile(
    r"not found|restricted|not available|invalid model|unknown model|does not exist|unsupported model|"
    r"no access to|not permitted|model .{0,40}(?:unavailable|disabled)", re.IGNORECASE)
MODEL_WORD_RE = re.compile(r"\bmodel", re.IGNORECASE)
# An API error on the first request that says *this model* cannot be used by this account (observed on CLI
# 2.1.300: HTTP 429 api_error_code "credits_required", api_error "model_requires_usage_credits", result text
# "You're out of usage credits. Switch to another model to continue."). The CLI's own --fallback-model only
# covers overloaded/unavailable models, so Kyora switches to the next model itself.
POST_INIT_MODEL_ERROR_RE = re.compile(
    r"switch to another model|credits_required|requires_usage_credits|out of usage credits|usage limit|"
    r"model_not_found|not_found_error|permission_error|"
    r"model.{0,60}(?:not found|not available|restricted|does not exist|unsupported|not supported|disabled|unavailable)|"
    r"(?:not found|not available|restricted|no access|not permitted).{0,60}model", re.IGNORECASE)
NO_MODEL_SWITCH_STATUSES = (401,)  # authentication problems affect every model
DEFAULT_COMPAT: dict[str, bool] = {"max_turns": True, "append_system_prompt_file": True, "permission_prompts": True,
                                   "tools": True, "strict_mcp_config": True}
FILE_PATH_KEYS = ("file_path", "notebook_path", "path")
_MODEL_SUFFIX_RE = re.compile(r"(?:\[[^\]]*\])+$")  # context-window suffix such as "[1m]"
_MODEL_DATE_RE = re.compile(r"-\d{8}$")  # dated snapshot suffix such as "-20261001"


# --------------------------------------------------------------------------- helpers
def _posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _tail(text: str, n: int = 1500) -> str:
    text = text or ""
    return text if len(text) <= n else text[-n:]


# Windows runs a .cmd/.bat target (the npm shim claude.cmd) through cmd.exe, which stops at the first line
# break, expands %VAR%, does not understand list2cmdline's \" escaping and treats & | < > ^ in an unquoted
# argument as syntax. Free text never goes into such an argv: the prompt goes through stdin, the system
# prompt only through --append-system-prompt-file, and any other argument with these characters is refused.
CMD_SHIM_UNSAFE_RE = re.compile(r'[\r\n%"&|<>^]')


def _is_cmd_shim(exe: str | None) -> bool:
    return str(exe or "").lower().endswith((".cmd", ".bat"))


def cmd_shim_unsafe_args(argv: list[str]) -> list[str]:
    """Arguments after ``argv[0]`` that cmd.exe would mangle when ``argv[0]`` is a .cmd/.bat shim."""
    if not argv or not _is_cmd_shim(argv[0]):
        return []
    return [a for a in argv[1:] if CMD_SHIM_UNSAFE_RE.search(a)]


def _canonical_model(name: str) -> str:
    """Lower-case model id without a ``[1m]``-style suffix or a ``-YYYYMMDD`` snapshot date."""
    out = _MODEL_SUFFIX_RE.sub("", name.strip().lower())
    return _MODEL_DATE_RE.sub("", out)


def is_real_model_name(name: Any) -> bool:
    """False for placeholders such as ``<synthetic>`` that the CLI uses for locally generated messages."""
    return isinstance(name, str) and bool(name.strip()) and not name.strip().startswith("<")


def same_model(requested: str | None, active: str | None) -> bool:
    """True when ``active`` is the model the user asked for.

    Aliases (``opus`` -> ``claude-opus-5-5``), context-window suffixes (``[1m]``)
    and dated snapshot ids (``claude-opus-5-5-20261001``) count as the same model.
    """
    if not requested or not active:
        return False
    r, a = _canonical_model(requested), _canonical_model(active)
    if r == a:
        return True
    if r and not any(ch.isdigit() for ch in r) and r in a:
        return True  # alias such as "opus" -> "claude-opus-5-5"
    return False


def primary_model_from_usage(usage: Any, candidates: list[str] | None = None) -> str | None:
    """Pick the model that did the work from a result's ``modelUsage`` map.

    The CLI may also list helper models used for background tasks, so a key that
    matches one of ``candidates`` (requested model + fallbacks) wins; otherwise
    the entry with the most output tokens.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    names = [k for k in usage.keys() if is_real_model_name(k)]
    if not names:
        return None
    for cand in candidates or []:
        for name in names:
            if same_model(cand, name):
                return name

    def _out_tokens(name: str) -> float:
        entry = usage.get(name)
        if isinstance(entry, dict):
            val = entry.get("outputTokens", entry.get("output_tokens", 0))
            if isinstance(val, (int, float)):
                return float(val)
        return 0.0

    return max(names, key=_out_tokens)


def summarize_tool_input(name: str, tool_input: Any) -> str:
    """``file_path`` for file tools, first 200 chars of ``command`` for Bash, else a short JSON."""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:200]
    for key in FILE_PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val[:200]
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd:
        return cmd.strip().replace("\n", " ")[:200]
    for key in ("pattern", "query", "url", "description", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val[:200]
    try:
        return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)[:200]
    except (TypeError, ValueError):
        return str(tool_input)[:200]


def _content_excerpt(content: Any, limit: int = 400) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        text = str(content)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _result_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


class _Throttle:
    """Allow at most ``burst`` events per ``window_s`` seconds (used for ``engine.stderr``)."""

    def __init__(self, burst: int = 10, window_s: float = 5.0):
        self.burst = burst
        self.window_s = window_s
        self._stamps: deque[float] = deque()
        self.suppressed = 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._stamps and now - self._stamps[0] > self.window_s:
                self._stamps.popleft()
            if len(self._stamps) < self.burst:
                self._stamps.append(now)
                return True
            self.suppressed += 1
            return False


class _Attempt:
    """Mutable outcome of one CLI process run."""

    def __init__(self) -> None:
        self.rc: int | None = None
        self.stderr_lines: deque[str] = deque(maxlen=400)
        self.cancelled = False
        self.timeout_reason: str | None = None
        self.started = time.monotonic()
        self.spawn_error: str | None = None

    @property
    def stderr_text(self) -> str:
        return "".join(self.stderr_lines)


# --------------------------------------------------------------------------- engine
class ClaudeCodeEngine(CodeEngine):
    """Code engine backed by the Claude Code CLI (``claude -p ... --output-format stream-json``)."""

    name = "claude_code"

    def __init__(self, cfg: ClaudeCodeCfg, home: Path, app):
        self.cfg = cfg
        self.home = Path(home)
        self.app = app
        self._avail_lock = threading.Lock()
        self._avail_cache: tuple[float, Availability] | None = None
        self._hook_lock = threading.Lock()

    # ------------------------------------------------------------------ hook
    def hook_path(self) -> Path:
        return self.home / "bin" / "kyora_hook.py"

    @staticmethod
    def hook_source() -> bytes:
        """The packaged hook script (works from a source tree and from ``kyora.pyz``)."""
        try:
            return resources.files("kyora.runners").joinpath("kyora_hook.py").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, AttributeError):
            fallback = Path(__file__).resolve().parent.parent / "runners" / "kyora_hook.py"
            return fallback.read_bytes()

    def install_hook(self) -> Path:
        """Copy the hook to ``KYORA_HOME/bin/kyora_hook.py`` when its content differs; return the path."""
        src = self.hook_source()
        dst = self.hook_path()
        with self._hook_lock:
            current = None
            try:
                current = dst.read_bytes()
            except FileNotFoundError:
                current = None
            except OSError as exc:
                log.warning("cannot read existing hook %s: %s", dst, exc)
            if current != src:
                atomic_write_bytes(dst, src)
                log.info("installed judge hook at %s", dst)
            if os.name != "nt":
                try:
                    dst.chmod(dst.stat().st_mode | 0o111)
                except OSError as exc:
                    log.debug("chmod hook failed: %s", exc)
        return dst

    # ------------------------------------------------------------------ availability
    def _resolve_command(self) -> list[str] | None:
        cmd = list(self.cfg.command or ["claude"])
        if not cmd:
            return None
        exe = which(cmd[0])
        if exe is None and (os.sep in cmd[0] or (os.altsep and os.altsep in cmd[0])):
            exe = cmd[0] if Path(cmd[0]).exists() else None
        if exe is None:
            return None
        return [exe] + cmd[1:]

    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in STRIP_ENV:
            env.pop(key, None)
        env.setdefault("KYORA_HOME", str(self.home))
        return env

    def availability(self) -> Availability:
        """Resolve the CLI, read ``--version`` and ``auth status --json`` (cached 60 s)."""
        with self._avail_lock:
            cached = self._avail_cache
            if cached and time.monotonic() - cached[0] < AVAILABILITY_TTL_S:
                return cached[1]
        avail = self._probe_availability()
        with self._avail_lock:
            self._avail_cache = (time.monotonic(), avail)
        return avail

    def invalidate_availability(self) -> None:
        with self._avail_lock:
            self._avail_cache = None

    def _probe_availability(self) -> Availability:
        cmd_name = (self.cfg.command or ["claude"])[0]
        details: dict[str, Any] = {"command": list(self.cfg.command or ["claude"]), "model": self.cfg.model,
                                   "fallback_models": list(self.cfg.fallback_models), "effort": self.cfg.effort}
        base = self._resolve_command()
        if base is None:
            return Availability(False, self.name, f"'{cmd_name}' not found on PATH (install Claude Code and run 'claude auth login')",
                                details)
        details["path"] = base[0]
        env = self._base_env()
        try:
            rc, out, err = run_cmd(base + ["--version"], timeout=20, env=env, cwd=self.home)
        except OSError as exc:
            return Availability(False, self.name, f"cannot execute {base[0]}: {exc}", details)
        if rc != 0:
            return Availability(False, self.name, f"'{cmd_name} --version' failed (rc={rc}): {_tail(err or out, 300).strip()}",
                                details)
        details["version"] = (out or err).strip().splitlines()[0] if (out or err).strip() else ""
        try:
            rc2, out2, err2 = run_cmd(base + ["auth", "status", "--json"], timeout=20, env=env, cwd=self.home)
        except OSError as exc:
            rc2, out2, err2 = -1, "", str(exc)
        status = self._parse_json_blob(out2)
        if isinstance(status, dict) and "loggedIn" in status:
            logged_in = bool(status.get("loggedIn"))
            details.update({"logged_in": logged_in, "auth_method": status.get("authMethod"),
                            "api_provider": status.get("apiProvider")})
            if not logged_in:
                return Availability(False, self.name, "Claude Code is not logged in: run 'claude auth login' (subscription)", details)
            return Availability(True, self.name, "", details)
        details.update({"logged_in": None, "auth_method": None, "api_provider": None,
                        "auth_status_error": _tail(err2 or out2, 300).strip() or f"rc={rc2}"})
        # Older CLIs have no 'auth status'; the CLI exists, so report it usable but say what is unknown.
        return Availability(True, self.name, "login state unknown ('claude auth status --json' unsupported)", details)

    @staticmethod
    def _parse_json_blob(text: str) -> Any:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            pass
        start = text.find("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except ValueError:
                pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    # ------------------------------------------------------------------ argv / settings
    def resolve_run_dir(self, job: EngineJob) -> Path:
        ctx = job.judge_context or {}
        if ctx.get("run_dir"):
            return Path(str(ctx["run_dir"]))
        if job.project_id:
            return self.home / "projects" / job.project_id / "runs" / job.run_id
        return self.home / "runs" / job.run_id

    def build_settings(self, job: EngineJob) -> dict:
        """Per-run ``--settings`` JSON: judge hook on ``cfg.judge_tools`` and web tools denied."""
        python = _posix(sys.executable or "python3")
        hook = _posix(self.hook_path())
        matcher = "|".join(t for t in (self.cfg.judge_tools or []) if t) or "Write|Edit|MultiEdit|NotebookEdit|Bash"
        return {
            "hooks": {
                "PreToolUse": [{
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": f'"{python}" "{hook}"', "timeout": HOOK_TIMEOUT_S}],
                }]
            },
            "permissions": {"deny": list(DENIED_PERMISSIONS)},
        }

    def prompt_via_stdin(self, job: EngineJob, exe: str | None = None) -> bool:
        """Long prompts, and every prompt for a .cmd/.bat shim ``exe``, go through stdin instead of argv."""
        return _is_cmd_shim(exe) or len(job.prompt or "") > PROMPT_ARGV_LIMIT

    def build_argv(self, job: EngineJob, run_dir: Path | str, compat: dict | None = None) -> list[str]:
        """Command line for one attempt. ``compat`` may override ``model``/``fallbacks`` and flag support;
        ``compat["cmd_shim"]`` says the resolved executable is a .cmd/.bat shim (no free text in argv)."""
        compat = {**DEFAULT_COMPAT, **(compat or {})}
        run_dir = Path(run_dir)
        model = compat.get("model") or job.model
        fallbacks = compat.get("fallbacks")
        if fallbacks is None:
            fallbacks = [m for m in (job.fallback_models or []) if m and m != model]
        argv = list(self.cfg.command or ["claude"])
        shim = bool(compat.get("cmd_shim")) or _is_cmd_shim(argv[0])
        argv.append("-p")
        if not shim and not self.prompt_via_stdin(job):
            prompt = job.prompt or ""
            argv.append(" " + prompt if prompt.startswith("-") else prompt)
        argv += ["--model", model]
        if fallbacks:
            argv += ["--fallback-model", ",".join(fallbacks)]
        argv += ["--effort", job.effort or self.cfg.effort or "max"]
        if compat.get("max_turns", True) and job.max_turns:
            argv += ["--max-turns", str(int(job.max_turns))]
        argv += ["--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
        if compat.get("permission_prompts", True):
            argv += ["--permission-prompts", "none"]
        argv += ["--allowedTools", ",".join(ALLOWED_TOOLS)]
        # Only the built-in tools Kyora judges are available: without --tools the session would
        # also get every MCP server and agent tool configured on this machine, unjudged.
        if compat.get("tools", True):
            argv += ["--tools", ",".join(ALLOWED_TOOLS)]
        if compat.get("strict_mcp_config", True):
            argv.append("--strict-mcp-config")
        if job.system_prompt:
            if compat.get("append_system_prompt_file", True) or shim:
                argv += ["--append-system-prompt-file", _posix(run_dir / "system_prompt.md")]
            else:
                argv += ["--append-system-prompt", job.system_prompt]
        argv += ["--settings", _posix(run_dir / "claude_settings.json")]
        if job.resume_session_id:
            argv += ["--resume", job.resume_session_id]
        argv += list(self.cfg.extra_args or [])
        return argv

    @staticmethod
    def describe_argv(argv: list[str]) -> list[str]:
        """argv with the prompt text and system prompt replaced by placeholders (for logs/events)."""
        out: list[str] = []
        skip_next = False
        for i, a in enumerate(argv):
            if skip_next:
                out.append("<text>")
                skip_next = False
                continue
            out.append(a)
            if a in ("-p", "--append-system-prompt") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                skip_next = True
        return out

    # ------------------------------------------------------------------ retry decisions (pure)
    @staticmethod
    def compat_retry_decision(stderr_text: str, compat: dict | None) -> dict | None:
        """Return ``{"compat": {...}, "reason", "argv_change"}`` when stderr shows a rejected optional flag."""
        compat = {**DEFAULT_COMPAT, **(compat or {})}
        for m in UNKNOWN_OPTION_RE.finditer(stderr_text or ""):
            opt = m.group(1).lower()
            if opt == "--max-turns" and compat.get("max_turns"):
                return {"compat": {"max_turns": False}, "reason": f"CLI rejected {opt}",
                        "argv_change": "dropped --max-turns (Kyora's watchdog bounds the run)"}
            # Through a .cmd/.bat shim the system prompt may only travel as a file, so there is no inline retry.
            if opt == "--append-system-prompt-file" and compat.get("append_system_prompt_file") \
                    and not compat.get("cmd_shim"):
                return {"compat": {"append_system_prompt_file": False}, "reason": f"CLI rejected {opt}",
                        "argv_change": "replaced --append-system-prompt-file <file> with --append-system-prompt <text>"}
            if opt == "--tools" and compat.get("tools"):
                return {"compat": {"tools": False}, "reason": f"CLI rejected {opt}",
                        "argv_change": "dropped --tools (older CLI; --strict-mcp-config still keeps MCP tools out)"}
            if opt == "--strict-mcp-config" and compat.get("strict_mcp_config"):
                return {"compat": {"strict_mcp_config": False}, "reason": f"CLI rejected {opt}",
                        "argv_change": "dropped --strict-mcp-config (older CLI)"}
            if opt == "--permission-prompts" and compat.get("permission_prompts"):
                return {"compat": {"permission_prompts": False}, "reason": f"CLI rejected {opt}",
                        "argv_change": "dropped --permission-prompts none (older CLI denies prompts in -p mode anyway)"}
        return None

    @staticmethod
    def model_retry_decision(text: str, saw_init: bool, model_index: int, models: list[str]) -> int | None:
        """Index of the next model to try when the CLI failed before ``init`` with a model error, else ``None``.

        The text must both look like an availability error and mention a model, so
        an unrelated pre-init failure (``file not found``) is not misreported as a
        model problem.
        """
        if saw_init:
            return None
        text = text or ""
        if not MODEL_ERROR_RE.search(text) or not MODEL_WORD_RE.search(text):
            return None
        if model_index + 1 >= len(models):
            return None
        return model_index + 1

    @staticmethod
    def model_unavailable_after_init(state: dict) -> str | None:
        """Reason to switch to the next model when the CLI started but the model refused before doing any work.

        True only when the session ended with an error result, no tool was used and
        no message came from a real model (only ``<synthetic>`` API-error text), and
        the error says the model cannot be used (credits / usage limit / not found /
        restricted). Authentication errors (HTTP 401) never switch models.
        """
        res = state.get("result")
        if not state.get("saw_init") or not isinstance(res, dict) or not res.get("is_error"):
            return None
        if state.get("tool_uses") or state.get("model_messages"):
            return None  # a model already worked in this session
        status = res.get("api_error_status")
        if status in NO_MODEL_SWITCH_STATUSES:
            return None
        codes = [str(res.get(k)) for k in ("api_error", "api_error_code") if res.get(k)]
        text = str(res.get("result") or "").strip()
        if not (POST_INIT_MODEL_ERROR_RE.search(" ".join(codes + [text]))
                or (status in (403, 404) and MODEL_WORD_RE.search(text))):
            return None
        detail = text[:300] or "API error"
        extra = ", ".join(([f"HTTP {status}"] if status else []) + codes)
        return f"{detail} ({extra})" if extra else detail

    # ------------------------------------------------------------------ stream-json parsing (pure)
    @staticmethod
    def new_parse_state(requested_model: str, max_turns: int, stage: str = "build") -> dict:
        return {"requested_model": requested_model, "active_model": None, "session_id": None, "saw_init": False,
                "tool_uses": 0, "max_turns": max(1, int(max_turns or 1)), "stage": stage or "build",
                "tool_names": {}, "result": None, "malformed": 0, "lines": 0, "model_changes": 0,
                "model_messages": 0, "rate_limits": []}

    @staticmethod
    def parse_stream_line(line: str, state: dict) -> list[EngineEvent]:
        """Turn one ``stream-json`` line into engine events; malformed lines are counted and ignored."""
        text = (line or "").strip()
        if not text:
            return []
        try:
            obj = json.loads(text)
        except ValueError:
            state["malformed"] = state.get("malformed", 0) + 1
            return []
        if not isinstance(obj, dict):
            state["malformed"] = state.get("malformed", 0) + 1
            return []
        state["lines"] = state.get("lines", 0) + 1
        kind = obj.get("type")
        events: list[EngineEvent] = []
        if kind == "system":
            if obj.get("subtype") == "init":
                state["saw_init"] = True
                if obj.get("session_id"):
                    state["session_id"] = obj["session_id"]
                model = obj.get("model")
                events.append(EngineEvent(ENGINE_SESSION, {
                    "session_id": obj.get("session_id"), "model": model, "tools": obj.get("tools") or [],
                    "permission_mode": obj.get("permissionMode"), "cwd": obj.get("cwd"), "backend": "claude_code"}))
                if isinstance(model, str) and model:
                    events.extend(ClaudeCodeEngine._resolve_model(state, model))
            return events
        if kind == "assistant":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            model = msg.get("model")
            if is_real_model_name(model):
                state["model_messages"] = state.get("model_messages", 0) + 1
            if isinstance(model, str) and model:
                events.extend(ClaudeCodeEngine._resolve_model(state, model))
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    if block.get("text"):
                        events.append(EngineEvent(ENGINE_TEXT, {"text": str(block["text"])}))
                elif btype == "thinking":
                    events.append(EngineEvent(ENGINE_THINKING, {"text": str(block.get("thinking") or "")}))
                elif btype == "tool_use":
                    tid = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    summary = summarize_tool_input(name, block.get("input"))
                    state["tool_names"][tid] = name
                    state["tool_uses"] = state.get("tool_uses", 0) + 1
                    events.append(EngineEvent(ENGINE_TOOL_USE, {"id": tid, "name": name, "summary": summary}))
                    pct = min(95, int(state["tool_uses"] / max(1, state.get("max_turns", 1)) * 100))
                    events.append(EngineEvent(STAGE_PROGRESS, {"stage": state.get("stage", "build"), "pct": pct,
                                                               "message": f"{name}: {summary}"[:200]}))
            return events
        if kind == "user":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            if not isinstance(content, list):
                return []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = str(block.get("tool_use_id") or "")
                    events.append(EngineEvent(ENGINE_TOOL_RESULT, {
                        "tool_use_id": tid, "name": state["tool_names"].get(tid), "is_error": bool(block.get("is_error")),
                        "excerpt": _content_excerpt(block.get("content"))}))
            return events
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") if isinstance(obj.get("rate_limit_info"), dict) else {}
            limits = state.setdefault("rate_limits", [])
            if len(limits) < 20:
                limits.append({k: info.get(k) for k in ("status", "resetsAt", "errorCode", "overageDisabledReason",
                                                        "rateLimitType") if k in info})
            return []
        if kind == "result":
            state["result"] = obj
            if obj.get("session_id"):
                state["session_id"] = obj["session_id"]
            result_text = obj.get("result")
            events.append(EngineEvent(ENGINE_RESULT, {
                "subtype": obj.get("subtype"), "is_error": bool(obj.get("is_error")), "num_turns": obj.get("num_turns"),
                "duration_ms": obj.get("duration_ms"), "cost_usd": obj.get("total_cost_usd"),
                "result": (result_text if isinstance(result_text, str) else json.dumps(result_text, default=str))[:4000]
                if result_text is not None else ""}))
            return events
        return []

    @staticmethod
    def _resolve_model(state: dict, model: str) -> list[EngineEvent]:
        if not is_real_model_name(model):
            return []  # "<synthetic>" messages (API errors, local notices) were not served by a model
        if model == state.get("active_model"):
            return []
        if state.get("active_model") is not None:
            state["model_changes"] = state.get("model_changes", 0) + 1
        state["active_model"] = model
        requested = state.get("requested_model")
        return [EngineEvent(ENGINE_MODEL_RESOLVED, {"requested": requested, "active": model,
                                                    "fallback_used": not same_model(requested, model)})]

    # ------------------------------------------------------------------ run
    def _child_env(self, job: EngineJob, token: str) -> dict[str, str]:
        env = self._base_env()
        for k, v in (job.env or {}).items():
            if v is None:
                env.pop(str(k), None)
            else:
                env[str(k)] = str(v)
        server_url = str(getattr(self.app, "server_url", "") or "").rstrip("/")
        env["KYORA_JUDGE_URL"] = f"{server_url}/api/judge" if server_url else ""
        env["KYORA_RUN_ID"] = job.run_id
        env["KYORA_HOOK_TOKEN"] = token or ""
        env["KYORA_HOME"] = str(self.home)
        return env

    @staticmethod
    def _emit(sink: EngineSink, ev: EngineEvent) -> None:
        try:
            sink.on_event(ev)
        except Exception as exc:  # noqa: BLE001 - a sink bug must not kill the reader thread
            log.error("engine sink failed for %s: %s", ev.type, exc)

    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult:
        """Run one BUILD/FIX job to completion (or cancel/timeout) and return the measured result."""
        started = time.monotonic()
        run_dir = self.resolve_run_dir(job)
        engine_dir = run_dir / "engine"
        engine_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(run_dir / "prompt.txt", job.prompt or "")
        atomic_write_text(run_dir / "system_prompt.md", job.system_prompt or "")
        atomic_write_json(run_dir / "claude_settings.json", self.build_settings(job))
        try:
            self.install_hook()
        except OSError as exc:
            msg = f"cannot install judge hook: {exc}"
            self._emit(sink, EngineEvent(ENGINE_ERROR, {"error": msg}))
            return EngineResult(ok=False, backend=self.name, requested_model=job.model, error=msg)

        base = self._resolve_command()
        if base is None:
            msg = f"'{(self.cfg.command or ['claude'])[0]}' not found on PATH"
            self._emit(sink, EngineEvent(ENGINE_ERROR, {"error": msg}))
            return EngineResult(ok=False, backend=self.name, requested_model=job.model, error=msg)

        judge = getattr(self.app, "judge", None)
        token = ""
        if judge is not None:
            ctx = dict(job.judge_context or {})
            ctx.setdefault("workspace", str(job.workspace))
            ctx.setdefault("project_id", job.project_id)
            ctx.setdefault("run_dir", str(run_dir))
            ctx.setdefault("kind", job.kind)
            token = judge.register_context(job.run_id, ctx)
            if not str(getattr(self.app, "server_url", "") or ""):
                self._emit(sink, EngineEvent(JUDGE_UNAVAILABLE, {
                    "reason": "Kyora server URL unknown; the hook cannot reach the judge (tool calls run unjudged)"}))
        else:
            self._emit(sink, EngineEvent(JUDGE_UNAVAILABLE, {"reason": "judge service not configured; tool calls run unjudged"}))

        env = self._child_env(job, token)
        models: list[str] = []
        for m in [job.model] + list(job.fallback_models or []):
            if m and m not in models:
                models.append(m)
        compat: dict[str, bool] = {**DEFAULT_COMPAT, "cmd_shim": _is_cmd_shim(base[0])}
        model_index = 0
        attempts = 0
        max_attempts = len(models) + len(DEFAULT_COMPAT)
        stream_path = engine_dir / "stream.jsonl"
        stderr_path = engine_dir / "stderr.log"
        state = self.new_parse_state(job.model, job.max_turns, job.kind)
        attempt = _Attempt()
        retries: list[dict] = []
        try:
            while attempts < max_attempts:
                if cancel.is_set() and attempts:
                    attempt.cancelled = True  # cancelled between attempts: do not start another CLI
                    break
                attempts += 1
                model = models[model_index] if models else job.model
                argv = self.build_argv(job, run_dir, {**compat, "model": model, "fallbacks": models[model_index + 1:]})
                argv = base + argv[len(self.cfg.command or ["claude"]):]
                state = self.new_parse_state(job.model, job.max_turns, job.kind)
                attempt = self._run_once(argv, job, env, sink, cancel, state, judge, stream_path, stderr_path, started, attempts)
                if attempt.cancelled or attempt.timeout_reason:
                    break
                if state["saw_init"]:
                    refusal = self.model_unavailable_after_init(state)
                    if refusal is None or model_index + 1 >= len(models):
                        break
                    nxt = model_index + 1
                    info = {"reason": f"model {model} refused before any work: {refusal}",
                            "argv_change": f"--model {models[nxt]}", "model": models[nxt]}
                    retries.append(info)
                    self._emit(sink, EngineEvent(ENGINE_COMPAT_RETRY, info))
                    model_index = nxt
                    continue
                text = attempt.stderr_text
                if attempt.spawn_error:
                    text += "\n" + attempt.spawn_error
                if isinstance(state.get("result"), dict):
                    text += "\n" + str(state["result"].get("result") or "")
                dec = self.compat_retry_decision(text, compat)
                if dec:
                    compat.update(dec["compat"])
                    info = {"reason": dec["reason"], "argv_change": dec["argv_change"], "model": model}
                    retries.append(info)
                    self._emit(sink, EngineEvent(ENGINE_COMPAT_RETRY, info))
                    continue
                nxt = self.model_retry_decision(text, state["saw_init"], model_index, models)
                if nxt is not None:
                    info = {"reason": f"model {model} unavailable before init: {_tail(text, 300).strip()}",
                            "argv_change": f"--model {models[nxt]}", "model": models[nxt]}
                    retries.append(info)
                    self._emit(sink, EngineEvent(ENGINE_COMPAT_RETRY, info))
                    model_index = nxt
                    continue
                break
        finally:
            if judge is not None:
                try:
                    judge.forget(job.run_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("judge.forget failed: %s", exc)
        return self._build_result(job, state, attempt, started, attempts, retries, compat, run_dir)

    def _run_once(self, argv: list[str], job: EngineJob, env: dict, sink: EngineSink, cancel: threading.Event,
                  state: dict, judge, stream_path: Path, stderr_path: Path, started: float, attempt_no: int) -> _Attempt:
        attempt = _Attempt()
        judge_tools = set(self.cfg.judge_tools or [])
        throttle = _Throttle()
        last_stdout = [time.monotonic()]
        log.info("claude_code attempt %d: %s", attempt_no, " ".join(self.describe_argv(argv)))
        try:
            with open(stream_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "kyora.attempt", "n": attempt_no, "ts": time.time(),
                                     "argv": self.describe_argv(argv)}) + "\n")
        except OSError as exc:
            log.warning("cannot write %s: %s", stream_path, exc)
        unsafe = cmd_shim_unsafe_args(argv)
        if unsafe:
            attempt.spawn_error = (
                f"{argv[0]} is a .cmd/.bat shim that Windows runs through cmd.exe, which would mangle the argument "
                f"{_tail(unsafe[0], 120)!r} (line breaks, %, quotes or & | < > ^); not started. Install the native "
                "Claude Code (claude.exe), or set claude_code.command to node and the CLI's cli.js, or move "
                "KYORA_HOME / claude_code.extra_args off those characters")
            attempt.rc = -1
            self._emit(sink, EngineEvent(ENGINE_ERROR, {"error": attempt.spawn_error}))
            return attempt
        via_stdin = self.prompt_via_stdin(job, argv[0])
        try:
            proc = popen(argv, cwd=job.workspace, env=env, stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL)
        except OSError as exc:
            attempt.spawn_error = f"cannot start {argv[0]}: {exc}"
            attempt.rc = -1
            self._emit(sink, EngineEvent(ENGINE_ERROR, {"error": attempt.spawn_error}))
            return attempt
        if via_stdin:
            def _feed():
                try:
                    proc.stdin.write(job.prompt or "")
                    proc.stdin.close()
                except (OSError, ValueError) as exc:
                    log.warning("prompt stdin write failed: %s", exc)
            threading.Thread(target=_feed, name="claude-stdin", daemon=True).start()

        def read_stdout() -> None:
            try:
                with open(stream_path, "a", encoding="utf-8") as fh:
                    for line in iter(proc.stdout.readline, ""):
                        last_stdout[0] = time.monotonic()
                        fh.write(line if line.endswith("\n") else line + "\n")
                        fh.flush()
                        for ev in self.parse_stream_line(line, state):
                            self._emit(sink, ev)
                            self._after_event(ev, job, state, sink, judge, judge_tools)
            except (OSError, ValueError) as exc:
                log.warning("stdout reader stopped: %s", exc)

        def read_stderr() -> None:
            try:
                with open(stderr_path, "a", encoding="utf-8") as fh:
                    for line in iter(proc.stderr.readline, ""):
                        attempt.stderr_lines.append(line)
                        fh.write(line if line.endswith("\n") else line + "\n")
                        fh.flush()
                        stripped = line.rstrip("\r\n")
                        if stripped and throttle.allow():
                            self._emit(sink, EngineEvent(ENGINE_STDERR, {"line": stripped[:1000]}))
            except (OSError, ValueError) as exc:
                log.warning("stderr reader stopped: %s", exc)

        t_out = threading.Thread(target=read_stdout, name="claude-stdout", daemon=True)
        t_err = threading.Thread(target=read_stderr, name="claude-stderr", daemon=True)
        t_out.start()
        t_err.start()
        wall = float(self.cfg.timeout_s or 0)
        idle = float(self.cfg.idle_timeout_s or 0)
        while True:
            rc = proc.poll()
            if rc is not None:
                attempt.rc = rc
                break
            now = time.monotonic()
            if cancel.is_set():
                attempt.cancelled = True
                kill_tree(proc)
                break
            if wall and now - started > wall:
                attempt.timeout_reason = f"wall timeout after {int(now - started)} s (claude_code.timeout_s={int(wall)})"
                kill_tree(proc)
                break
            if idle and now - last_stdout[0] > idle:
                attempt.timeout_reason = f"idle timeout: no output for {int(now - last_stdout[0])} s (claude_code.idle_timeout_s={int(idle)})"
                kill_tree(proc)
                break
            time.sleep(0.25)
        t_out.join(timeout=15)
        t_err.join(timeout=15)
        try:
            attempt.rc = proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 - already killed; keep whatever rc we have
            pass
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None or (stream is proc.stdout and t_out.is_alive()) or (stream is proc.stderr and t_err.is_alive()):
                continue  # a reader still blocked on a pipe held open by a grandchild keeps its stream
            try:
                stream.close()
            except (OSError, ValueError) as exc:
                log.debug("closing child pipe failed: %s", exc)
        if throttle.suppressed:
            self._emit(sink, EngineEvent(ENGINE_STDERR, {"line": f"... {throttle.suppressed} more stderr lines in engine/stderr.log"}))
        if attempt.timeout_reason:
            self._emit(sink, EngineEvent(ENGINE_ERROR, {"error": attempt.timeout_reason}))
        return attempt

    def _after_event(self, ev: EngineEvent, job: EngineJob, state: dict, sink: EngineSink, judge, judge_tools: set) -> None:
        """Flag judged tools whose call produced no journaled verdict (hook could not reach the judge)."""
        if ev.type != ENGINE_TOOL_RESULT or judge is None:
            return
        name = ev.data.get("name")
        tid = ev.data.get("tool_use_id")
        if not name or name not in judge_tools or not tid:
            return
        try:
            has = judge.has_verdict(job.run_id, tid)
        except Exception as exc:  # noqa: BLE001
            log.warning("judge.has_verdict failed: %s", exc)
            return
        if not has:
            self._emit(sink, EngineEvent(JUDGE_UNAVAILABLE, {
                "reason": f"no judge verdict recorded for {name} ({tid}): the hook could not reach the judge, the call ran unjudged",
                "tool_name": name, "tool_use_id": tid}))

    def _build_result(self, job: EngineJob, state: dict, attempt: _Attempt, started: float, attempts: int,
                      retries: list[dict], compat: dict, run_dir: Path) -> EngineResult:
        res = state.get("result") if isinstance(state.get("result"), dict) else {}
        subtype = res.get("subtype")
        is_error = bool(res.get("is_error"))
        ok = subtype == "success" and not is_error and not attempt.cancelled and not attempt.timeout_reason
        error: str | None = None
        if not ok:
            if attempt.cancelled:
                error = "cancelled"
            elif attempt.timeout_reason:
                error = attempt.timeout_reason
            elif attempt.spawn_error:
                error = attempt.spawn_error
            elif is_error and isinstance(res.get("result"), str) and res["result"].strip():
                # the CLI reports API errors as subtype "success" + is_error; name the real terminal reason
                label = subtype if subtype and subtype != "success" else str(res.get("terminal_reason") or "error")
                status = res.get("api_error_status")
                error = f"{label}{f' (HTTP {status})' if status else ''}: {res['result'].strip()[:1500]}"
            elif _tail(attempt.stderr_text).strip():
                error = f"{subtype or f'exit code {attempt.rc}'}: {_tail(attempt.stderr_text, 1500).strip()}"
            elif subtype:
                error = str(subtype)
            else:
                error = f"claude exited with code {attempt.rc} without a result line"
        # The init line names the model the CLI was *configured* with; it only counts as the active model
        # once a real model answered (a message or a tool call). A model refused on its first request
        # (credits, not found) never served, so it is not reported as active.
        served = bool(state.get("model_messages") or state.get("tool_uses"))
        active = state.get("active_model") if served else None
        if not active:
            active = primary_model_from_usage(res.get("modelUsage"), [job.model] + list(job.fallback_models or []))
        duration_ms = int(res.get("duration_ms") or 0) or int((time.monotonic() - started) * 1000)
        denials = res.get("permission_denials") or []
        return EngineResult(
            ok=ok, backend=self.name, requested_model=job.model, active_model=active,
            fallback_used=bool(active) and not same_model(job.model, active), session_id=state.get("session_id"),
            num_turns=res.get("num_turns"), duration_ms=duration_ms,
            cost_usd=res.get("total_cost_usd") if isinstance(res.get("total_cost_usd"), (int, float)) else None,
            result_text=_result_text(res.get("result")),
            error=error, permission_denials=list(denials) if isinstance(denials, list) else [],
            raw={"rc": attempt.rc, "attempts": attempts, "retries": retries, "compat": compat,
                 "run_dir": _posix(run_dir), "stream": _posix(run_dir / "engine" / "stream.jsonl"),
                 "stderr": _posix(run_dir / "engine" / "stderr.log"), "tool_uses": state.get("tool_uses", 0),
                 "malformed_lines": state.get("malformed", 0), "model_usage": res.get("modelUsage"),
                 "duration_api_ms": res.get("duration_api_ms"), "stderr_tail": _tail(attempt.stderr_text, 1500),
                 "api_error": {k: res.get(k) for k in ("api_error_status", "api_error_code", "api_error", "terminal_reason")
                               if res.get(k) is not None} or None,
                 "rate_limits": list(state.get("rate_limits") or [])})

    # ------------------------------------------------------------------ probe (selfcheck)
    def probe_model(self, model: str, fallbacks: list[str] | None = None, timeout_s: float = 120) -> dict:
        """One-turn ``claude -p`` probe reporting which model actually answered (used by selfcheck).

        Follows the same model chain as :meth:`run`: a model the API refuses before
        any work (credits, usage limit, not found) is recorded in ``refused`` and the
        next model of ``[model] + fallbacks`` is probed.
        """
        base = self._resolve_command()
        requested = model or self.cfg.model
        out_info: dict[str, Any] = {"ok": False, "requested": requested, "active": None, "fallback_used": False,
                                    "error": None, "output_tail": "", "duration_ms": 0, "refused": []}
        if base is None:
            out_info["error"] = f"'{(self.cfg.command or ['claude'])[0]}' not found on PATH"
            return out_info
        chain: list[str] = [requested]
        for m in (fallbacks if fallbacks is not None else self.cfg.fallback_models):
            if m and m not in chain:
                chain.append(m)
        compat = dict(DEFAULT_COMPAT)
        env = self._base_env()
        started = time.monotonic()
        rc, out, err = -1, "", ""
        parsed: Any = None
        idx = 0
        for _ in range(len(chain) + len(DEFAULT_COMPAT)):
            current, rest = chain[idx], chain[idx + 1:]
            argv = base + ["-p", PROBE_PROMPT, "--model", current]
            if rest:
                argv += ["--fallback-model", ",".join(rest)]
            if compat["max_turns"]:
                argv += ["--max-turns", "1"]
            argv += ["--output-format", "json", "--permission-mode", "acceptEdits"]
            if compat["permission_prompts"]:
                argv += ["--permission-prompts", "none"]
            try:
                rc, out, err = run_cmd(argv, timeout=timeout_s, env=env, cwd=self.home)
            except OSError as exc:
                out_info["error"] = f"cannot execute {base[0]}: {exc}"
                return out_info
            dec = self.compat_retry_decision(err, compat) if rc != 0 else None
            if dec:
                compat.update(dec["compat"])
                continue
            parsed = self._parse_json_blob(out)
            if rest:
                if isinstance(parsed, dict):
                    refusal = self.model_unavailable_after_init(
                        {"saw_init": True, "result": parsed, "tool_uses": 0, "model_messages": 0})
                elif rc != 0 and self.model_retry_decision(err or out, False, idx, chain) is not None:
                    refusal = _tail(err or out, 300).strip()
                else:
                    refusal = None
                if refusal:
                    out_info["refused"].append({"model": current, "reason": refusal})
                    idx += 1
                    continue
            break
        out_info["duration_ms"] = int((time.monotonic() - started) * 1000)
        active = None
        if isinstance(parsed, dict):
            active = primary_model_from_usage(parsed.get("modelUsage"), chain)
            ok = rc == 0 and parsed.get("subtype") == "success" and not parsed.get("is_error")
            out_info.update({"ok": bool(ok), "active": active, "result": str(parsed.get("result") or "")[:200],
                             "num_turns": parsed.get("num_turns"), "cost_usd": parsed.get("total_cost_usd")})
            if not ok:
                out_info["error"] = str(parsed.get("result") or parsed.get("subtype") or _tail(err, 400)).strip()[:600]
        else:
            out_info["error"] = (_tail(err, 600).strip() or _tail(out, 600).strip() or f"rc={rc}") if rc != 0 else "no JSON result"
            if rc == -9:
                out_info["error"] = f"probe timed out after {int(timeout_s)} s"
        out_info["fallback_used"] = bool(active) and not same_model(requested, active)
        out_info["output_tail"] = (_tail(out, 600) + ("\n" + _tail(err, 400) if err.strip() else "")).strip()
        out_info["compat"] = compat
        return out_info
