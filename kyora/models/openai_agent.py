"""The Kyora agent engine: Kyora's own tool loop on any OpenAI-compatible chat endpoint.

BUILD and FIX keep working without a Claude subscription and without an
Anthropic API key. The same Claude models stay reachable without a
subscription through an Anthropic API key (the ``anthropic_api`` engine, pay
per use); THIS engine runs a different model - by default ``z-ai/glm-5.3`` on
NVIDIA's API Catalog with the user's own NVIDIA keys, then the configured
fallback models. Its capability is not claimed to equal Claude's: the quality
gate (>= 94 % of the measured checks) is unchanged, so its output is measured
exactly like every other engine's.

The loop is plain OpenAI function calling: each turn is one streamed
``/chat/completions`` request with three function tools - ``bash`` and
``str_replace_based_edit_tool`` (the same judged, workspace-confined tools as
the Messages-API engine, :class:`~kyora.models.agent_tools.ToolLoopEngine`)
and ``kyora_check``, which runs Kyora's own static checks and the workspace's
Node logic tests (``node --test``, as the TEST stage does) so the model can
measure its work before it stops; it writes no file itself, and its run of the
model-written logic tests is judged like the Bash command it is. Every
Write/Edit/Bash call is judged before it runs. The run's deadline and cancel
reach the tools too: a call after the deadline is not run, and a running bash
command is killed.

What can go wrong at the endpoint is handled, and said, never hidden: a key
the endpoint refuses (401/403) is replaced by the configured alternate key
(:class:`~kyora.models.nim_client.NIMClient`), a retired or unknown model
(404/410) or one that stays overloaded is replaced by the next fallback model
(:func:`~kyora.models.nim_client.call_with_model_fallback`); a model that
refuses function tools is skipped for this engine. A transient failure (the
endpoint unreachable, every model briefly overloaded, a 429, an answer the
endpoint ended with ``finish_reason`` ``error``/``abort``) retries the same
turn after a growing wait, at most :data:`MAX_TRANSIENT_RETRIES` times in a
row and never past the deadline. A failure that is left is classified in
``EngineResult.raw["failure_kind"]`` (``auth``, ``quota``, ``unavailable``,
``model``) so the pipeline can fail over to another engine.
Costs are not reported by these endpoints and stay ``None``; token totals are
the endpoint's own ``usage`` numbers.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from ..pipeline.events import (
    ENGINE_ERROR, ENGINE_MODEL_RESOLVED, ENGINE_RESULT, ENGINE_SESSION, ENGINE_STDERR, ENGINE_TEXT, ENGINE_THINKING,
    ENGINE_TOOL_USE, STAGE_PROGRESS,
)
from ..util.fs import read_json
from . import agent_tools
from .agent_tools import BASH_TIMEOUT_S, BASH_TOOL, EDITOR_TOOL, EditorError, ToolLoopEngine, _posix, _truncate, confine
from .base import Availability, EngineJob, EngineResult, EngineSink
from .kimi import model_family
from .nim_client import (
    BUSY_COOLDOWN_S, MODEL_GONE_STATUSES, ChatResult, ModelChain, NIMCancelled, NIMClient, NIMError, NIMTimeout,
    NIMUnreachable, _retry_after_raw,
    call_with_model_fallback, explain_http_failure, function_tool, is_temporary_failure, is_tool_rejection,
    strip_think, url_host,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..config import AgentEngineCfg

log = logging.getLogger("kyora.openai_agent")

ROLE = "agent"
CHECK_TOOL = "kyora_check"
PROBE_TOOL = "kyora_probe"
CHECK_JUDGE_NAME = "KyoraCheck"
USER_AGENT = "kyora-agent"
MAX_NUDGES = 2
MAX_CONTINUES = 3
MAX_CONTEXT_RETRIES = 3       # each one keeps half as many tool results: 4, 2, 1
KEEP_TOOL_MESSAGES = 8
MAX_TRANSIENT_RETRIES = 6     # retries of one turn in a row after transient failures
RETRY_BASE_S = 5.0            # first wait before such a retry; doubled each time
MAX_RETRY_WAIT_S = 60.0
RETRY_MIN_LEFT_S = 10.0       # time a retried request needs before the run deadline (the loop's shortest request)
TRANSIENT_FINISH_REASONS = ("error", "abort")  # the server gave up on this answer; the same request may work
MAX_TEXT_CALL_NUDGES = 3      # answers that wrote tool calls as text instead of calling the tools
MAX_THINKING_RETRIES = 3      # answers cut off at max_tokens while still thinking
# reasoning characters an answer may stream, with no text and no tool call, after thinking was switched OFF: a model
# that ignores the switch is stopped there (live 2026-10-23: GLM-5.3 reasoned 16384 tokens - 29 minutes - twice)
MAX_UNREQUESTED_REASONING = 8000
# ...or, slower, this many characters once the request is this old (live 2026-10-24: GLM-5.3 took 135-185 s to stream
# the 8000 characters, three times in one run)
SLOW_UNREQUESTED_REASONING = 2000
MAX_UNREQUESTED_REASONING_S = 90.0
# such a model is sidelined for this long (a sticky cooldown: answering does not end it; the chain, and so the wait,
# carries over to the next BUILD/FIX), so it does not stand in for a model that is only rate limited (live
# 2026-10-24: each time it did, it reasoned again)
RUNAWAY_COOLDOWN_S = 1800.0
MAX_RETRY_TOKENS = 32768      # cap of the doubled max_tokens of such a retry
MAX_TOOL_CALLS_PER_TURN = 32
CHECK_TIMEOUT_S = 120         # the Node logic tests of one kyora_check (NodeRunner's default)
CHECK_COMMAND = "node --test tests/logic-tests.mjs"  # what kyora_check runs of the model's code, as judged
CHECK_JUDGE_CONTENT = 200000  # characters of the test file shown to the judge (and in its cache key)
EVENT_TEXT_LIMIT = 4000
JOURNAL_TEXT_LIMIT = 20000
MAX_ARG_KEEP = 2000           # tool-call argument strings longer than this may be elided from old turns
MIN_CONTEXT_BUDGET = 20000   # floor of the budget after the endpoint said the conversation is too long
REQUEST_TIMEOUT_S = 600.0     # socket timeout of one request; the run's wall clock is agent_engine.timeout_s
PROBE_MAX_TOKENS = 1024
PROBE_RETRY_MAX_TOKENS = 4 * PROBE_MAX_TOKENS  # once, after the probe's answer was cut off before the call
MAX_CHECK_LINES = 40
STOPPED_REASONS = ("content_filter", "error", "abort", "cancelled")  # an answer the endpoint cut off, not a final one
STOPPED_CALL = "the endpoint stopped this answer (finish_reason={reason}); the call was not run"
TOO_MANY_CALLS = (f"more than {MAX_TOOL_CALLS_PER_TURN} tool calls in one answer: this call was not run; send at most "
                  f"{MAX_TOOL_CALLS_PER_TURN} calls per answer")
ELIDED_PREFIX = "[output elided to save context:"
DISABLED_REASON = "the Kyora agent engine is disabled in Settings (agent_engine.enabled)"
NO_KEY_REASON = ("no API key for the agent endpoint ({endpoint}): add an NVIDIA API key in Settings (build.nvidia.com "
                 "-> Get API Key) or set keys.agent.api_key for another OpenAI-compatible endpoint")
NO_ENDPOINT_REASON = "no endpoint configured for the agent engine (keys.agent.base_url or nvidia.base_url)"
_TEMPORARY_RE = re.compile(r"overload|temporarily unavailable|service unavailable", re.IGNORECASE)
_QUOTA_RE = re.compile(r"quota|insufficient.?(?:balance|credit|funds)|out of credits|payment required", re.IGNORECASE)
_CONTEXT_RE = re.compile(r"context (?:length|window)|maximum context|too many tokens|prompt is too long|"
                         r"input is too long|reduce the length|maximum input", re.IGNORECASE)
_NO_CREDIT_RE = re.compile(r"insufficient.?(?:balance|credit|funds)|out of credits|payment required", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after\D{0,4}(\d+(?:\.\d+)?)|(?:retry|try again) in (\d+(?:\.\d+)?)\s*(ms)?",
                             re.IGNORECASE)
_GENERIC_ID_RE = re.compile(r"call_\d+")  # what the client fills in when the endpoint sends no id: same every turn
# tool-call markup some chat templates leave in the text when the server does not parse it into tool_calls
_TEXT_CALL_RE = re.compile(r"<tool_call>|<\|tool_call|<function=", re.IGNORECASE)
TEXT_CALL_SCAN = 100000       # characters of an answer searched for a JSON tool call written as text
_JSON = json.JSONDecoder()
_SPEC_TEST_RE = re.compile(r"^###\s+`([^`]+)`\s+-\s+(.*)$")
_SPEC_KIND_RE = re.compile(r"^-\s*kind:\s*([A-Za-z]+)(\s+-\s+REQUIRED)?", re.IGNORECASE)

BASH_FUNCTION = function_tool(BASH_TOOL, (
    "Run a shell command in the project workspace root. Every call starts a fresh shell (cd and exported variables "
    f"do not persist), runs at most {BASH_TIMEOUT_S} s and has no API keys in its environment; "
    f"{agent_tools.BASH_BACKGROUND_NOTE}. Kyora judges every command before it runs."), {
    "type": "object",
    "properties": {"command": {"type": "string", "description": "The shell command to run."}},
    "required": ["command"],
})
EDITOR_FUNCTION = function_tool(EDITOR_TOOL, (
    "View, create and edit text files inside the project workspace. view: a file with line numbers, or a directory "
    "listing. create: write the complete file (file_text). str_replace: replace old_str, which must occur exactly "
    "once, with new_str. insert: insert new_str after line insert_line (0 = top of the file). Kyora judges every "
    "write before it runs."), {
    "type": "object",
    "properties": {
        "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"],
                    "description": "The editor command."},
        "path": {"type": "string", "description": "File or directory path, relative to the workspace root."},
        "file_text": {"type": "string", "description": "create: the complete content of the file."},
        "old_str": {"type": "string", "description": "str_replace: the exact text to replace (must be unique)."},
        "new_str": {"type": "string", "description": "str_replace: the replacement text. insert: the text to insert."},
        "insert_line": {"type": "integer", "description": "insert: the line after which new_str is inserted (0 = top)."},
        "view_range": {"type": "array", "items": {"type": "integer"},
                       "description": "view: two integers [first, last] (1-based; last -1 = end of the file)."},
    },
    "required": ["command", "path"],
})
CHECK_FUNCTION = function_tool(CHECK_TOOL, (
    "Run Kyora's measured static checks (entry page, manifest, HTML, JSON and JavaScript syntax, local-only "
    "dependencies, size, test ids) and the Node logic tests on the workspace now. Returns every failing check, or "
    "'all N checks pass'. Writes no file itself; Kyora judges the run of tests/logic-tests.mjs like a bash command."),
    {"type": "object", "properties": {}})
PROBE_FUNCTION = function_tool(PROBE_TOOL, "Confirms that the connection works. Call it with ok=true.", {
    "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
PROBE_PROMPT = ("Connectivity test from Kyora. Call the function kyora_probe with the arguments {\"ok\": true}. "
                "Do not answer with text.")

AGENT_NOTE = """

# Environment (Kyora agent engine: OpenAI-compatible function calling)
- Workspace root and working directory: {workspace}
- Call the tools as functions with JSON arguments; several calls in one answer run in the order given.
{bash_line}
- `str_replace_based_edit_tool`: `view` shows a file with line numbers (or lists a directory), `create` writes a
  whole file, `str_replace` replaces `old_str` (it must occur exactly once) with `new_str`, `insert` puts `new_str`
  after line `insert_line` (0 = top). Paths are relative to the workspace root; paths outside it are rejected. One
  answer is cut off after {max_tokens} tokens: write a large file in parts (create, then insert or str_replace).
{check_line}- Kyora judges every file write/edit and every bash command before it runs; a denied call returns an error with
  the reason - adapt instead of retrying the same call.
- When the work is complete, answer with a short plain-text summary and no tool call: that ends the session.
"""
BASH_LINE = ("- `bash`: every command starts in a fresh shell in the workspace root (cd and exported variables do not\n"
             "  persist between calls), runs at most {timeout} s, and has no API keys or tokens in its environment;\n"
             f"  {agent_tools.BASH_BACKGROUND_NOTE}.")
NO_BASH_LINE = ("- There is no POSIX shell on this device, so there is no `bash` tool: create and edit files with\n"
                "  `str_replace_based_edit_tool`.")
CHECK_LINE = ("- `kyora_check`: runs Kyora's static checks{node} on the workspace right now and lists every\n"
              "  failing check (or says that all pass). Run it when the game is written and fix what fails before you\n"
              "  finish; the in-browser acceptance tests run later in Kyora's harness.\n")
NUDGE = ("You stopped, but the work is not finished: {files} {verb} not exist in the workspace yet. Create {them} with "
         "str_replace_based_edit_tool (command \"create\") through the function-calling interface (not as text){check}, "
         "then end with a short summary of what you changed.")
TEXT_CALL_NUDGE = ("Your answer wrote a tool call as text ({sample}). Kyora runs only tools called through the "
                   "function-calling interface, so nothing was run. Call {tools} as functions with JSON arguments; when "
                   "the work is complete, answer with a short plain-text summary and no tool-call markup.")
IDLE_NUDGE = ("You stopped {how}. Kyora changes the workspace only through the function tools ({tools}), called "
              "through the function-calling interface. If work is left, do it with those tools now; if the work is "
              "complete, answer with a short plain-text summary of what was done.")


# --------------------------------------------------------------------------- helpers
def endpoint_host(base_url: str | None) -> str:
    """The host (and port) of an endpoint URL - never its user info, path or query, which may carry tokens."""
    return url_host(base_url)


def family_extra(model: str, thinking: bool) -> dict:
    """Request fields a model family reads: Nemotron's thinking switch both ways; for GLM (which thinks by
    default) only the switch-off, so ``agent_engine.thinking: false`` can save its reasoning on every turn;
    nothing for other families."""
    if model_family(model) == "nemotron":
        return {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}
    if not thinking and "glm" in (model or "").lower():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def _thinking_sent(extra: Mapping) -> bool:
    kwargs = extra.get("chat_template_kwargs") if isinstance(extra, Mapping) else None
    return isinstance(kwargs, Mapping) and kwargs.get("enable_thinking") is True


def _clip(text: str, limit: int = EVENT_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def _int_like(value: Any) -> Any:
    """``"12"`` / ``12.0`` -> ``12`` (models sometimes quote integers); anything else unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    return value


def _coerce(args: dict) -> dict:
    """Light, lossless repairs of editor arguments (quoted integers, a JSON-encoded view_range)."""
    out = dict(args)
    if "insert_line" in out:
        out["insert_line"] = _int_like(out["insert_line"])
    vr = out.get("view_range")
    if isinstance(vr, str) and vr.strip().startswith("["):
        try:
            vr = json.loads(vr)
        except json.JSONDecodeError:
            pass
    if isinstance(vr, list):
        out["view_range"] = [_int_like(v) for v in vr]
    return out


def parse_arguments(raw: Any) -> tuple[dict | None, str | None]:
    """``(arguments, None)`` for a tool call's ``function.arguments``, or ``(None, error)`` when they are not a JSON object."""
    if isinstance(raw, Mapping):
        return _coerce(dict(raw)), None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if not isinstance(raw, str):
        return None, f"invalid JSON arguments: expected a JSON object, got {type(raw).__name__}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (f"invalid JSON arguments: {exc.msg} at line {exc.lineno} column {exc.colno} (character {exc.pos} "
                      f"of {len(raw)}); send the arguments as one complete JSON object")
    if isinstance(value, str):  # double-encoded by some gateways
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if not isinstance(value, dict):
        return None, f"invalid JSON arguments: expected a JSON object, got {type(value).__name__}"
    return _coerce(value), None


def endpoint_wait_s(exc: BaseException) -> float | None:
    """Seconds the endpoint asked to wait before a retry (``Retry-After`` header or wording in the body), uncapped."""
    for attr in ("retry_after", "retry_after_s"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    value = _retry_after_raw(getattr(exc, "headers", None))
    if value is not None:
        return value
    m = _RETRY_AFTER_RE.search(f"{getattr(exc, 'body', None) or ''} {exc}")
    if not m:
        return None
    seconds = float(m.group(1) or m.group(2))
    if m.group(3):
        seconds /= 1000.0
    return seconds


def retry_after_s(exc: BaseException) -> float | None:
    """:func:`endpoint_wait_s` capped at :data:`MAX_RETRY_WAIT_S`."""
    value = endpoint_wait_s(exc)
    return None if value is None else min(value, MAX_RETRY_WAIT_S)


def text_tool_call(text: str, tool_names: list[str]) -> str | None:
    """The tool-call markup in an answer's text (``<tool_call>``, ``<|tool_call...``, ``<function=``, or a JSON
    ``{"name": <tool>, "arguments": {...}}``), as a short sample - or ``None``. Such a call is never run."""
    if not text:
        return None
    m = _TEXT_CALL_RE.search(text)
    if m:
        return " ".join(text[m.start():m.start() + 80].split())
    text = text[:TEXT_CALL_SCAN]
    pos = 0
    for _ in range(8):  # a few "name" keys, each decoded from the nearest braces before it: linear, never a full scan
        key = text.find('"name"', pos)
        if key < 0:
            return None
        pos = key + 6
        start = key
        for _level in range(3):
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            try:
                obj, _end = _JSON.raw_decode(text, start)
            except ValueError:
                continue
            if isinstance(obj, Mapping) and isinstance(obj.get("function"), Mapping):
                obj = obj["function"]
            if isinstance(obj, Mapping) and obj.get("name") in tool_names and ("arguments" in obj or "parameters" in obj):
                return f'{{"name": "{obj["name"]}", "arguments": ...}}'
    return None


def _msg_chars(m: Mapping) -> int:
    """Characters a message adds to the context: content, echoed reasoning and tool-call arguments."""
    n = len(m.get("content") or "") if isinstance(m.get("content"), str) else len(json.dumps(m.get("content") or ""))
    n += len(m.get("reasoning_content") or "")
    for tc in m.get("tool_calls") or []:
        n += len(str(((tc or {}).get("function") or {}).get("arguments") or ""))
    return n


def context_chars(msgs: list[dict]) -> int:
    return sum(_msg_chars(m) for m in msgs)


def elide_context(msgs: list[dict], limit: int, keep: int = KEEP_TOOL_MESSAGES, *, deep: bool = False) -> dict:
    """Shrink ``msgs`` in place below ``limit`` characters without removing a message.

    Only what lies before the ``keep`` newest tool results (and the assistant turns that asked for them) is
    touched, oldest first: tool results are replaced by ``[output elided to save context: N chars]``, then
    echoed reasoning is dropped, then long string arguments of old tool calls (file contents already
    written) are replaced by a marker. ``deep`` (after the endpoint said the conversation is too long):
    reasoning and long arguments may go from every assistant message but the newest, also when there
    are no more than ``keep`` tool results. Every
    tool call keeps its id and every tool result its ``tool_call_id``, so the pairing stays valid.
    Returns ``{"messages", "chars"}`` - how many messages changed and how many characters were saved.
    """
    total = context_chars(msgs)
    stats = {"messages": 0, "chars": 0}
    if total <= limit:
        return stats
    keep = max(1, int(keep))
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    old_tools = tool_idx[:-keep] if len(tool_idx) > keep else []
    if not old_tools and not deep:
        return stats
    boundary = 0
    if old_tools:
        boundary = tool_idx[-keep]  # from the assistant message that owns the oldest kept result on, all stays intact
        while boundary > 0 and msgs[boundary - 1].get("role") == "tool":
            boundary -= 1
        if boundary > 0 and msgs[boundary - 1].get("role") == "assistant":
            boundary -= 1
    if deep:  # everything before the newest assistant message
        boundary = max([boundary] + [i for i, m in enumerate(msgs) if m.get("role") == "assistant"])

    def saved(before: int, after: int) -> None:
        nonlocal total
        total -= before - after
        stats["messages"] += 1
        stats["chars"] += before - after

    for i in old_tools:
        if total <= limit:
            return stats
        content = msgs[i].get("content")
        if not isinstance(content, str) or content.startswith(ELIDED_PREFIX):
            continue
        marker = f"{ELIDED_PREFIX} {len(content)} chars]"
        if len(marker) < len(content):
            msgs[i] = {**msgs[i], "content": marker}
            saved(len(content), len(marker))
    for i in range(boundary):
        if total <= limit:
            return stats
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("reasoning_content"):
            msgs[i] = {k: v for k, v in m.items() if k != "reasoning_content"}
            saved(len(m["reasoning_content"]), 0)
    for i in range(boundary):
        if total <= limit:
            return stats
        m = msgs[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        calls, before, after = [], 0, 0
        for tc in m["tool_calls"]:
            fn = dict((tc or {}).get("function") or {})
            raw = str(fn.get("arguments") or "")
            args, err = parse_arguments(raw)
            if err is None and args and any(isinstance(v, str) and len(v) > MAX_ARG_KEEP for v in args.values()):
                short = {k: (f"[elided to save context: {len(v)} chars; view the file for its current content]"
                             if isinstance(v, str) and len(v) > MAX_ARG_KEEP else v) for k, v in args.items()}
                fn["arguments"] = json.dumps(short, ensure_ascii=False)
            before += len(raw)
            after += len(str(fn.get("arguments") or ""))
            calls.append({**tc, "function": fn})
        if after < before:
            msgs[i] = {**m, "tool_calls": calls}
            saved(before, after)
    return stats


def testspec_tests(workspace: Path) -> list[dict]:
    """Acceptance tests listed in the workspace's ``KYORA_TESTSPEC.md`` (written by Kyora before BUILD/FIX)."""
    path = Path(workspace) / "KYORA_TESTSPEC.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = _SPEC_TEST_RE.match(line.strip())
        if not m:
            continue
        kind, required = "browser", False
        k = _SPEC_KIND_RE.match(lines[i + 1].strip()) if i + 1 < len(lines) else None
        if k:
            kind, required = k.group(1).lower(), bool(k.group(2))
        out.append({"id": m.group(1), "name": m.group(2).strip(), "kind": kind, "required": required})
    return out


def check_plan(job: EngineJob, workspace: Path, run_dir: Path) -> dict:
    """Files, constraints and acceptance tests for the static checks, as the TEST stage measures them.

    The run's PLAN artifact (``<run>/plan.json``) when it exists; otherwise the judge context Kyora sent
    with the job and the acceptance tests listed in ``KYORA_TESTSPEC.md``.
    """
    ctx = job.judge_context or {}
    planned = read_json(Path(run_dir) / "plan.json", default=None)
    src = planned if isinstance(planned, Mapping) else {}
    inner = ctx.get("plan") if isinstance(ctx.get("plan"), Mapping) else {}
    files = src.get("files") or ctx.get("files") or inner.get("files") or []
    constraints = src.get("constraints") or ctx.get("constraints") or inner.get("constraints") or {}
    tests = src.get("acceptance_tests") or ctx.get("acceptance_tests") or testspec_tests(workspace)
    return {"files": files, "constraints": constraints, "acceptance_tests": tests}


def summarize_checks(checks: list, node: str | None) -> str:
    """Compact text of a check list for the model: the failing checks, or ``all N checks pass``."""
    counted = [c for c in checks if c.status in ("pass", "fail")]
    fails = [c for c in counted if c.status == "fail"]
    deferred = [c for c in checks if c.status == "deferred"]
    lines: list[str] = []
    if fails:
        lines.append(f"{len(fails)} of {len(counted)} checks fail ({sum(1 for c in fails if c.required)} required):")
        for c in fails[:MAX_CHECK_LINES]:
            detail = " ".join(str(c.detail or c.name or "").split())
            lines.append(f"- FAIL {c.id}{' [required]' if c.required else ''}: {detail[:300]}")
        if len(fails) > MAX_CHECK_LINES:
            lines.append(f"- ... and {len(fails) - MAX_CHECK_LINES} more failing checks")
    else:
        lines.append(f"all {len(counted)} checks pass")
    if deferred:
        lines.append(f"{len(deferred)} deferred (no Node on this device; the browser harness verifies them later): "
                     + ", ".join(c.id for c in deferred[:10]))
    for c in checks:
        if c.status == "skip" and c.id == "node:logic-tests":
            lines.append(f"Node logic tests not run: {c.detail}")
    lines.append("Not measured here: the in-browser acceptance tests, which Kyora's harness runs after this session.")
    return "\n".join(lines)


class _RunStop(threading.Event):
    """Set when the user cancels OR the run's wall-clock deadline passes - what a request polls while it streams."""

    def __init__(self, cancel: threading.Event, deadline: float):
        super().__init__()
        self.cancel = cancel
        self.deadline = deadline

    def timed_out(self) -> bool:
        return time.monotonic() >= self.deadline

    def is_set(self) -> bool:
        return self.cancel.is_set() or self.timed_out() or super().is_set()

    def wait(self, timeout: float | None = None) -> bool:
        end = self.deadline if timeout is None else min(self.deadline, time.monotonic() + max(0.0, float(timeout)))
        while not self.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            self.cancel.wait(min(left, 0.5))
        return self.is_set()


class _TurnStop(threading.Event):
    """The run's stop for one request, plus Kyora's own reason to end that request early (:meth:`stop`)."""

    def __init__(self, run_stop: threading.Event):
        super().__init__()
        self.run_stop = run_stop
        self.reason: str | None = None

    def stop(self, reason: str) -> None:
        self.reason = reason
        super().set()

    def is_set(self) -> bool:
        return super().is_set() or self.run_stop.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while not self.is_set():
            left = 0.5 if end is None else min(0.5, end - time.monotonic())
            if left <= 0:
                break
            self.run_stop.wait(left)
        return self.is_set()


class _RunawayReasoning(Exception):
    """A model asked not to think streamed reasoning and nothing else past the limits (:data:`MAX_UNREQUESTED_REASONING`
    characters, or :data:`SLOW_UNREQUESTED_REASONING` after :data:`MAX_UNREQUESTED_REASONING_S` seconds)."""

    def __init__(self, model: str, chars: int, reasoning: str, seconds: float | None = None):
        took = f" in {seconds:.0f} s" if seconds is not None else ""
        super().__init__(f"{model} kept reasoning ({chars} characters{took}, no text and no tool call) although "
                         "thinking was switched off")
        self.model, self.chars, self.reasoning, self.seconds = model, chars, reasoning, seconds


class _ProbeBudget(NIMError):
    """The probe's time budget ran out before the next model could be tried."""


# --------------------------------------------------------------------------- engine
class OpenAIAgentEngine(ToolLoopEngine):
    """Kyora's own code engine on an OpenAI-compatible endpoint (default: NVIDIA API Catalog with the user's keys).

    Constructed as ``OpenAIAgentEngine(cfg, judge, home, client_factory=None)``; ``cfg`` is a
    zero-argument callable returning the current :class:`~kyora.config.AgentEngineCfg` (so Settings
    changes apply to the next run) or the config itself. ``client_factory(cfg)`` replaces the
    :class:`NIMClient` (tests). The model chain - which models were found gone - is kept across runs
    while the endpoint, model and fallbacks stay the same. ``transient_retries`` and ``retry_base_s``
    bound the retries of a turn after transient failures (tests make them fast).
    """

    name = "openai_agent"
    transient_retries = MAX_TRANSIENT_RETRIES
    retry_base_s = RETRY_BASE_S

    def __init__(self, cfg: "Callable[[], AgentEngineCfg] | AgentEngineCfg", judge, home: Path | str, *,
                 client_factory: "Callable[[AgentEngineCfg], NIMClient] | None" = None):
        super().__init__(judge, home)
        self._cfg = cfg
        self.client_factory = client_factory
        self._chain_lock = threading.Lock()
        self._chain_key: tuple | None = None
        self._chain: ModelChain | None = None

    # ------------------------------------------------------------------ configuration
    @property
    def cfg(self) -> "AgentEngineCfg":
        return self._cfg() if callable(self._cfg) else self._cfg

    def _client(self, c: "AgentEngineCfg") -> NIMClient:
        if self.client_factory is not None:
            return self.client_factory(c)
        return NIMClient(c.base_url, c.api_key, key_label=c.key_label, alt_keys=list(c.alt_keys or []),
                         user_agent=USER_AGENT)

    def _chain_for(self, c: "AgentEngineCfg") -> ModelChain:
        """The model chain for this endpoint/model/fallbacks; a fresh one when all of its models were found gone."""
        key = (c.base_url, c.model, tuple(c.fallback_models or ()))
        with self._chain_lock:
            chain = self._chain
            if chain is None or self._chain_key != key or len(chain.gone()) >= 1 + len(chain.fallbacks):
                chain = self._chain = ModelChain(c.model, list(c.fallback_models or []))
                self._chain_key = key
            return chain

    def _forget_chain(self, chain: ModelChain) -> None:
        """No model of ``chain`` could serve: the next run (or probe) tries every model again."""
        with self._chain_lock:
            if self._chain is chain:
                self._chain = None
                self._chain_key = None

    @staticmethod
    def tool_schemas(*, bash: bool = True, self_check: bool = True) -> list[dict]:
        """The function tools of a run: ``bash`` (when a POSIX shell exists), the editor, ``kyora_check`` (Settings)."""
        tools = [BASH_FUNCTION] if bash else []
        tools.append(EDITOR_FUNCTION)
        if self_check:
            tools.append(CHECK_FUNCTION)
        return [json.loads(json.dumps(t)) for t in tools]

    # ------------------------------------------------------------------ availability
    def _availability(self, c: "AgentEngineCfg") -> Availability:
        details = {"model": c.model, "fallback_models": list(c.fallback_models or []),
                   "endpoint": endpoint_host(c.base_url), "key_label": c.key_label if c.api_key else None,
                   "enabled": bool(c.enabled), "alt_key_labels": [label for label, key in (c.alt_keys or []) if key]}
        if not c.enabled:
            return Availability(False, self.name, DISABLED_REASON, details)
        if not c.api_key:
            return Availability(False, self.name, NO_KEY_REASON.format(endpoint=details["endpoint"] or "?"), details)
        if not str(c.base_url or "").strip():
            return Availability(False, self.name, NO_ENDPOINT_REASON, details)
        return Availability(True, self.name, "", details)

    def availability(self) -> Availability:
        """Available when enabled in Settings and an API key for the endpoint is configured."""
        try:
            c = self.cfg
        except Exception as exc:  # noqa: BLE001 - a broken settings file must not break engine selection
            return Availability(False, self.name, f"agent engine settings unreadable: {type(exc).__name__}: {exc}", {})
        return self._availability(c)

    # ------------------------------------------------------------------ prompts
    def system_prompt(self, job: EngineJob, workspace: Path, c: "AgentEngineCfg", *, bash: bool, node: bool) -> str:
        """``job.system_prompt`` plus a note on the workspace and how the tools behave in this engine."""
        bash_line = BASH_LINE.format(timeout=BASH_TIMEOUT_S) if bash else NO_BASH_LINE
        check_line = ""
        if c.self_check_tool:
            check_line = CHECK_LINE.format(node=" and the Node logic tests (tests/logic-tests.mjs)" if node else
                                           " (Node is not installed here: JavaScript syntax is verified later in the browser)")
        return (job.system_prompt or "").rstrip() + AGENT_NOTE.format(
            workspace=_posix(workspace), bash_line=bash_line, max_tokens=c.max_tokens, check_line=check_line)

    @staticmethod
    def missing_files(job: EngineJob, workspace: Path) -> list[str]:
        """``index.html`` and the planned files (``judge_context["files"]`` paths) that do not exist in the workspace."""
        wanted = ["index.html"]
        for f in (job.judge_context or {}).get("files") or []:
            path = f.get("path") if isinstance(f, Mapping) else f
            if isinstance(path, str) and path.strip() and not path.strip().endswith("/"):
                wanted.append(path.strip())
        missing = []
        for rel in dict.fromkeys(wanted):
            try:
                target = confine(workspace, rel)
            except EditorError:
                continue  # a planned path outside the workspace is the judge's business, not a nudge
            if not target.is_file():
                missing.append(rel)
        return missing

    # ------------------------------------------------------------------ tools
    def _describe(self, name: str, inp: Any) -> tuple[str, str]:
        if name == CHECK_TOOL:
            return CHECK_JUDGE_NAME, "static checks + Node logic tests"
        return super()._describe(name, inp)

    def _handle_tool(self, job: EngineJob, sink: EngineSink, block: Any, workspace: Path, run_dir: Path,
                     state: dict) -> tuple[str, bool]:
        if isinstance(block, Mapping) and block.get("error"):
            return f"Error: {block['error']}", True
        name = str(block.get("name") or "") if isinstance(block, Mapping) else ""
        offered = state.get("tool_names") or [BASH_TOOL, EDITOR_TOOL]
        if name == CHECK_TOOL and CHECK_TOOL in offered:
            denied = self._judge_check(job, sink, block, workspace, state)
            if denied is not None:
                return denied, True
            timeout_s = self._tool_timeout(state, CHECK_TIMEOUT_S)
            if timeout_s <= 0:
                return agent_tools.DEADLINE_PASSED, True
            return self.kyora_check(job, workspace, run_dir, timeout_s=timeout_s, cancel=state.get("tool_cancel")), False
        if name not in (BASH_TOOL, EDITOR_TOOL):
            return f"Error: unknown tool {name!r} (available: {', '.join(offered)})", True
        return super()._handle_tool(job, sink, block, workspace, run_dir, state)

    def _judge_check(self, job: EngineJob, sink: EngineSink, block: Mapping, workspace: Path,
                     state: dict) -> str | None:
        """Judge kyora_check's run of the model-written logic tests as the Bash command it is.

        Returns the tool result of a deny, or ``None`` (allowed, not judged, or nothing of the model's
        code would run: no test file or no Node).
        """
        from ..pipeline.quality import LOGIC_TESTS_PATH, find_node  # noqa: PLC0415 - pipeline imports models
        test_file = Path(workspace) / LOGIC_TESTS_PATH
        if not test_file.is_file() or not find_node():
            return None
        try:
            with open(test_file, encoding="utf-8", errors="replace") as fh:
                content = fh.read(CHECK_JUDGE_CONTENT)  # what runs: shown to the judge, part of its cache key
        except OSError:
            content = ""
        tid = str(block.get("id") or "")
        verdict = self._judge_call(job, sink, "Bash", {"command": CHECK_COMMAND, "file_path": _posix(test_file),
                                                       "content": content}, tid, workspace)
        if verdict is None or str(verdict.get("decision", "")).lower() != "deny":
            return None
        reason = str(verdict.get("reason") or "no reason given")
        state["denials"].append({"tool_name": "Bash", "tool_use_id": tid, "reason": reason,
                                 "source": verdict.get("source")})
        return f"Denied by the Kyora judge: {reason}"

    def kyora_check(self, job: EngineJob, workspace: Path, run_dir: Path, *,
                    timeout_s: float = CHECK_TIMEOUT_S, cancel=None) -> str:
        """Kyora's static checks (+ Node logic tests when Node exists), as the TEST stage runs them; writes no file.

        ``timeout_s`` bounds the logic tests (the run deadline caps it). The judge is asked in :meth:`_handle_tool`.
        """
        from ..pipeline.quality import NodeRunner, StaticChecker, find_node  # noqa: PLC0415 - pipeline imports models
        plan = check_plan(job, workspace, run_dir)
        node = find_node()
        checks = list(StaticChecker(workspace, plan, node).run())
        if node:
            checks += NodeRunner(node, workspace, timeout_s=timeout_s, cancel=cancel).logic_tests()
        return summarize_checks(checks, node)

    def _tool_blocks(self, msg: dict, turn: int, truncated: bool, max_tokens: int,
                     used_ids: set[str] | None = None) -> list[dict]:
        """``tool_use`` blocks for the shared executor, and ``msg``'s tool calls made safe to echo.

        Tool-call ids are made present and unique in the conversation (``used_ids``): an id that is
        missing, the client's generic ``call_<n>`` or already used becomes ``call_<turn>_<i>``, in ``msg``
        and in the block alike. Arguments are echoed as one valid JSON object - the parsed ones
        re-encoded, unparsable ones wrapped as ``{"_invalid_arguments": "<first 2000 chars>"}`` - because
        vLLM/SGLang-based endpoints parse every historical tool call and refuse the whole request
        otherwise. A call past the first :data:`MAX_TOOL_CALLS_PER_TURN` is answered with an error.
        """
        used = used_ids if used_ids is not None else set()
        blocks: list[dict] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            tid = str(tc.get("id") or "")
            if not tid or _GENERIC_ID_RE.fullmatch(tid) or tid in used:
                tid, n = f"call_{turn}_{i}", 1
                while tid in used:
                    tid, n = f"call_{turn}_{i}_{n}", n + 1
            used.add(tid)
            tc["id"] = tid
            fn = tc.get("function") if isinstance(tc.get("function"), Mapping) else {}
            raw = fn.get("arguments")
            args, err = parse_arguments(raw)
            if err:
                text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                echoed = json.dumps({"_invalid_arguments": text[:MAX_ARG_KEEP]}, ensure_ascii=False)
            else:
                echoed = json.dumps(args, ensure_ascii=False)
            tc["function"] = {**fn, "arguments": echoed}
            block = {"type": "tool_use", "id": tid, "name": str(fn.get("name") or ""), "input": args}
            if err:
                if truncated:
                    err += (f" - the answer reached max_tokens ({max_tokens}) inside this call, so its arguments were "
                            "cut off: write large files in smaller parts")
                block["error"] = err
                block["invalid"] = True
            elif i >= MAX_TOOL_CALLS_PER_TURN:
                block["error"] = TOO_MANY_CALLS
            blocks.append(block)
        return blocks

    # ------------------------------------------------------------------ model calls
    def _call_chain(self, chain: ModelChain, call: Callable[[str], Any], attempts: list, served: set, *,
                    run_id: str | None = None, project_id: str | None = None, purpose: str | None = None,
                    skip_sidelined: bool = False) -> Any:
        """:func:`call_with_model_fallback`, and a model that refuses function tools is skipped for this engine.

        A tool refusal only counts for a model that has not answered in this run yet (later 400s come
        from the conversation, not the model's abilities).
        """
        for _ in range(2 + len(chain.fallbacks)):
            try:
                return call_with_model_fallback(chain, None, call, role=ROLE, run_id=run_id, project_id=project_id,
                                                purpose=purpose, skip_sidelined=skip_sidelined)
            except NIMError as exc:
                if isinstance(exc, (NIMCancelled, NIMUnreachable)) or not is_tool_rejection(exc) or not attempts:
                    raise
                failed = attempts[-1][0]
                if failed in served or not [m for m in chain.candidates() if m != failed]:
                    raise
                chain.mark_gone(failed, f"{failed} refused function tools at this endpoint (HTTP {exc.status}): the "
                                        "agent engine needs a model with tool calling")
        raise NIMError("no model of the agent chain accepted function tools")  # pragma: no cover - bounded above

    def _turn(self, client: NIMClient, chain: ModelChain, msgs: list[dict], tools: list[dict], c: "AgentEngineCfg",
              stop: threading.Event, job: EngineJob, sink: EngineSink, state: dict,
              timeout_s: float) -> tuple[ChatResult, str]:
        """One model call with key and model fallback; returns ``(result, model that answered)``.

        ``state["turn_thinking"]`` / ``state["turn_max_tokens"]`` (set after an answer was cut off while
        thinking) override ``c.thinking`` / ``c.max_tokens`` for this turn. A model sidelined for runaway
        reasoning does not stand in for one that failed temporarily (the loop waits and retries instead)
        until this turn's last try (``state["last_resort"]`` or the last transient retry).
        """
        attempts: list[tuple[str, BaseException | None]] = []
        streamed = {"content": 0, "reasoning": 0, "tool_call": 0}
        state["last_attempts"] = attempts
        thinking = c.thinking if state.get("turn_thinking") is None else bool(state["turn_thinking"])
        max_tokens = state.get("turn_max_tokens") or c.max_tokens
        guard = {"stop": None, "off": False, "reasoning": [], "t0": time.monotonic()}

        def on_delta(delta) -> None:
            streamed[delta.kind] = streamed.get(delta.kind, 0) + len(delta.text or "")
            if delta.kind == "reasoning" and guard["off"]:
                guard["reasoning"].append(delta.text or "")
                chars = streamed["reasoning"]
                if (not streamed["content"] and not streamed["tool_call"] and guard["stop"] is not None
                        and (chars > MAX_UNREQUESTED_REASONING or (
                            chars > SLOW_UNREQUESTED_REASONING
                            and time.monotonic() - guard["t0"] > MAX_UNREQUESTED_REASONING_S))):
                    guard["stop"].stop("runaway reasoning")

        def call(model: str):
            extra = family_extra(model, thinking)
            for k in streamed:
                streamed[k] = 0
            # the switch-off was sent: a stream of reasoning alone is a model ignoring it, ended early
            guard["off"] = (extra.get("chat_template_kwargs") or {}).get("enable_thinking") is False
            guard["reasoning"] = []
            guard["t0"] = time.monotonic()
            guard["stop"] = turn_stop = _TurnStop(stop)
            try:
                res = client.chat(model, msgs, tools=tools, tool_choice="auto", stream=True, max_tokens=max_tokens,
                                  temperature=c.temperature, top_p=c.top_p, extra=extra or None, on_delta=on_delta,
                                  cancel=turn_stop, timeout_s=timeout_s)
            except NIMCancelled as exc:
                if turn_stop.reason and not stop.is_set():
                    runaway = _RunawayReasoning(model, streamed["reasoning"], "".join(guard["reasoning"]),
                                                time.monotonic() - guard["t0"])
                    attempts.append((model, runaway))
                    raise runaway from None
                attempts.append((model, exc))
                raise
            except BaseException as exc:
                attempts.append((model, exc))
                raise
            attempts.append((model, None))
            return res, model

        try:
            skip = state["transient_retries"] < self.transient_retries and not state["last_resort"]
            state["skipped_sidelined"] = skip and bool(chain.sidelined())
            out = self._call_chain(chain, call, attempts, state["served"], run_id=job.run_id,
                                   project_id=job.project_id, purpose=job.kind, skip_sidelined=skip)
            state["streamed"] = dict(streamed)  # characters per delta kind, for the journal
            return out
        finally:
            self._key_notes(client, sink, state)
            self._model_notes(attempts, chain, sink, state)

    def _key_notes(self, client: NIMClient, sink: EngineSink | None, state: dict) -> None:
        """Publish the client's key switches (labels only, never key material)."""
        pop = getattr(client, "pop_key_notes", None)
        for note in (pop() if callable(pop) else []):
            state["key_switches"].append(dict(note))
            status = note.get("status")
            line = (f"the endpoint refused the {note.get('from_key') or 'own key'}"
                    f"{f' (HTTP {status})' if status else ''}; the agent engine now uses the "
                    f"{note.get('to_key') or 'other key'}")
            if sink is not None:
                self._emit(sink, ENGINE_STDERR, {"line": line, "kind": "key", "from_key": note.get("from_key"),
                                                 "to_key": note.get("to_key"), "status": status,
                                                 "model": note.get("model")})

    def _model_notes(self, attempts: list, chain: ModelChain, sink: EngineSink, state: dict) -> None:
        """Say which model failed and which one was tried next (one line per switch)."""
        gone = chain.gone()
        for i, (model, exc) in enumerate(attempts):
            if exc is None or i + 1 >= len(attempts) or not isinstance(exc, NIMError):
                continue
            nxt = attempts[i + 1][0]
            status = exc.status
            reason = gone.get(model) or explain_http_failure(status, exc.body or str(exc), model) or str(exc)[:300]
            what = f"HTTP {status}" if status is not None else "an error"
            info = {"from_model": model, "to_model": nxt, "status": status, "reason": reason,
                    "temporary": model not in gone}
            state["model_switches"].append(info)
            self._emit(sink, ENGINE_STDERR, {"line": f"agent model {model} answered {what} ({reason}); trying {nxt}",
                                             "kind": "model", **info})

    def _account(self, res: ChatResult, model: str, c: "AgentEngineCfg", chain: ModelChain, sink: EngineSink,
                 state: dict) -> None:
        """Add the endpoint's usage numbers and publish a change of the answering model."""
        for key, val in (res.usage or {}).items():
            if isinstance(val, int) and not isinstance(val, bool):
                state["usage"][key] = state["usage"].get(key, 0) + val
        state["served"].add(model)
        if model != c.model:
            state["fallback_used"] = True
        if model != state["active_model"]:
            state["active_model"] = model
            data = {"requested": c.model, "active": model, "fallback_used": model != c.model}
            why = chain.gone().get(c.model)
            if model != c.model and why:
                data["reason"] = why
            self._emit(sink, ENGINE_MODEL_RESOLVED, data)

    def _switch_model(self, model: str, nxt: str, why: str, chain: ModelChain, sink: EngineSink, state: dict, *,
                      cooldown_s: float, sticky: bool = False) -> str:
        """``model`` waits ``cooldown_s`` behind the others and the turn continues on ``nxt``; returns the change."""
        chain.mark_busy(model, cooldown_s, why, sticky=sticky)
        state["turn_thinking"] = state["turn_max_tokens"] = None
        info = {"from_model": model, "to_model": nxt, "status": None, "reason": why, "temporary": True,
                **({"sidelined_s": cooldown_s} if sticky else {})}
        state["model_switches"].append(info)
        self._emit(sink, ENGINE_STDERR, {"line": f"agent model {why}; trying {nxt}", "kind": "model", **info})
        return f"the next model of the chain ({nxt})"

    @staticmethod
    def _context_overflow(exc: NIMError) -> bool:
        """The endpoint refused the request as too long: HTTP 413, or 400/422 with context-length wording."""
        if exc.status == 413:
            return True
        return exc.status in (400, 422) and bool(_CONTEXT_RE.search(f"{exc} {exc.body or ''}"))

    @staticmethod
    def _transient(exc: BaseException, host: str) -> str | None:
        """What went wrong when a failed model call is worth sending again unchanged after a wait, else ``None``.

        The endpoint unreachable (connection reset, read timeout), every model temporarily overloaded
        (:func:`is_temporary_failure`) or a 429 the client's own short retries did not outlast - but not
        a 429/402 that says the account has no credits left: waiting cannot fix that.
        """
        if not isinstance(exc, NIMError) or isinstance(exc, NIMCancelled):
            return None
        if isinstance(exc, NIMTimeout):
            return f"no agent model answered in time ({str(exc)[:300]})"
        if isinstance(exc, NIMUnreachable):
            return f"the agent endpoint could not be reached ({str(exc)[:300]})"
        text = " ".join(f"{exc.body or exc}".split())[:300]
        if exc.status == 429 and not _NO_CREDIT_RE.search(text):
            return f"{host} answered HTTP 429 (rate limited): {text}"
        if is_temporary_failure(exc):
            what = f"HTTP {exc.status}" if exc.status is not None else "temporarily unavailable"
            return f"the agent models at {host} are temporarily unavailable ({what}): {text}"
        return None

    def _retry_turn(self, why: str, exc: BaseException | None, stop: _RunStop, sink: EngineSink, state: dict,
                    transcript: Path) -> bool:
        """Wait, then let the loop send the same turn again: ``False`` when the retries or the time are used up.

        The wait grows (:attr:`retry_base_s` doubled per retry, at most :data:`MAX_RETRY_WAIT_S`) unless
        the endpoint said how long (``Retry-After``); it is never begun when it would end past the run
        deadline, and a cancel ends it at once (the loop then stops).
        """
        k = state["transient_retries"]
        if k >= self.transient_retries or stop.is_set():
            return False
        asked = endpoint_wait_s(exc) if exc is not None else None
        if asked is not None and asked > MAX_RETRY_WAIT_S:
            return False  # the endpoint wants a longer wait than a retry may take: classify now (fail over)
        after = asked
        delay = after if after is not None else min(MAX_RETRY_WAIT_S, float(self.retry_base_s) * 2 ** k)
        if time.monotonic() + delay + RETRY_MIN_LEFT_S >= stop.deadline:
            return False  # the retried request would only run into the deadline: classify the failure now
        state["transient_retries"] = k + 1
        state["retries"] += 1
        turn = state["turns"] + 1
        line = f"{why}; retrying turn {turn} in {delay:.1f} s (retry {k + 1} of {self.transient_retries})"
        self._emit(sink, ENGINE_STDERR, {"line": line, "kind": "retry", "turn": turn, "retry": k + 1,
                                         "max_retries": self.transient_retries, "delay_s": round(delay, 2),
                                         "status": getattr(exc, "status", None), "reason": why})
        self._journal(transcript, {"turn": turn, "ts": time.time(), "retry": k + 1, "delay_s": round(delay, 2),
                                   "reason": why})
        stop.wait(delay)
        return True

    def _rethink(self, model: str, c: "AgentEngineCfg", sink: EngineSink, state: dict, transcript: Path,
                 res: ChatResult | None, chain: ModelChain | None = None,
                 runaway: "_RunawayReasoning | None" = None) -> bool:
        """After an answer cut off while still thinking, or a ``runaway``: change the next request of this turn.

        A ``runaway`` (reasoning although thinking was off, stopped early) moves the turn to the next
        model of ``chain`` that is not sidelined, and sidelines ``model`` for :data:`RUNAWAY_COOLDOWN_S`
        (a sticky cooldown: it does not stand in for a model that is only rate limited); that bounds
        these switches, so they use none of the turn's thinking retries. A sidelined model that runs
        away again (it was the turn's last resort) ends the run.
        For a cut-off thought, first thinking is switched off (when it is on and ``model`` has a
        switch). When it was off already - the model thinks anyway - the next model of ``chain``
        continues the turn (``model`` cools down like an overloaded one), at most twice per other model
        of the chain; only then is max_tokens doubled (at most :data:`MAX_RETRY_TOKENS`). ``False`` when
        nothing is left to change - the same request would end the same way.
        """
        tokens = state.get("turn_max_tokens") or c.max_tokens
        thinking = c.thinking if state.get("turn_thinking") is None else bool(state["turn_thinking"])
        others = [m for m in chain.candidates() if m != model] if chain is not None else []
        if runaway is not None:
            sidelined = chain.sidelined() if chain is not None else set()
            fresh = [m for m in others if m not in sidelined]
            if not fresh or model in sidelined:
                return False
            change = self._switch_model(model, fresh[0], str(runaway), chain, sink, state,
                                        cooldown_s=RUNAWAY_COOLDOWN_S, sticky=True)
        else:
            if state["thinking_retries"] >= MAX_THINKING_RETRIES:
                return False
            if thinking and family_extra(model, False):
                state["turn_thinking"] = False
                change = "thinking switched off"
            elif others and state["thinking_switches"] < 2 * len(others):
                state["thinking_switches"] += 1
                change = self._switch_model(model, others[0], f"{model} used all {tokens} tokens thinking and wrote "
                                            "no answer", chain, sink, state, cooldown_s=BUSY_COOLDOWN_S)
            else:
                more = max(tokens, min(2 * int(c.max_tokens), MAX_RETRY_TOKENS))
                if more <= tokens:
                    return False
                state["turn_max_tokens"] = more
                change = f"max_tokens={more}"
            state["thinking_retries"] += 1
        turn = state["turns"] + 1
        what = (f"{model} kept reasoning ({runaway.chars} characters) although thinking was switched off and was "
                "stopped" if runaway is not None else f"{model} used all {tokens} tokens thinking")
        line = f"{what} and wrote no answer; turn {turn} is retried with {change}"
        self._emit(sink, ENGINE_STDERR, {"line": line, "kind": "thinking", "turn": turn, "model": model,
                                         "max_tokens": tokens, "change": change})
        reasoning = runaway.reasoning if runaway is not None else (res.reasoning if res is not None else "")
        self._journal(transcript, {"turn": turn, "ts": time.time(), "model": model,
                                   "usage": dict(res.usage or {}) if res is not None else {},
                                   "discarded": "stopped: reasoning although thinking was off" if runaway is not None
                                   else "cut off at max_tokens while thinking", "retry_with": change,
                                   "reasoning": _truncate(reasoning or "", JOURNAL_TEXT_LIMIT)})
        return True

    @staticmethod
    def _keys_tried(c: "AgentEngineCfg") -> list[str]:
        labels = [c.key_label or "own key"] + [label for label, key in (c.alt_keys or []) if key]
        return list(dict.fromkeys(labels))

    def _failure(self, exc: BaseException, c: "AgentEngineCfg", chain: ModelChain, host: str,
                 state: dict) -> tuple[str, str | None, str, str]:
        """``(error, failure_kind, api_error, hint)`` for a model call that failed after every fallback."""
        attempts = [a for a in state.get("last_attempts") or [] if a[1] is not None]
        model = attempts[-1][0] if attempts else chain.active
        if not isinstance(exc, NIMError):
            return f"{type(exc).__name__}: {exc}", None, "", ""
        status = exc.status
        body = exc.body or str(exc)
        text = f"{exc} {exc.body or ''}"
        api_error = (f"HTTP {status}: " if status is not None else "") + " ".join(str(body).split())[:500]
        hint = explain_http_failure(status, body, model)
        tried = ", ".join(f"{m} ({f'HTTP {e.status}' if getattr(e, 'status', None) is not None else type(e).__name__})"
                          for m, e in attempts)
        if isinstance(exc, NIMTimeout):
            return (f"no agent model at {host} answered in time (tried: {tried}): {exc}", "unavailable",
                    str(exc)[:500], "the models are slow or overloaded now; the run resumes by itself later")
        if isinstance(exc, NIMUnreachable):
            return (f"cannot reach the agent endpoint {host}: {exc}", "unavailable", str(exc)[:500],
                    "check the network connection and the agent endpoint URL")
        if status in (401, 403):
            keys = ", ".join(self._keys_tried(c))
            return (f"{host} refused the API key for {model} (HTTP {status}; keys tried: {keys}): {hint}",
                    "auth", api_error, hint)
        if status in (402, 429) or (status is None and _QUOTA_RE.search(text)):
            retried = " after retries" if (getattr(exc, "attempts", None) or 0) > 1 else ""
            return (f"HTTP {status} from {host} for {model}{retried}: {hint or str(exc)[:300]}", "quota", api_error,
                    hint)
        gone = chain.gone()
        if status in MODEL_GONE_STATUSES or (is_tool_rejection(exc) and model not in state["served"]):
            reasons = dict(gone)
            reasons.setdefault(model, hint if status in MODEL_GONE_STATUSES else
                               f"refused function tools (HTTP {status}): {' '.join(str(body).split())[:200]}")
            listed = "; ".join(f"{m}: {why}" for m, why in reasons.items())
            return f"no configured agent model can serve this run at {host}: {listed}", "model", api_error, hint
        if (status is not None and status >= 500) or (status is None and _TEMPORARY_RE.search(text)):
            return (f"the agent models are temporarily unavailable at {host} (tried: {tried or model}): "
                    f"{hint or str(exc)[:300]}", "unavailable", api_error, hint)
        return f"model call failed at {host} ({model}): {str(exc)[:1000]}", None, api_error, hint

    # ------------------------------------------------------------------ run
    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult:
        """Run one BUILD/FIX job as an OpenAI-compatible function-calling loop and return the measured result."""
        started = time.monotonic()
        try:
            c = self.cfg
        except Exception as exc:  # noqa: BLE001
            msg = f"agent engine settings unreadable: {type(exc).__name__}: {exc}"
            self._emit(sink, ENGINE_ERROR, {"error": msg})
            return EngineResult(ok=False, backend=self.name, requested_model="", error=msg)
        avail = self._availability(c)
        if not avail.available:
            self._emit(sink, ENGINE_ERROR, {"error": avail.reason})
            return EngineResult(ok=False, backend=self.name, requested_model=c.model, error=avail.reason)
        workspace = Path(job.workspace).resolve()
        if not workspace.is_dir():
            msg = f"workspace does not exist: {_posix(workspace)}"
            self._emit(sink, ENGINE_ERROR, {"error": msg})
            return EngineResult(ok=False, backend=self.name, requested_model=c.model, error=msg)
        run_dir = self.resolve_run_dir(job)
        (run_dir / "engine").mkdir(parents=True, exist_ok=True)
        from ..pipeline.quality import find_node  # noqa: PLC0415 - pipeline imports models
        bash = agent_tools.find_posix_shell() is not None
        system = self.system_prompt(job, workspace, c, bash=bash, node=find_node() is not None)
        self._write_prompt_files(run_dir, job, system)

        judge = self._register_judge(job, sink, workspace, run_dir)
        try:
            return self._loop(c, job, sink, cancel, workspace, run_dir, system, started, bash)
        finally:
            self._forget_judge(judge, job.run_id)

    def _loop(self, c: "AgentEngineCfg", job: EngineJob, sink: EngineSink, cancel: threading.Event, workspace: Path,
              run_dir: Path, system: str, started: float, bash: bool) -> EngineResult:
        host = endpoint_host(c.base_url)
        try:
            client = self._client(c)
        except Exception as exc:  # noqa: BLE001 - e.g. an empty base_url
            msg = f"cannot create the client for the agent endpoint {host or '?'}: {type(exc).__name__}: {exc}"
            self._emit(sink, ENGINE_ERROR, {"error": msg})
            return EngineResult(ok=False, backend=self.name, requested_model=c.model, error=msg)
        chain = self._chain_for(c)
        tools = self.tool_schemas(bash=bash, self_check=c.self_check_tool)
        tool_names = [t["function"]["name"] for t in tools]
        max_turns = max(1, int(c.max_turns or 1))
        transcript = run_dir / "engine" / "agent_turns.jsonl"
        state: dict = {"turns": 0, "usage": {}, "active_model": None, "fallback_used": False, "denials": [],
                       "tool_uses": 0, "finish_reason": None, "nudges": 0, "continues": 0, "key_switches": [],
                       "model_switches": [], "elided_messages": 0, "elided_chars": 0, "invalid_arguments": 0,
                       "context_retries": 0, "served": set(), "tool_names": tool_names,
                       "budget": max(1, int(c.context_chars or 1)), "missing_files": [],
                       "last_attempts": [], "keep": KEEP_TOOL_MESSAGES, "transient_retries": 0, "retries": 0,
                       "thinking_retries": 0, "thinking_switches": 0, "turn_thinking": None, "turn_max_tokens": None,
                       "text_call_nudges": 0, "last_resort": False, "skipped_sidelined": False,
                       "idle_nudges": 0, "final_seen": False, "used_ids": set()}
        msgs: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": job.prompt or ""}]
        effort = "thinking" if _thinking_sent(family_extra(chain.active, c.thinking)) else "default"
        session = {"session_id": None, "backend": self.name, "model": chain.active, "requested_model": c.model,
                   "tools": tool_names, "cwd": _posix(workspace), "permission_mode": "kyora-judge", "effort": effort,
                   "endpoint": host, "key_label": getattr(client, "key_label", c.key_label)}
        if job.resume_session_id:
            session["resume"] = "not supported by the Kyora agent engine; starting a fresh conversation"
        self._emit(sink, ENGINE_SESSION, session)

        stop = _RunStop(cancel, started + float(c.timeout_s))
        ok = cancelled = False
        error: str | None = None
        failure_kind: str | None = None
        api_error = ""
        subtype = "error_during_execution"
        result_text = ""
        while True:
            if cancel.is_set():
                cancelled = True
                break
            if state["turns"] >= max_turns:
                subtype = "error_max_turns"
                error = f"error_max_turns: reached agent_engine.max_turns={max_turns} without a final answer"
                break
            left = stop.deadline - time.monotonic()
            if left <= 0:
                subtype = "error_timeout"
                error = f"timeout: the agent loop ran longer than agent_engine.timeout_s={c.timeout_s} s"
                break
            shrunk = elide_context(msgs, state["budget"], state["keep"], deep=state["context_retries"] > 0)
            state["elided_messages"] += shrunk["messages"]
            state["elided_chars"] += shrunk["chars"]
            self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                              "pct": min(95, int(state["turns"] / max_turns * 100)),
                                              "message": f"turn {state['turns'] + 1} ({chain.active})"})
            try:
                res, model = self._turn(client, chain, msgs, tools, c, stop, job, sink, state,
                                        timeout_s=min(REQUEST_TIMEOUT_S, max(10.0, left)))
            except NIMCancelled:
                if cancel.is_set():
                    cancelled = True
                else:
                    subtype = "error_timeout"
                    error = f"timeout: the agent loop ran longer than agent_engine.timeout_s={c.timeout_s} s"
                break
            except Exception as exc:  # noqa: BLE001 - classified below, never re-raised into the pipeline
                if cancel.is_set():  # the user's cancel, not a failure to fail over from
                    cancelled = True
                    break
                if isinstance(exc, _RunawayReasoning):
                    self._emit(sink, ENGINE_THINKING, {"text": _clip(exc.reasoning)})
                    if self._rethink(exc.model, c, sink, state, transcript, None, chain, runaway=exc):
                        continue
                    others = [m for m in chain.candidates() if m != exc.model]
                    left = (f", and the other configured agent model{'s' if len(others) > 1 else ''} "
                            f"({', '.join(others)}) could not take over (unavailable, or stopped for the same "
                            "reason)" if others else ", and no other configured agent model is left to continue")
                    error, failure_kind = (f"{exc}{left}: choose another agent_engine.model or add "
                                           "fallback_models"), "model"
                    break
                why = self._transient(exc, host)
                if why and self._retry_turn(why, exc, stop, sink, state, transcript):
                    continue  # the same turn again: msgs only grow after an answer; the loop top sees a cancel
                if why and state["skipped_sidelined"] and not state["last_resort"]:
                    state["last_resort"] = True  # no retry is left: a sidelined model may try this turn once
                    continue
                if isinstance(exc, NIMError) and exc.status in (400, 422) and state.get("turn_max_tokens"):
                    # the max_tokens bump after an answer cut off while thinking was refused (above the model's
                    # output or context limit): not a long conversation, so nothing is elided and no context
                    # retry is used up; the cause is the cut-off thought
                    tokens, state["turn_max_tokens"] = state["turn_max_tokens"], None
                    who = state["last_attempts"][-1][0] if state["last_attempts"] else chain.active
                    error = (f"the answer was cut off at max_tokens={c.max_tokens} while {who} was still thinking, "
                             f"and the endpoint refused the retry with max_tokens={tokens} (HTTP {exc.status}: "
                             f"{_clip(exc.body or str(exc), 200)}): turn agent_engine.thinking off or choose a model "
                             "with a larger output limit")
                    self._journal(transcript, {"turn": state["turns"] + 1, "ts": time.time(), "error": error})
                    break
                if isinstance(exc, NIMError) and self._context_overflow(exc):
                    shrunk = {"messages": 0, "chars": 0}
                    while not shrunk["messages"] and state["context_retries"] < MAX_CONTEXT_RETRIES:
                        state["context_retries"] += 1
                        state["keep"] = max(1, KEEP_TOOL_MESSAGES >> state["context_retries"])
                        state["budget"] = max(MIN_CONTEXT_BUDGET, int(context_chars(msgs) * 0.6))
                        shrunk = elide_context(msgs, state["budget"], state["keep"], deep=True)
                    if shrunk["messages"]:
                        state["elided_messages"] += shrunk["messages"]
                        state["elided_chars"] += shrunk["chars"]
                        self._emit(sink, ENGINE_STDERR, {"line": f"the conversation is too long for the model (HTTP "
                                                         f"{exc.status}); older tool output and reasoning were "
                                                         f"elided ({shrunk['chars']} chars; the {state['keep']} "
                                                         "newest tool results are kept) and the turn is retried",
                                                         "kind": "context"})
                        continue
                if not isinstance(exc, NIMError):
                    log.exception("agent turn failed")
                error, failure_kind, api_error, _hint = self._failure(exc, c, chain, host, state)
                if failure_kind == "model":
                    self._forget_chain(chain)
                self._journal(transcript, {"turn": state["turns"] + 1, "ts": time.time(), "error": error,
                                           "failure_kind": failure_kind, "attempts": [
                                               {"model": m, "error": str(e)[:500] if e else None,
                                                "status": getattr(e, "status", None)}
                                               for m, e in state["last_attempts"]]})
                break
            self._account(res, model, c, chain, sink, state)
            state["finish_reason"] = fr = res.finish_reason
            text = strip_think(res.content or "").strip()
            if fr in TRANSIENT_FINISH_REASONS:
                why = (f"{model} ended its answer with finish_reason={fr}"
                       + (" (its tool calls were not run)" if res.tool_calls else ""))
                if self._retry_turn(why, None, stop, sink, state, transcript):
                    continue  # the answer is dropped, nothing of it was run
            if fr == "length" and not text and not res.tool_calls and (res.reasoning or "").strip():
                # cut off while thinking: templates drop old reasoning, so "continue" would resend the same request
                tokens = state.get("turn_max_tokens") or c.max_tokens
                self._emit(sink, ENGINE_THINKING, {"text": _clip(res.reasoning)})
                if self._rethink(model, c, sink, state, transcript, res, chain):
                    continue
                tried = state["thinking_retries"]
                also = (f", also after {tried} retr{'y' if tried == 1 else 'ies'} with thinking switched off, another "
                        "model or more tokens" if tried else "")
                error = (f"the answer was cut off at max_tokens={tokens} while {model} was still thinking (no text "
                         f"and no tool call){also}: raise agent_engine.max_tokens or turn agent_engine.thinking off")
                self._journal(transcript, {"turn": state["turns"] + 1, "ts": time.time(), "model": model,
                                           "usage": dict(res.usage or {}), "error": error,
                                           "reasoning": _truncate(res.reasoning or "", JOURNAL_TEXT_LIMIT)})
                break
            state["transient_retries"] = state["thinking_retries"] = 0
            state["last_resort"] = False
            state["turn_thinking"] = state["turn_max_tokens"] = None
            state["turns"] += 1
            m = res.metrics.to_dict()
            turn_metrics = {"turn": state["turns"], "model": model, "total_ms": m.get("total_ms"),
                            "ttft_ms": m.get("ttft_ms"), "completion_tokens": m.get("completion_tokens"),
                            "prompt_tokens": m.get("prompt_tokens"), "reasoning_chars": len(res.reasoning or ""),
                            "tool_calls": len(res.tool_calls or []), "finish_reason": res.finish_reason}
            secs = (m.get("total_ms") or 0) / 1000.0
            self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                              "pct": min(95, int(state["turns"] / max_turns * 100)),
                                              "message": f"turn {state['turns']}: {model} answered in {secs:.1f} s "
                                                         f"({m.get('completion_tokens') or '?'} tokens, "
                                                         f"{len(res.tool_calls or [])} tool call(s))",
                                              "turn_metrics": turn_metrics})
            msg = dict(res.message)
            truncated = fr == "length"
            stopped = fr in STOPPED_REASONS
            blocks = self._tool_blocks(msg, state["turns"], truncated, c.max_tokens, state["used_ids"])
            if stopped:
                for block in blocks:
                    block["error"] = STOPPED_CALL.format(reason=fr)  # never run what the endpoint stopped
            msgs.append(msg)  # the complete assistant message (tool_calls, echoed reasoning) - pairing stays valid
            self._journal(transcript, {"turn": state["turns"], "ts": time.time(), "model": model,
                                       "finish_reason": res.finish_reason, "usage": dict(res.usage or {}),
                                       "metrics": res.metrics.to_dict(), "streamed": state.get("streamed"),
                                       "content": res.content,
                                       "reasoning": _truncate(res.reasoning or "", JOURNAL_TEXT_LIMIT),
                                       "tool_calls": msg.get("tool_calls") or []})
            if (res.reasoning or "").strip():
                self._emit(sink, ENGINE_THINKING, {"text": _clip(res.reasoning)})
            if text:
                self._emit(sink, ENGINE_TEXT, {"text": _clip(text)})
                result_text = text
            if blocks:
                if not stopped:
                    state["continues"] = 0
                    state["tool_uses"] += len(blocks)
                    state["invalid_arguments"] += sum(1 for b in blocks if b.get("invalid"))
                results = []
                for block in blocks:
                    kyora_name, summary = self._describe(block["name"], block["input"])
                    self._emit(sink, ENGINE_TOOL_USE, {"id": block["id"], "name": kyora_name, "tool": block["name"],
                                                       "summary": summary})
                    # the user's cancel kills a running command; no call starts after the run deadline
                    result = self._run_tools(job, sink, cancel, [block], workspace, run_dir, state,
                                             deadline=stop.deadline)[0]
                    msgs.append({"role": "tool", "tool_call_id": result["tool_use_id"], "content": result["content"]})
                    results.append(result)
                self._journal(transcript, {"turn": state["turns"], "ts": time.time(), "tool_results": [
                    {"id": r["tool_use_id"], "is_error": r["is_error"], "content": _truncate(r["content"], 4000)}
                    for r in results]})
                if not stopped:
                    continue
            if truncated:  # cut off in its text: the text is in the conversation, the model can go on
                state["continues"] += 1
                if state["continues"] > MAX_CONTINUES:
                    error = (f"the answer was cut off at agent_engine.max_tokens={c.max_tokens} {MAX_CONTINUES} times "
                             "in a row without a tool call")
                    break
                msgs.append({"role": "user", "content": "continue"})
                continue
            if stopped:
                error = f"the endpoint stopped the answer: finish_reason={res.finish_reason}"
                break
            sample = text_tool_call(text, tool_names)
            if sample and state["text_call_nudges"] < MAX_TEXT_CALL_NUDGES:
                state["text_call_nudges"] += 1
                msgs.append({"role": "user", "content": TEXT_CALL_NUDGE.format(sample=sample,
                                                                               tools=", ".join(tool_names))})
                self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                                  "pct": min(95, int(state["turns"] / max_turns * 100)),
                                                  "message": "the model wrote a tool call as text instead of calling "
                                                             "the tool; asking it to use function calling "
                                                             f"({state['text_call_nudges']}/{MAX_TEXT_CALL_NUDGES})"})
                continue
            if sample:  # the nudges did not help: this model's tool calls are not parsed at this endpoint
                why = (f"{model} kept writing tool calls as text instead of calling the tools "
                       f"({MAX_TEXT_CALL_NUDGES} reminders); the endpoint does not parse its tool calls")
                chain.mark_gone(model, why)
                nxt = next((m for m in chain.candidates() if m not in chain.gone()), None)
                if nxt is not None:
                    state["text_call_nudges"] = 0
                    info = {"from_model": model, "to_model": nxt, "status": None, "reason": why, "temporary": False}
                    state["model_switches"].append(info)
                    self._emit(sink, ENGINE_STDERR, {"line": f"agent model {why}; trying {nxt}", "kind": "model",
                                                     **info})
                    continue
                self._forget_chain(chain)
                error, failure_kind = (f"{why}, and no other configured agent model is left: choose another "
                                       "agent_engine.model or add fallback_models"), "model"
                break
            first_final, state["final_seen"] = not state["final_seen"], True
            missing = self.missing_files(job, workspace)
            if missing and state["nudges"] < MAX_NUDGES:
                state["nudges"] += 1
                many = len(missing) > 1
                msgs.append({"role": "user", "content": NUDGE.format(
                    files=", ".join(missing), verb="do" if many else "does", them="them" if many else "it",
                    check=", run kyora_check" if CHECK_TOOL in tool_names else "")})
                self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                                  "pct": min(95, int(state["turns"] / max_turns * 100)),
                                                  "message": f"the model stopped with {', '.join(missing)} missing; "
                                                             f"asking it to continue ({state['nudges']}/{MAX_NUDGES})"})
                continue
            if first_final and (state["tool_uses"] == 0 or not text):
                state["idle_nudges"] += 1
                how = " and ".join(([] if text else ["with an empty answer"])
                                   + (["without calling any tool"] if state["tool_uses"] == 0 else []))
                msgs.append({"role": "user", "content": IDLE_NUDGE.format(how=how, tools=", ".join(tool_names))})
                self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                                  "pct": min(95, int(state["turns"] / max_turns * 100)),
                                                  "message": f"the model stopped {how}; asking it once to go on"})
                continue
            state["missing_files"] = missing
            ok, subtype = True, "success"
            break

        if cancelled:
            subtype, error, ok, failure_kind = "cancelled", "cancelled", False, None
        if error:
            data = {"error": error}
            if failure_kind:
                data["failure_kind"] = failure_kind
            self._emit(sink, ENGINE_ERROR, data)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit(sink, ENGINE_RESULT, {"subtype": subtype, "is_error": not ok, "num_turns": state["turns"],
                                         "duration_ms": duration_ms, "cost_usd": None, "result": result_text[:4000]})
        if ok:
            message = "engine finished"
            if state["missing_files"]:
                message += f"; still missing: {', '.join(state['missing_files'])} (the quality gate measures it)"
            self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build", "pct": 100, "message": message})
        active = state["active_model"]
        raw = {"usage": dict(state["usage"]), "turns": state["turns"], "finish_reason": state["finish_reason"],
               "tool_uses": state["tool_uses"], "models": [c.model] + [m for m in c.fallback_models if m != c.model],
               "model_chain": chain.to_dict(), "model_switches": state["model_switches"][-20:],
               "key_switches": state["key_switches"], "key_label": getattr(client, "key_label", c.key_label),
               "endpoint": host, "effort": effort, "nudges": state["nudges"], "missing_files": state["missing_files"],
               "invalid_arguments": state["invalid_arguments"], "elided_messages": state["elided_messages"],
               "elided_chars": state["elided_chars"], "tools": tool_names, "transcript": _posix(transcript),
               "subtype": subtype, "retries": state["retries"], "context_retries": state["context_retries"],
               "text_call_nudges": state["text_call_nudges"], "idle_nudges": state["idle_nudges"]}
        if failure_kind:
            raw["failure_kind"] = failure_kind
        if api_error:
            raw["api_error"] = api_error
        return EngineResult(
            ok=ok, backend=self.name, requested_model=c.model, active_model=active,
            fallback_used=bool(state["fallback_used"]), session_id=None, num_turns=state["turns"],
            duration_ms=duration_ms, cost_usd=None, result_text=result_text, error=None if ok else error,
            permission_denials=list(state["denials"]), raw=raw)

    # ------------------------------------------------------------------ probe
    def probe(self, timeout_s: float = 60) -> dict:
        """One small non-streamed request asking the model to call a function tool. Never raises.

        ``ok``: a model of the chain answered (endpoint reachable, key accepted, model served);
        ``tool_calls_ok``: its answer called the tool with JSON-object arguments - what the engine
        needs. 404/410 and overloaded models are skipped along the chain like in a run. An answer cut
        off before the call is asked once more with :data:`PROBE_RETRY_MAX_TOKENS` (``cut_off`` says
        ``thinking`` or ``text`` when that did not help either). ``fallback_used`` only when the
        configured model was tried and failed; one skipped untried (gone, or busy from an earlier
        request) is listed in ``skipped`` with the chain's reason.
        """
        out: dict = {"ok": False, "model": None, "latency_ms": None, "tool_calls_ok": False, "error": None,
                     "status": None, "key_label": None, "hint": "", "endpoint": "", "tried": [],
                     "fallback_used": False}
        client = None
        try:
            c = self.cfg
            out["endpoint"] = endpoint_host(c.base_url)
            out["key_label"] = c.key_label if c.api_key else None
            avail = self._availability(c)
            if not c.api_key or not str(c.base_url or "").strip():
                out["error"] = avail.reason
                out["hint"] = avail.reason
                return out
            client = self._client(c)
            chain = self._chain_for(c)
            deadline = time.monotonic() + float(timeout_s)
            # each model gets a share of the budget: one slow model must not use it up before the next is tried
            per_model = max(20.0, float(timeout_s) / max(1, len(chain.candidates())))
            attempts: list[tuple[str, BaseException | None]] = []
            msgs = [{"role": "user", "content": PROBE_PROMPT}]

            def call(model: str):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _ProbeBudget(f"probe time budget of {timeout_s:g} s used up before {model} was tried")
                out["tried"].append(model)
                t0 = time.monotonic()
                try:
                    # the sampling fields a run sends: an endpoint that refuses them fails here, not in BUILD
                    res = client.chat(model, msgs, stream=False, tools=[PROBE_FUNCTION], tool_choice="auto",
                                      max_tokens=PROBE_MAX_TOKENS, extra=family_extra(model, False) or None,
                                      temperature=c.temperature, top_p=c.top_p,
                                      timeout_s=max(1.0, min(left, per_model)))
                except BaseException as exc:
                    attempts.append((model, exc))
                    out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                    raise
                attempts.append((model, None))
                out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                return res, model

            try:
                res, model = self._call_chain(chain, call, attempts, set())
            except NIMError as exc:
                model = attempts[-1][0] if attempts else chain.active
                out["model"] = model
                out["error"] = str(exc)
                out["status"] = exc.status
                _err, kind, _api, hint = self._failure(exc, c, chain, out["endpoint"],
                                                        {"last_attempts": attempts, "served": set()})
                out["failure_kind"] = kind
                if kind == "model":
                    self._forget_chain(chain)
                out["hint"] = hint or (
                    "no model of the chain answered within the probe's time: they are slow or overloaded now "
                    "(a run waits longer and retries)" if isinstance(exc, NIMTimeout) else
                    "check the network connection and the agent endpoint URL" if isinstance(exc, NIMUnreachable)
                    else "")
                return out
            tokens = PROBE_MAX_TOKENS
            left = deadline - time.monotonic()
            if res.finish_reason == "length" and not res.tool_calls and left > 1:
                # cut off before the call (a model that thinks first): once more with a larger budget
                t0 = time.monotonic()
                try:
                    res = client.chat(model, msgs, stream=False, tools=[PROBE_FUNCTION], tool_choice="auto",
                                      max_tokens=PROBE_RETRY_MAX_TOKENS, extra=family_extra(model, False) or None,
                                      temperature=c.temperature, top_p=c.top_p,
                                      timeout_s=max(1.0, left))
                    tokens = PROBE_RETRY_MAX_TOKENS
                    out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                except NIMError as exc:  # the first, cut-off answer is reported
                    log.info("agent probe: retry of %s after a cut-off answer failed: %s", model, exc)
            out["ok"] = True
            out["model"] = model
            # a fallback only when the configured model was asked; one skipped before (busy, gone) is said so
            out["fallback_used"] = model != c.model and c.model in out["tried"]
            skipped = ""
            if model != c.model and c.model not in out["tried"]:
                gone = chain.gone().get(c.model)
                why = gone or chain.busy_reason(c.model) or "not tried"
                out["skipped"] = [{"model": c.model, "state": "gone" if gone else "busy", "reason": why}]
                skipped = (f"{c.model} was not tried ({'gone' if gone else 'busy from an earlier request'}: "
                           f"{why}); {model} answered")
            calls = [tc for tc in res.tool_calls if ((tc.get("function") or {}).get("name") == PROBE_TOOL)]
            out["tool_calls_ok"] = any(parse_arguments((tc.get("function") or {}).get("arguments"))[1] is None
                                       for tc in calls)
            if not out["tool_calls_ok"] and res.finish_reason == "length" and not res.tool_calls:
                thinking = (res.reasoning or "").strip() and not strip_think(res.content or "").strip()
                out["cut_off"] = "thinking" if thinking else "text"
                out["hint"] = (f"{model} answered but was cut off after {tokens} tokens"
                               + (" while thinking" if thinking else "") + ", before it called the test function: "
                               "the agent engine needs a model that calls functions within agent_engine.max_tokens - "
                               "turn agent_engine.thinking off, raise agent_engine.max_tokens or choose another "
                               "agent_engine.model")
            elif not out["tool_calls_ok"]:
                out["hint"] = (f"{model} answered but did not call the test function"
                               + (" with JSON-object arguments" if calls else "")
                               + ": the agent engine needs a model with function calling at this endpoint - choose "
                                 "another agent_engine.model or add fallback models")
            if skipped:
                out["hint"] = f"{out['hint']}; {skipped}" if out["hint"] else skipped
            return out
        except Exception as exc:  # noqa: BLE001 - a probe never raises
            log.exception("agent engine probe failed")
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        finally:
            if client is not None:
                out["key_label"] = getattr(client, "key_label", out["key_label"])
                notes: list = []
                pop = getattr(client, "pop_key_notes", None)
                try:
                    notes = list(pop()) if callable(pop) else []
                except Exception:  # noqa: BLE001
                    notes = []
                if notes:
                    out["key_switches"] = notes


__all__ = ["OpenAIAgentEngine", "family_extra", "parse_arguments", "elide_context", "context_chars", "check_plan",
           "summarize_checks", "testspec_tests", "endpoint_host", "endpoint_wait_s", "retry_after_s", "text_tool_call", "CHECK_TOOL",
           "PROBE_TOOL", "CHECK_COMMAND"]
