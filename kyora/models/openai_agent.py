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
measure its work before it stops; it writes no file itself and is not judged.
Every Write/Edit/Bash call is judged before it runs.

What can go wrong at the endpoint is handled, and said, never hidden: a key
the endpoint refuses (401/403) is replaced by the configured alternate key
(:class:`~kyora.models.nim_client.NIMClient`), a retired or unknown model
(404/410) or one that stays overloaded is replaced by the next fallback model
(:func:`~kyora.models.nim_client.call_with_model_fallback`); a model that
refuses function tools is skipped for this engine. A failure that is left is
classified in ``EngineResult.raw["failure_kind"]`` (``auth``, ``quota``,
``unavailable``, ``model``) so the pipeline can fail over to another engine.
Costs are not reported by these endpoints and stay ``None``; token totals are
the endpoint's own ``usage`` numbers.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
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
    MODEL_GONE_STATUSES, ChatResult, ModelChain, NIMCancelled, NIMClient, NIMError, NIMUnreachable,
    call_with_model_fallback, explain_http_failure, function_tool, is_tool_rejection, strip_think,
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
MAX_CONTEXT_RETRIES = 2
KEEP_TOOL_MESSAGES = 8
EVENT_TEXT_LIMIT = 4000
JOURNAL_TEXT_LIMIT = 20000
MAX_ARG_KEEP = 2000           # tool-call argument strings longer than this may be elided from old turns
MIN_CONTEXT_BUDGET = 20000   # floor of the budget after the endpoint said the conversation is too long
REQUEST_TIMEOUT_S = 600.0     # socket timeout of one request; the run's wall clock is agent_engine.timeout_s
PROBE_MAX_TOKENS = 1024
MAX_CHECK_LINES = 40
STOPPED_REASONS = ("content_filter", "error", "abort", "cancelled")  # an answer the endpoint cut off, not a final one
ELIDED_PREFIX = "[output elided to save context:"
DISABLED_REASON = "the Kyora agent engine is disabled in Settings (agent_engine.enabled)"
NO_KEY_REASON = ("no API key for the agent endpoint ({endpoint}): add an NVIDIA API key in Settings (build.nvidia.com "
                 "-> Get API Key) or set keys.agent.api_key for another OpenAI-compatible endpoint")
NO_ENDPOINT_REASON = "no endpoint configured for the agent engine (keys.agent.base_url or nvidia.base_url)"
_TEMPORARY_RE = re.compile(r"overload|temporarily unavailable|service unavailable", re.IGNORECASE)
_QUOTA_RE = re.compile(r"quota|insufficient.?(?:balance|credit|funds)|out of credits|payment required", re.IGNORECASE)
_CONTEXT_RE = re.compile(r"context (?:length|window)|maximum context|too many tokens|prompt is too long|"
                         r"input is too long|reduce the length|maximum input", re.IGNORECASE)
_SPEC_TEST_RE = re.compile(r"^###\s+`([^`]+)`\s+-\s+(.*)$")
_SPEC_KIND_RE = re.compile(r"^-\s*kind:\s*([A-Za-z]+)(\s+-\s+REQUIRED)?", re.IGNORECASE)

BASH_FUNCTION = function_tool(BASH_TOOL, (
    "Run a shell command in the project workspace root. Every call starts a fresh shell (cd and exported variables "
    f"do not persist), runs at most {BASH_TIMEOUT_S} s and has no API keys in its environment. Kyora judges every "
    "command before it runs."), {
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
    "'all N checks pass'. Writes no file itself."), {"type": "object", "properties": {}})
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
             "  persist between calls), runs at most {timeout} s, and has no API keys or tokens in its environment.")
NO_BASH_LINE = ("- There is no POSIX shell on this device, so there is no `bash` tool: create and edit files with\n"
                "  `str_replace_based_edit_tool`.")
CHECK_LINE = ("- `kyora_check`: runs Kyora's static checks{node} on the workspace right now and lists every\n"
              "  failing check (or says that all pass). Run it when the game is written and fix what fails before you\n"
              "  finish; the in-browser acceptance tests run later in Kyora's harness.\n")
NUDGE = ("You stopped, but the work is not finished: {files} {verb} not exist in the workspace yet. Create {them} with "
         "str_replace_based_edit_tool (command \"create\") through the function-calling interface (not as text){check}, "
         "then end with a short summary of what you changed.")


# --------------------------------------------------------------------------- helpers
def endpoint_host(base_url: str | None) -> str:
    """The host (and port) of an endpoint URL - never its path or query, which may carry tokens."""
    text = str(base_url or "").strip()
    if not text:
        return ""
    return urllib.parse.urlsplit(text).netloc or text.split("/")[0]


def family_extra(model: str, thinking: bool) -> dict:
    """Request fields a model family reads: Nemotron's thinking switch; nothing for other families."""
    if model_family(model) == "nemotron":
        return {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}
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


def _msg_chars(m: Mapping) -> int:
    """Characters a message adds to the context: content, echoed reasoning and tool-call arguments."""
    n = len(m.get("content") or "") if isinstance(m.get("content"), str) else len(json.dumps(m.get("content") or ""))
    n += len(m.get("reasoning_content") or "")
    for tc in m.get("tool_calls") or []:
        n += len(str(((tc or {}).get("function") or {}).get("arguments") or ""))
    return n


def context_chars(msgs: list[dict]) -> int:
    return sum(_msg_chars(m) for m in msgs)


def elide_context(msgs: list[dict], limit: int, keep: int = KEEP_TOOL_MESSAGES) -> dict:
    """Shrink ``msgs`` in place below ``limit`` characters without removing a message.

    Only what lies before the ``keep`` newest tool results (and the assistant turns that asked for them) is
    touched, oldest first: tool results are replaced by ``[output elided to save context: N chars]``, then
    echoed reasoning is dropped, then long string arguments of old tool calls (file contents already
    written) are replaced by a marker. Every
    tool call keeps its id and every tool result its ``tool_call_id``, so the pairing stays valid.
    Returns ``{"messages", "chars"}`` - how many messages changed and how many characters were saved.
    """
    total = context_chars(msgs)
    stats = {"messages": 0, "chars": 0}
    if total <= limit:
        return stats
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    if len(tool_idx) <= keep:
        return stats
    boundary = tool_idx[-keep]  # from the assistant message that owns the oldest kept result on, all stays intact
    while boundary > 0 and msgs[boundary - 1].get("role") == "tool":
        boundary -= 1
    if boundary > 0 and msgs[boundary - 1].get("role") == "assistant":
        boundary -= 1

    def saved(before: int, after: int) -> None:
        nonlocal total
        total -= before - after
        stats["messages"] += 1
        stats["chars"] += before - after

    for i in tool_idx[:-keep]:
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


class _ProbeBudget(NIMError):
    """The probe's time budget ran out before the next model could be tried."""


# --------------------------------------------------------------------------- engine
class OpenAIAgentEngine(ToolLoopEngine):
    """Kyora's own code engine on an OpenAI-compatible endpoint (default: NVIDIA API Catalog with the user's keys).

    Constructed as ``OpenAIAgentEngine(cfg, judge, home, client_factory=None)``; ``cfg`` is a
    zero-argument callable returning the current :class:`~kyora.config.AgentEngineCfg` (so Settings
    changes apply to the next run) or the config itself. ``client_factory(cfg)`` replaces the
    :class:`NIMClient` (tests). The model chain - which models were found gone - is kept across runs
    while the endpoint, model and fallbacks stay the same.
    """

    name = "openai_agent"

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
            return self.kyora_check(job, workspace, run_dir), False
        if name not in (BASH_TOOL, EDITOR_TOOL):
            return f"Error: unknown tool {name!r} (available: {', '.join(offered)})", True
        return super()._handle_tool(job, sink, block, workspace, run_dir, state)

    def kyora_check(self, job: EngineJob, workspace: Path, run_dir: Path) -> str:
        """Kyora's static checks (+ Node logic tests when Node exists), as the TEST stage runs them; writes no file."""
        from ..pipeline.quality import NodeRunner, StaticChecker, find_node  # noqa: PLC0415 - pipeline imports models
        plan = check_plan(job, workspace, run_dir)
        node = find_node()
        checks = list(StaticChecker(workspace, plan, node).run())
        if node:
            checks += NodeRunner(node, workspace).logic_tests()
        return summarize_checks(checks, node)

    def _tool_blocks(self, msg: dict, turn: int, truncated: bool, max_tokens: int) -> list[dict]:
        """``tool_use`` blocks for the shared executor; tool-call ids are made present and unique in ``msg`` too."""
        blocks: list[dict] = []
        seen: set[str] = set()
        for i, tc in enumerate(msg.get("tool_calls") or []):
            tid = str(tc.get("id") or "") or f"call_{turn}_{i}"
            if tid in seen:
                tid = f"{tid}_{i}"
            seen.add(tid)
            tc["id"] = tid
            fn = tc.get("function") or {}
            args, err = parse_arguments(fn.get("arguments"))
            block = {"type": "tool_use", "id": tid, "name": str(fn.get("name") or ""), "input": args}
            if err:
                if truncated:
                    err += (f" - the answer reached max_tokens ({max_tokens}) inside this call, so its arguments were "
                            "cut off: write large files in smaller parts")
                block["error"] = err
            blocks.append(block)
        return blocks

    # ------------------------------------------------------------------ model calls
    def _call_chain(self, chain: ModelChain, call: Callable[[str], Any], attempts: list, served: set, *,
                    run_id: str | None = None, project_id: str | None = None, purpose: str | None = None) -> Any:
        """:func:`call_with_model_fallback`, and a model that refuses function tools is skipped for this engine.

        A tool refusal only counts for a model that has not answered in this run yet (later 400s come
        from the conversation, not the model's abilities).
        """
        for _ in range(2 + len(chain.fallbacks)):
            try:
                return call_with_model_fallback(chain, None, call, role=ROLE, run_id=run_id, project_id=project_id,
                                                purpose=purpose)
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
        """One model call with key and model fallback; returns ``(result, model that answered)``."""
        attempts: list[tuple[str, BaseException | None]] = []
        streamed = {"content": 0, "reasoning": 0, "tool_call": 0}
        state["last_attempts"] = attempts

        def on_delta(delta) -> None:
            streamed[delta.kind] = streamed.get(delta.kind, 0) + len(delta.text or "")

        def call(model: str):
            extra = family_extra(model, c.thinking)
            try:
                res = client.chat(model, msgs, tools=tools, tool_choice="auto", stream=True, max_tokens=c.max_tokens,
                                  temperature=c.temperature, top_p=c.top_p, extra=extra or None, on_delta=on_delta,
                                  cancel=stop, timeout_s=timeout_s)
            except BaseException as exc:
                attempts.append((model, exc))
                raise
            attempts.append((model, None))
            return res, model

        try:
            out = self._call_chain(chain, call, attempts, state["served"], run_id=job.run_id,
                                   project_id=job.project_id, purpose=job.kind)
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

    @staticmethod
    def _context_overflow(exc: NIMError) -> bool:
        return exc.status in (400, 413, 422) and bool(_CONTEXT_RE.search(f"{exc} {exc.body or ''}"))

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
                       "last_attempts": []}
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
            shrunk = elide_context(msgs, state["budget"])
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
                if isinstance(exc, NIMError) and self._context_overflow(exc) \
                        and state["context_retries"] < MAX_CONTEXT_RETRIES:
                    state["context_retries"] += 1
                    state["budget"] = max(MIN_CONTEXT_BUDGET, int(context_chars(msgs) * 0.6))
                    shrunk = elide_context(msgs, state["budget"])
                    if shrunk["messages"]:
                        state["elided_messages"] += shrunk["messages"]
                        state["elided_chars"] += shrunk["chars"]
                        self._emit(sink, ENGINE_STDERR, {"line": f"the conversation is too long for the model (HTTP "
                                                         f"{exc.status}); older tool output was elided "
                                                         f"({shrunk['chars']} chars) and the turn is retried",
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
            state["turns"] += 1
            self._account(res, model, c, chain, sink, state)
            state["finish_reason"] = res.finish_reason
            msg = dict(res.message)
            truncated = res.finish_reason == "length"
            blocks = self._tool_blocks(msg, state["turns"], truncated, c.max_tokens)
            msgs.append(msg)  # the complete assistant message (tool_calls, echoed reasoning) - pairing stays valid
            self._journal(transcript, {"turn": state["turns"], "ts": time.time(), "model": model,
                                       "finish_reason": res.finish_reason, "usage": dict(res.usage or {}),
                                       "metrics": res.metrics.to_dict(), "streamed": state.get("streamed"),
                                       "content": res.content,
                                       "reasoning": _truncate(res.reasoning or "", JOURNAL_TEXT_LIMIT),
                                       "tool_calls": msg.get("tool_calls") or []})
            if (res.reasoning or "").strip():
                self._emit(sink, ENGINE_THINKING, {"text": _clip(res.reasoning)})
            text = strip_think(res.content or "").strip()
            if text:
                self._emit(sink, ENGINE_TEXT, {"text": _clip(text)})
                result_text = text
            if blocks:
                state["continues"] = 0
                state["tool_uses"] += len(blocks)
                state["invalid_arguments"] += sum(1 for b in blocks if b.get("error"))
                results = []
                for block in blocks:
                    kyora_name, summary = self._describe(block["name"], block["input"])
                    self._emit(sink, ENGINE_TOOL_USE, {"id": block["id"], "name": kyora_name, "tool": block["name"],
                                                       "summary": summary})
                    result = self._run_tools(job, sink, cancel, [block], workspace, run_dir, state)[0]
                    msgs.append({"role": "tool", "tool_call_id": result["tool_use_id"], "content": result["content"]})
                    results.append(result)
                self._journal(transcript, {"turn": state["turns"], "ts": time.time(), "tool_results": [
                    {"id": r["tool_use_id"], "is_error": r["is_error"], "content": _truncate(r["content"], 4000)}
                    for r in results]})
                continue
            if truncated:
                state["continues"] += 1
                if state["continues"] > MAX_CONTINUES:
                    error = (f"the answer was cut off at agent_engine.max_tokens={c.max_tokens} {MAX_CONTINUES} times "
                             "in a row without a tool call")
                    break
                msgs.append({"role": "user", "content": "continue"})
                continue
            if res.finish_reason in STOPPED_REASONS:
                error = f"the endpoint stopped the answer: finish_reason={res.finish_reason}"
                break
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
               "subtype": subtype}
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
        needs. 404/410 and overloaded models are skipped along the chain like in a run.
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
            attempts: list[tuple[str, BaseException | None]] = []
            msgs = [{"role": "user", "content": PROBE_PROMPT}]

            def call(model: str):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _ProbeBudget(f"probe time budget of {timeout_s:g} s used up before {model} was tried")
                out["tried"].append(model)
                t0 = time.monotonic()
                try:
                    res = client.chat(model, msgs, stream=False, tools=[PROBE_FUNCTION], tool_choice="auto",
                                      max_tokens=PROBE_MAX_TOKENS, extra=family_extra(model, False) or None,
                                      timeout_s=max(1.0, left))
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
                out["hint"] = hint or ("check the network connection and the agent endpoint URL"
                                       if isinstance(exc, NIMUnreachable) else "")
                return out
            out["ok"] = True
            out["model"] = model
            out["fallback_used"] = model != c.model
            calls = [tc for tc in res.tool_calls if ((tc.get("function") or {}).get("name") == PROBE_TOOL)]
            out["tool_calls_ok"] = any(parse_arguments((tc.get("function") or {}).get("arguments"))[1] is None
                                       for tc in calls)
            if not out["tool_calls_ok"]:
                out["hint"] = (f"{model} answered but did not call the test function"
                               + (" with JSON-object arguments" if calls else "")
                               + ": the agent engine needs a model with function calling at this endpoint - choose "
                                 "another agent_engine.model or add fallback models")
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
           "summarize_checks", "testspec_tests", "endpoint_host", "CHECK_TOOL", "PROBE_TOOL"]
