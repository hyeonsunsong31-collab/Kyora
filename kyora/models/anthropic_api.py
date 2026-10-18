"""Optional code engine on the Anthropic Messages API (official ``anthropic`` SDK, API key).

The default code engine is the Claude Code CLI with the user's subscription
login. This engine activates only when an ``ANTHROPIC_API_KEY`` is configured
AND the ``anthropic`` package is installed; the package is imported inside
:meth:`AnthropicAPIEngine.availability`/:meth:`AnthropicAPIEngine.run` only, so
this module always imports (iSH / Termux / Windows builds without the SDK).

The engine runs a manual agent loop with the Anthropic-defined client tools
``bash_20250124`` and ``text_editor_20250728``. Each turn is streamed with
``client.beta.messages.stream`` (``output_config.effort``, server-side refusal
fallbacks ``fallbacks="default"`` behind the ``server-side-fallback-2026-07-01``
beta, no ``thinking`` parameter - Claude Fable 5.1 rejects explicit configs).
``stop_reason`` is checked before content is read; a refusal never executes the
turn's tools. Every Write/Edit/Bash call is judged by Kyora's
:class:`~kyora.pipeline.judge.JudgeService` before it runs; bash runs in the
workspace with secrets scrubbed from its environment and the editor is
confined to the workspace (symlink escapes rejected). The tools, the judge
calls and the journal are shared with the Kyora agent engine
(:class:`~kyora.models.agent_tools.ToolLoopEngine`). Nothing is fabricated:
costs are not reported by the API and stay ``None``; token totals are the
API's own ``usage`` numbers.
"""
from __future__ import annotations

import importlib.util
import json  # noqa: F401 - kept importable from here
import logging
import os  # noqa: F401 - tests patch anthropic_api.os.name
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..pipeline.events import (
    ENGINE_COMPAT_RETRY, ENGINE_ERROR, ENGINE_MODEL_RESOLVED, ENGINE_RESULT, ENGINE_SESSION, ENGINE_TEXT,
    ENGINE_THINKING, ENGINE_TOOL_RESULT, ENGINE_TOOL_USE, JUDGE_UNAVAILABLE, STAGE_PROGRESS,  # noqa: F401
)
from ..util.fs import atomic_write_text, which  # noqa: F401 - which: the PATH lookup of find_posix_shell below
from ..util.procs import run as run_cmd  # noqa: F401 - kept importable from here
from . import agent_tools as _tools
# The shared tool machinery lives in agent_tools; every name this module exported before stays importable here.
from .agent_tools import (  # noqa: F401
    BASH_TIMEOUT_S, BASH_TOOL, DEFAULT_JUDGE_TOOLS, EDITOR_JUDGE_NAMES, EDITOR_TOOL, GIT_BASH_ENV, LISTING_LIMIT,
    MAX_TOOL_OUTPUT, MAX_VIEW_BYTES, NO_SHELL, NO_SHELL_WINDOWS, SECRET_ENV_RE, STRIP_ENV, EditorError,
    ToolLoopEngine, _get, _is_wsl_launcher, _numbered, _plain, _posix, _truncate, confine, scrubbed_env,
)
from .base import Availability, EngineEvent, EngineJob, EngineResult, EngineSink  # noqa: F401

log = logging.getLogger("kyora.anthropic_api")

BETAS: tuple[str, ...] = ("server-side-fallback-2026-07-01",)  # pairs with the scalar form fallbacks="default"
TOOLS: tuple[dict, ...] = ({"type": "bash_20250124", "name": BASH_TOOL},
                           {"type": "text_editor_20250728", "name": EDITOR_TOOL})
MAX_TOKENS = 64000
MAX_JSON_RETRIES = 2
NO_KEY_REASON = "no ANTHROPIC_API_KEY (the default engine is the Claude Code CLI with your subscription login)"
NO_PACKAGE_REASON = "anthropic package not installed (pip install anthropic)"
_MODEL_SUFFIX_RE = re.compile(r"(?:\[[^\]]*\])+$")
_MODEL_DATE_RE = re.compile(r"-\d{8}$")


def find_posix_shell(*, windows: bool | None = None, environ: Mapping[str, str] | None = None) -> str | None:
    """:func:`kyora.models.agent_tools.find_posix_shell` with this module's ``which`` (both stay patchable here)."""
    return _tools.find_posix_shell(windows=windows, environ=environ, which_fn=which)


ENGINE_NOTE = """

# Environment (Kyora Messages-API engine)
- Workspace root and working directory: {workspace}
- `bash`: every command starts in a fresh shell in the workspace root (cd and exported variables do not persist
  between calls), runs at most {timeout} s, and has no API keys or tokens in its environment.
- `str_replace_based_edit_tool`: paths are relative to the workspace root or absolute inside it; paths outside it
  are rejected.
- Kyora judges every file write/edit and every bash command before it runs; a denied call returns an error with
  the reason - adapt instead of retrying the same call.
"""


class _Never(Exception):
    """Placeholder for SDK exception classes missing from an (old or fake) ``anthropic`` module."""


# --------------------------------------------------------------------------- helpers
def _canonical_model(name: str) -> str:
    out = _MODEL_SUFFIX_RE.sub("", (name or "").strip().lower())
    return _MODEL_DATE_RE.sub("", out)


def same_model(requested: str | None, active: str | None) -> bool:
    """True when ``active`` is the requested model (``[1m]`` suffixes and ``-YYYYMMDD`` snapshots ignored)."""
    if not requested or not active:
        return False
    return _canonical_model(requested) == _canonical_model(active)


# --------------------------------------------------------------------------- engine
class AnthropicAPIEngine(ToolLoopEngine):
    """Code engine on the Anthropic Messages API; constructed as ``AnthropicAPIEngine(api_key, claude_code_cfg, judge, home)``.

    ``api_key`` may be a string or a zero-argument callable returning the
    current key (so a key added in Settings is picked up without a restart).
    """

    name = "anthropic_api"

    def __init__(self, api_key: str | Callable[[], str] | None, claude_code_cfg, judge, home: Path | str):
        super().__init__(judge, home)
        self._api_key = api_key
        self.cfg = claude_code_cfg

    # ------------------------------------------------------------------ availability
    @property
    def api_key(self) -> str:
        key = self._api_key() if callable(self._api_key) else self._api_key
        return str(key or "").strip()

    @staticmethod
    def package_installed() -> bool:
        """True when the ``anthropic`` package can be imported (checked without importing it)."""
        try:
            return importlib.util.find_spec("anthropic") is not None
        except (ImportError, ValueError):
            # ValueError: a module object without __spec__ is already in sys.modules
            return sys.modules.get("anthropic") is not None

    def availability(self) -> Availability:
        """Available only with a non-empty API key AND the ``anthropic`` package installed."""
        details = {"model": getattr(self.cfg, "model", None), "fallback_models": list(getattr(self.cfg, "fallback_models", []) or []),
                   "effort": getattr(self.cfg, "effort", None), "api_key_present": bool(self.api_key)}
        if not self.api_key:
            return Availability(False, self.name, NO_KEY_REASON, details)
        if not self.package_installed():
            details["package_installed"] = False
            return Availability(False, self.name, NO_PACKAGE_REASON, details)
        details["package_installed"] = True
        return Availability(True, self.name, "", details)

    # ------------------------------------------------------------------ plumbing
    def _judge_tools(self) -> set[str]:
        return set(getattr(self.cfg, "judge_tools", None) or DEFAULT_JUDGE_TOOLS)

    @staticmethod
    def system_prompt(job: EngineJob, workspace: Path) -> str:
        """``job.system_prompt`` plus a short note on the workspace and how the tools behave here."""
        return (job.system_prompt or "").rstrip() + ENGINE_NOTE.format(workspace=_posix(workspace), timeout=BASH_TIMEOUT_S)

    @staticmethod
    def _shell_argv(command: str) -> list[str] | None:
        shell = find_posix_shell()  # this module's (patchable) finder
        return [shell, "-c", command] if shell else None

    # ------------------------------------------------------------------ turn handling
    @staticmethod
    def _stream_turn(client, params: dict, cancel: threading.Event) -> tuple[Any, bool]:
        """One streamed request; returns ``(final_message, cancelled)``."""
        with client.beta.messages.stream(**params) as stream:
            if callable(getattr(stream, "__iter__", None)):
                for _event in stream:  # consume events so a cancel is noticed while the turn streams
                    if cancel.is_set():
                        return None, True
            if cancel.is_set():
                return None, True
            return stream.get_final_message(), False

    @staticmethod
    def echo_content(content: Any) -> Any:
        """The assistant turn to send back: ``content`` unchanged unless a mid-output fallback happened.

        After a ``fallback`` block the API requires omitting ``thinking``,
        ``redacted_thinking``, ``tool_use`` and unpaired server-tool blocks that
        appear *before* the last ``fallback`` block (shared/model-migration.md,
        "Echoing fallback turns back"); text blocks and everything after the
        boundary are kept. Without a ``fallback`` block the very same object is returned.
        """
        idx = max((i for i, b in enumerate(content) if _get(b, "type") == "fallback"), default=-1)
        if idx < 0:
            return content
        content = list(content)
        later_result_ids = {str(_get(b, "tool_use_id")) for b in content if str(_get(b, "type") or "").endswith("_tool_result")}
        kept_server_ids = {str(_get(b, "id")) for b in content[:idx]
                           if _get(b, "type") == "server_tool_use" and str(_get(b, "id")) in later_result_ids}
        out = []
        for i, b in enumerate(content):
            btype = str(_get(b, "type") or "")
            if i < idx:
                if btype in ("text", "fallback"):
                    out.append(b)
                elif btype == "server_tool_use" and str(_get(b, "id")) in kept_server_ids:
                    out.append(b)
                elif btype.endswith("_tool_result") and str(_get(b, "tool_use_id")) in kept_server_ids:
                    out.append(b)
                continue  # thinking / redacted_thinking / tool_use / unknown internal blocks are dropped
            out.append(b)
        return out

    @staticmethod
    def active_blocks(content: list) -> list:
        """Blocks produced by the model that served the turn (after the last ``fallback`` block, if any)."""
        idx = max((i for i, b in enumerate(content) if _get(b, "type") == "fallback"), default=-1)
        return list(content[idx + 1:]) if idx >= 0 else list(content)

    def _account(self, final: Any, job: EngineJob, sink: EngineSink, state: dict) -> None:
        """Add usage to the totals and publish model resolution / fallback switches."""
        usage = _get(final, "usage")
        iterations = list(_get(usage, "iterations") or [])
        sources = iterations if iterations else ([usage] if usage is not None else [])
        for src in sources:
            for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = _get(src, key)
                if isinstance(val, int) and not isinstance(val, bool):
                    state["usage"][key] = state["usage"].get(key, 0) + val
        fallback_ran = any(_get(e, "type") == "fallback_message" for e in iterations)
        for b in _get(final, "content") or []:
            if _get(b, "type") == "fallback":
                frm = _get(b, "from_") or _get(b, "from")
                to = _get(b, "to")
                state["fallbacks"].append({"from": _get(frm, "model"), "to": _get(to, "model"), "turn": state["turns"]})
        model = _get(final, "model")
        if isinstance(model, str) and model:
            if fallback_ran or not same_model(job.model, model):
                state["fallback_used"] = True
            if model != state.get("active_model"):
                state["active_model"] = model
                data = {"requested": job.model, "active": model,
                        "fallback_used": bool(fallback_ran or not same_model(job.model, model))}
                if state["fallbacks"]:
                    data["fallbacks"] = list(state["fallbacks"][-3:])
                self._emit(sink, ENGINE_MODEL_RESOLVED, data)

    def _emit_blocks(self, sink: EngineSink, blocks: list) -> None:
        for b in blocks:
            btype = _get(b, "type")
            if btype == "text":
                text = str(_get(b, "text") or "")
                if text.strip():
                    self._emit(sink, ENGINE_TEXT, {"text": text})
            elif btype == "thinking":
                text = str(_get(b, "thinking") or "")
                if text.strip():  # Fable 5.1 returns empty thinking text unless a display mode is requested
                    self._emit(sink, ENGINE_THINKING, {"text": text})
            elif btype == "tool_use":
                name = str(_get(b, "name") or "")
                kyora_name, summary = self._describe(name, _get(b, "input"))
                self._emit(sink, ENGINE_TOOL_USE, {"id": str(_get(b, "id") or ""), "name": kyora_name, "tool": name,
                                                   "summary": summary})

    # ------------------------------------------------------------------ run
    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult:
        """Run one BUILD/FIX job as a Messages-API agent loop and return the measured result."""
        started = time.monotonic()
        avail = self.availability()
        if not avail.available:
            self._emit(sink, ENGINE_ERROR, {"error": avail.reason})
            return EngineResult(ok=False, backend=self.name, requested_model=job.model, error=avail.reason)
        try:
            import anthropic  # noqa: PLC0415 - optional dependency, imported only here
        except Exception as exc:  # noqa: BLE001 - a broken install must surface as an engine error
            msg = f"cannot import anthropic: {type(exc).__name__}: {exc}"
            self._emit(sink, ENGINE_ERROR, {"error": msg})
            return EngineResult(ok=False, backend=self.name, requested_model=job.model, error=msg)
        workspace = Path(job.workspace).resolve()
        if not workspace.is_dir():
            msg = f"workspace does not exist: {_posix(workspace)}"
            self._emit(sink, ENGINE_ERROR, {"error": msg})
            return EngineResult(ok=False, backend=self.name, requested_model=job.model, error=msg)
        run_dir = self.resolve_run_dir(job)
        (run_dir / "engine").mkdir(parents=True, exist_ok=True)
        system = self.system_prompt(job, workspace)
        self._write_prompt_files(run_dir, job, system)

        judge = self._register_judge(job, sink, workspace, run_dir)
        try:
            return self._loop(anthropic, job, sink, cancel, workspace, run_dir, system, started)
        finally:
            self._forget_judge(judge, job.run_id)

    def _loop(self, anthropic, job: EngineJob, sink: EngineSink, cancel: threading.Event, workspace: Path,
              run_dir: Path, system: str, started: float) -> EngineResult:
        not_found = getattr(anthropic, "NotFoundError", _Never)
        rate_limit = getattr(anthropic, "RateLimitError", _Never)
        status_error = getattr(anthropic, "APIStatusError", _Never)
        connection_error = getattr(anthropic, "APIConnectionError", _Never)

        client = anthropic.Anthropic(api_key=self.api_key, max_retries=4)
        models: list[str] = []
        for m in [job.model] + list(job.fallback_models or []):
            if m and m not in models:
                models.append(m)
        model_idx = 0
        effort = job.effort or getattr(self.cfg, "effort", None) or "max"
        max_turns = max(1, int(job.max_turns or getattr(self.cfg, "max_turns", 60) or 60))
        transcript = run_dir / "engine" / "api_turns.jsonl"
        state: dict = {"turns": 0, "usage": {"input_tokens": 0, "output_tokens": 0}, "active_model": None,
                       "fallback_used": False, "fallbacks": [], "denials": [], "tool_uses": 0, "request_ids": [],
                       "stop_reason": None, "retries": []}
        msgs: list[dict] = [{"role": "user", "content": job.prompt or ""}]
        session = {"session_id": None, "model": models[0] if models else job.model, "tools": [BASH_TOOL, EDITOR_TOOL],
                   "permission_mode": "kyora-judge", "cwd": _posix(workspace), "backend": self.name, "effort": effort}
        if job.resume_session_id:
            session["resume"] = "not supported by the Messages-API engine; starting a fresh conversation"
        self._emit(sink, ENGINE_SESSION, session)

        ok = False
        cancelled = False
        error: str | None = None
        subtype = "error_during_execution"
        result_text = ""
        json_failures = 0
        while True:
            if cancel.is_set():
                cancelled = True
                break
            if state["turns"] >= max_turns:
                subtype = "error_max_turns"
                error = f"error_max_turns: reached max_turns={max_turns} without end_turn"
                break
            model = models[model_idx] if models else job.model
            self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build",
                                              "pct": min(95, int(state["turns"] / max_turns * 100)),
                                              "message": f"turn {state['turns'] + 1} ({model})"})
            params = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                      "output_config": {"effort": effort}, "betas": list(BETAS), "fallbacks": "default",
                      "tools": [dict(t) for t in TOOLS], "messages": msgs, "cache_control": {"type": "ephemeral"}}
            try:
                final, was_cancelled = self._stream_turn(client, params, cancel)
            except not_found as exc:
                msg = str(getattr(exc, "message", None) or exc)
                if state["turns"] == 0 and model_idx + 1 < len(models):
                    info = {"reason": f"model {model} not found (HTTP 404): {msg[:300]}",
                            "argv_change": f"model {models[model_idx + 1]}", "model": models[model_idx + 1]}
                    state["retries"].append(info)
                    self._emit(sink, ENGINE_COMPAT_RETRY, info)
                    model_idx += 1
                    continue
                error = f"model not found (HTTP 404): {msg[:1000]}"
                break
            except rate_limit as exc:
                error = f"rate limited (HTTP 429) after SDK retries: {str(getattr(exc, 'message', None) or exc)[:1000]}"
                break
            except status_error as exc:
                status = getattr(exc, "status_code", None)
                msg = str(getattr(exc, "message", None) or exc)[:1000]
                hint = " - check ANTHROPIC_API_KEY" if status in (401, 403) else ""
                error = f"HTTP {status}: {msg}{hint}"
                break
            except connection_error as exc:
                error = f"cannot reach the Anthropic API: {str(getattr(exc, 'message', None) or exc)[:1000]}"
                break
            except ValueError as exc:  # the SDK could not parse a streamed tool input; no tool_use_id to answer
                json_failures += 1
                log.warning("unparseable streamed tool input (%d/%d): %s", json_failures, MAX_JSON_RETRIES, exc)
                if json_failures > MAX_JSON_RETRIES:
                    error = f"unparseable tool input from the API after {MAX_JSON_RETRIES} retries: {exc}"
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - surface anything else as an engine error
                log.exception("Messages-API turn failed")
                error = f"{type(exc).__name__}: {exc}"
                break
            if was_cancelled or final is None:
                cancelled = True
                break
            json_failures = 0
            state["turns"] += 1
            rid = _get(final, "_request_id")
            if rid:
                state["request_ids"].append(str(rid))
            self._account(final, job, sink, state)
            stop = _get(final, "stop_reason")
            state["stop_reason"] = stop
            raw_content = _get(final, "content") or []
            content = list(raw_content)
            self._journal(transcript, {"turn": state["turns"], "ts": time.time(), "model": _get(final, "model"),
                                       "stop_reason": stop, "usage": _plain(_get(final, "usage")),
                                       "request_id": rid, "content": _plain(content)})
            # stop_reason first: a refusal can cut a tool_use off mid-input, so nothing of it is used
            if stop == "refusal":
                details = _get(final, "stop_details")
                category = _get(details, "category")
                subtype = "refusal"
                error = f"refusal:{category or 'unspecified'}"
                data = {"error": error}
                if _get(details, "explanation"):
                    data["explanation"] = str(_get(details, "explanation"))[:1000]
                if _get(details, "recommended_model"):
                    data["recommended_model"] = _get(details, "recommended_model")
                self._emit(sink, ENGINE_ERROR, data)
                break
            msgs.append({"role": "assistant", "content": self.echo_content(raw_content)})
            active = self.active_blocks(content)
            self._emit_blocks(sink, active)
            tool_uses = [b for b in active if _get(b, "type") == "tool_use"]
            state["tool_uses"] += len(tool_uses)
            texts = [str(_get(b, "text") or "") for b in active if _get(b, "type") == "text"]
            if any(t.strip() for t in texts):
                result_text = "\n".join(t for t in texts if t.strip())
            if stop == "max_tokens":
                if tool_uses:
                    error = "max_tokens reached inside a tool call (the tool input was truncated; not executed)"
                    break
                msgs.append({"role": "user", "content": "continue"})
                continue
            if stop == "pause_turn":
                continue  # the trailing assistant turn is resumed by the next request
            if stop == "tool_use" and tool_uses:
                results = self._run_tools(job, sink, cancel, tool_uses, workspace, run_dir, state)
                msgs.append({"role": "user", "content": results})
                continue
            if stop in ("end_turn", "stop_sequence", "tool_use"):
                ok = True
                subtype = "success"
                break
            error = f"stopped: {stop}"
            break

        if cancelled:
            subtype, error, ok = "cancelled", "cancelled", False
        if error and subtype != "refusal":
            self._emit(sink, ENGINE_ERROR, {"error": error})
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit(sink, ENGINE_RESULT, {"subtype": subtype, "is_error": not ok, "num_turns": state["turns"],
                                         "duration_ms": duration_ms, "cost_usd": None, "result": result_text[:4000]})
        if ok:
            self._emit(sink, STAGE_PROGRESS, {"stage": job.kind or "build", "pct": 100, "message": "engine finished"})
        active_model = state["active_model"]
        return EngineResult(
            ok=ok, backend=self.name, requested_model=job.model, active_model=active_model,
            fallback_used=bool(state["fallback_used"] or (active_model and not same_model(job.model, active_model))),
            session_id=None, num_turns=state["turns"], duration_ms=duration_ms, cost_usd=None,
            result_text=result_text, error=None if ok else error, permission_denials=list(state["denials"]),
            raw={"usage": dict(state["usage"]), "turns": state["turns"], "stop_reason": state["stop_reason"],
                 "tool_uses": state["tool_uses"], "fallbacks": state["fallbacks"], "retries": state["retries"],
                 "request_ids": state["request_ids"][-20:], "models": models, "effort": effort,
                 "transcript": _posix(transcript), "subtype": subtype})
