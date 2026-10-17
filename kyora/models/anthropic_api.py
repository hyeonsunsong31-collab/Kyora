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
confined to the workspace (symlink escapes rejected). Nothing is fabricated:
costs are not reported by the API and stay ``None``; token totals are the
API's own ``usage`` numbers.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..pipeline.events import (
    ENGINE_COMPAT_RETRY, ENGINE_ERROR, ENGINE_MODEL_RESOLVED, ENGINE_RESULT, ENGINE_SESSION, ENGINE_TEXT,
    ENGINE_THINKING, ENGINE_TOOL_RESULT, ENGINE_TOOL_USE, JUDGE_UNAVAILABLE, STAGE_PROGRESS,
)
from ..util.fs import atomic_write_text, which
from ..util.procs import run as run_cmd
from .base import Availability, CodeEngine, EngineEvent, EngineJob, EngineResult, EngineSink

log = logging.getLogger("kyora.anthropic_api")

BETAS: tuple[str, ...] = ("server-side-fallback-2026-07-01",)  # pairs with the scalar form fallbacks="default"
BASH_TOOL = "bash"
EDITOR_TOOL = "str_replace_based_edit_tool"
TOOLS: tuple[dict, ...] = ({"type": "bash_20250124", "name": BASH_TOOL},
                           {"type": "text_editor_20250728", "name": EDITOR_TOOL})
MAX_TOKENS = 64000
BASH_TIMEOUT_S = 120
MAX_TOOL_OUTPUT = 30000
MAX_VIEW_BYTES = 4 * 1024 * 1024
MAX_JSON_RETRIES = 2
LISTING_LIMIT = 1000
EDITOR_JUDGE_NAMES: dict[str, str] = {"create": "Write", "str_replace": "Edit", "insert": "Edit", "view": "Read"}
DEFAULT_JUDGE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")
SECRET_ENV_RE = re.compile(r"API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY", re.IGNORECASE)
STRIP_ENV = ("CLAUDECODE",)
NO_KEY_REASON = "no ANTHROPIC_API_KEY (the default engine is the Claude Code CLI with your subscription login)"
NO_PACKAGE_REASON = "anthropic package not installed (pip install anthropic)"
_MODEL_SUFFIX_RE = re.compile(r"(?:\[[^\]]*\])+$")
_MODEL_DATE_RE = re.compile(r"-\d{8}$")
GIT_BASH_ENV = "CLAUDE_CODE_GIT_BASH_PATH"  # the variable Claude Code itself reads on Windows
NO_SHELL = "Error: no POSIX shell (bash or sh) found on this device"
NO_SHELL_WINDOWS = ("Error: no POSIX shell found on this Windows device: install Git for Windows (Git Bash) or set "
                    f"{GIT_BASH_ENV} to its bash.exe (WSL's bash.exe in System32 is never used)")


def _is_wsl_launcher(path: str | Path, environ: Mapping[str, str]) -> bool:
    """``bash.exe``/``sh.exe`` under ``%SystemRoot%`` (System32: the WSL launcher) or a WindowsApps alias."""
    p = os.path.normcase(os.path.abspath(str(path)))
    root = os.path.normcase(os.path.abspath(environ.get("SystemRoot") or environ.get("windir") or r"C:\Windows"))
    try:
        if os.path.commonpath([p, root]) == root:
            return True
    except ValueError:  # different drives
        pass
    parts = [s.lower() for s in Path(p).parts]
    return any(a == "microsoft" and b == "windowsapps" for a, b in zip(parts, parts[1:]))


def find_posix_shell(*, windows: bool | None = None, environ: Mapping[str, str] | None = None) -> str | None:
    """The shell for the ``bash`` tool. On Windows that is Git Bash, as Claude Code uses: ``CLAUDE_CODE_GIT_BASH_PATH``,
    then the ``bash.exe`` next to ``git.exe`` (``<Git>\\cmd\\git.exe`` -> ``<Git>\\bin\\bash.exe``), then ``bash``/``sh``
    on PATH and the default install folders - never System32's ``bash.exe``, which runs the command inside WSL."""
    windows = os.name == "nt" if windows is None else windows
    environ = os.environ if environ is None else environ
    if not windows:
        shell = which("bash") or which("sh")
        if shell is None and Path("/bin/sh").exists():
            shell = "/bin/sh"
        return shell
    candidates: list[str | Path] = []
    if environ.get(GIT_BASH_ENV):
        candidates.append(environ[GIT_BASH_ENV])
    git = which("git")
    if git:
        g = Path(git)
        for root in (g.parent.parent, g.parent.parent.parent):  # cmd\git.exe, bin\git.exe, mingw64\bin\git.exe
            candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    candidates += [which("bash"), which("sh")]
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if environ.get(var):
            candidates.append(Path(environ[var]) / "Git" / "bin" / "bash.exe")
    if environ.get("LOCALAPPDATA"):
        candidates.append(Path(environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c and Path(c).is_file() and not _is_wsl_launcher(c, environ):
            return str(c)
    return None

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


class EditorError(Exception):
    """A text-editor or bash tool call that cannot be carried out (returned to Claude as ``is_error``)."""


class _Never(Exception):
    """Placeholder for SDK exception classes missing from an (old or fake) ``anthropic`` module."""


# --------------------------------------------------------------------------- helpers
def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute of an SDK object or key of a dict (content blocks may be either)."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _canonical_model(name: str) -> str:
    out = _MODEL_SUFFIX_RE.sub("", (name or "").strip().lower())
    return _MODEL_DATE_RE.sub("", out)


def same_model(requested: str | None, active: str | None) -> bool:
    """True when ``active`` is the requested model (``[1m]`` suffixes and ``-YYYYMMDD`` snapshots ignored)."""
    if not requested or not active:
        return False
    return _canonical_model(requested) == _canonical_model(active)


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n... [{len(text) - limit} characters omitted] ...\n{tail}"


def _plain(obj: Any) -> Any:
    """JSON-safe form of an SDK object for the transcript journal."""
    for attr in ("to_dict", "model_dump"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - fall through to the generic forms
                pass
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def scrubbed_env(extra: Mapping | None = None) -> dict[str, str]:
    """``os.environ`` (+ ``extra``) without API keys, tokens, secrets, passwords and credentials."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k) and k not in STRIP_ENV}
    for k, v in (extra or {}).items():
        k = str(k)
        if v is None:
            env.pop(k, None)
        elif not SECRET_ENV_RE.search(k):
            env[k] = str(v)
    return env


def confine(workspace: Path | str, raw: Any) -> Path:
    """Resolve the model-supplied ``raw`` path inside ``workspace`` or raise :class:`EditorError`.

    Relative paths are taken from the workspace root; absolute paths must lie
    inside it. Symlinks (also dangling ones) are resolved first, so a link
    pointing outside the workspace is an escape. ``~`` and URL-encoded
    traversal are refused.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise EditorError("Parameter `path` is required")
    text = raw.strip()
    low = text.lower()
    if "\x00" in text or text.startswith("~") or any(enc in low for enc in ("%2e", "%2f", "%5c")):
        raise EditorError(f"path not allowed: {text}")
    ws = Path(workspace).resolve()
    p = Path(text)
    if not p.is_absolute():
        p = ws / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        raise EditorError(f"cannot resolve path {text}: {exc}") from None
    try:
        resolved.relative_to(ws)
    except ValueError:
        raise EditorError(f"path escapes the project workspace ({_posix(ws)}): {text}") from None
    return resolved


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, start))


# --------------------------------------------------------------------------- engine
class AnthropicAPIEngine(CodeEngine):
    """Code engine on the Anthropic Messages API; constructed as ``AnthropicAPIEngine(api_key, claude_code_cfg, judge, home)``.

    ``api_key`` may be a string or a zero-argument callable returning the
    current key (so a key added in Settings is picked up without a restart).
    """

    name = "anthropic_api"

    def __init__(self, api_key: str | Callable[[], str] | None, claude_code_cfg, judge, home: Path | str):
        self._api_key = api_key
        self.cfg = claude_code_cfg
        self.judge = judge
        self.home = Path(home)

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
    def resolve_run_dir(self, job: EngineJob) -> Path:
        ctx = job.judge_context or {}
        if ctx.get("run_dir"):
            return Path(str(ctx["run_dir"]))
        if job.project_id:
            return self.home / "projects" / job.project_id / "runs" / job.run_id
        return self.home / "runs" / job.run_id

    @staticmethod
    def _emit(sink: EngineSink, type_: str, data: dict) -> None:
        try:
            sink.on_event(EngineEvent(type_, data))
        except Exception as exc:  # noqa: BLE001 - a sink bug must not break the loop
            log.error("engine sink failed for %s: %s", type_, exc)

    def _judge_tools(self) -> set[str]:
        return set(getattr(self.cfg, "judge_tools", None) or DEFAULT_JUDGE_TOOLS)

    @staticmethod
    def system_prompt(job: EngineJob, workspace: Path) -> str:
        """``job.system_prompt`` plus a short note on the workspace and how the tools behave here."""
        return (job.system_prompt or "").rstrip() + ENGINE_NOTE.format(workspace=_posix(workspace), timeout=BASH_TIMEOUT_S)

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _shell_argv(command: str) -> list[str] | None:
        shell = find_posix_shell()
        return [shell, "-c", command] if shell else None

    def _bash(self, command: str, workspace: Path | str, extra_env: Mapping | None = None,
              timeout_s: float = BASH_TIMEOUT_S) -> tuple[str, bool]:
        """Run ``command`` in ``workspace`` with a scrubbed environment; returns ``(output, is_error)``."""
        argv = self._shell_argv(command)
        if argv is None:
            return (NO_SHELL_WINDOWS if os.name == "nt" else NO_SHELL), True
        try:
            rc, out, err = run_cmd(argv, cwd=Path(workspace), env=scrubbed_env(extra_env), timeout=timeout_s)
        except OSError as exc:
            return f"Error: cannot start {argv[0]}: {exc}", True
        text = out
        if err:
            text = (text + ("\n" if text and not text.endswith("\n") else "") + err) if text else err
        text = _truncate(text.rstrip("\n"))
        if rc == -9:
            return (text + "\n" if text else "") + f"[command timed out after {int(timeout_s)} s and was killed]", True
        if rc != 0:
            return (text + "\n" if text else "") + f"[exit code {rc}]", True
        return text or "(no output)", False

    def _editor(self, workspace: Path | str, command: str, path: str, file_text: str | None = None,
                old_str: str | None = None, new_str: str | None = None, insert_line: int | None = None,
                view_range: list | None = None, insert_text: str | None = None,
                backup_dir: Path | None = None) -> str:
        """Carry out one ``text_editor_20250728`` command inside ``workspace``; raises :class:`EditorError`."""
        target = confine(workspace, path)
        ws = Path(workspace).resolve()
        rel = target.relative_to(ws)
        if command == "view":
            return self._view(target, path, view_range, ws)
        if command not in ("create", "str_replace", "insert"):
            raise EditorError(f"Unrecognized command {command!r}. Allowed: view, create, str_replace, insert")
        if rel.parts and rel.parts[0].lower() == ".git":
            raise EditorError(f"writes under .git/ are not allowed: {path}")
        if command == "create":
            if file_text is None:
                raise EditorError("Parameter `file_text` is required for command: create")
            if target.is_dir():
                raise EditorError(f"{path} is a directory")
            if target.exists() and backup_dir is not None:
                self._backup(target, rel, backup_dir)
            self._write(target, file_text)
            return f"File created successfully at: {path}"
        if not target.is_file():
            raise EditorError(f"The path {path} does not exist or is not a file.")
        text = self._read(target, path)
        if command == "str_replace":
            if old_str is None or old_str == "":
                raise EditorError("Parameter `old_str` is required for command: str_replace")
            count = text.count(old_str)
            if count == 0:
                raise EditorError(f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {path}.")
            if count > 1:
                lines = [i + 1 for i, line in enumerate(text.split("\n")) if old_str.split("\n")[0] in line]
                raise EditorError(f"No replacement was performed. Multiple occurrences of old_str `{old_str}` in lines "
                                  f"{lines}. Please ensure it is unique")
            replacement = new_str if new_str is not None else ""
            start_line = text.split(old_str, 1)[0].count("\n")
            new_text = text.replace(old_str, replacement, 1)
            if backup_dir is not None:
                self._backup(target, rel, backup_dir)
            self._write(target, new_text)
            snippet_lines = new_text.split("\n")
            lo = max(0, start_line - 4)
            hi = min(len(snippet_lines), start_line + replacement.count("\n") + 5)
            return (f"The file {path} has been edited. Here's the result of running `cat -n` on a snippet of {path}:\n"
                    f"{_numbered(snippet_lines[lo:hi], lo + 1)}\n"
                    "Review the changes and make sure they are as expected. Edit the file again if necessary.")
        # insert
        new_text_part = insert_text if insert_text is not None else new_str
        if new_text_part is None:
            raise EditorError("Parameter `insert_text` is required for command: insert")
        if isinstance(insert_line, bool) or not isinstance(insert_line, int):
            raise EditorError("Parameter `insert_line` (integer) is required for command: insert")
        lines = text.split("\n")
        if not 0 <= insert_line <= len(lines):
            raise EditorError(f"Invalid `insert_line` parameter: {insert_line}. It should be within the range of lines "
                              f"of the file: [0, {len(lines)}]")
        added = new_text_part.split("\n")
        new_lines = lines[:insert_line] + added + lines[insert_line:]
        if backup_dir is not None:
            self._backup(target, rel, backup_dir)
        self._write(target, "\n".join(new_lines))
        lo = max(0, insert_line - 4)
        hi = min(len(new_lines), insert_line + len(added) + 4)
        return (f"The file {path} has been edited. Here's the result of running `cat -n` on a snippet of the edited file:\n"
                f"{_numbered(new_lines[lo:hi], lo + 1)}\n"
                "Review the changes and make sure they are as expected (correct indentation, no duplicate lines, etc). "
                "Edit the file again if necessary.")

    @staticmethod
    def _read(target: Path, shown: str) -> str:
        try:
            if target.stat().st_size > MAX_VIEW_BYTES:
                raise EditorError(f"{shown} is larger than {MAX_VIEW_BYTES // (1024 * 1024)} MB; inspect it with bash instead")
            with open(target, "r", encoding="utf-8", newline="") as fh:
                return fh.read()
        except UnicodeDecodeError:
            raise EditorError(f"{shown} is not UTF-8 text") from None
        except OSError as exc:
            raise EditorError(f"cannot read {shown}: {exc}") from None

    @staticmethod
    def _write(target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError as exc:
            raise EditorError(f"cannot write {_posix(target)}: {exc}") from None

    @staticmethod
    def _backup(target: Path, rel: Path, backup_dir: Path) -> None:
        """Keep the previous content outside the workspace (``<run>/engine/editor-backups``)."""
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}_{_posix(rel).replace('/', '__')}"
            (backup_dir / name).write_bytes(target.read_bytes())
        except OSError as exc:
            log.warning("editor backup of %s failed: %s", target, exc)

    def _view(self, target: Path, shown: str, view_range: Any, ws: Path) -> str:
        if target.is_dir():
            if view_range:
                raise EditorError("The `view_range` parameter is not allowed when `path` points to a directory.")
            entries: list[str] = []
            base_depth = len(target.parts)
            for root, dirs, files in os.walk(target):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")
                for name in list(dirs) + sorted(f for f in files if not f.startswith(".")):
                    if len(entries) >= LISTING_LIMIT:
                        break
                    full = Path(root) / name
                    entries.append(_posix(full.relative_to(ws)) + ("/" if full.is_dir() else ""))
                if len(Path(root).parts) - base_depth >= 1:
                    dirs[:] = []  # entries of the root (level 1) and of its sub-directories (level 2) only
            listing = "\n".join(entries) if entries else "(empty)"
            return (f"Here's the files and directories up to 2 levels deep in {shown}, excluding hidden items:\n"
                    f"{listing}\n")
        if not target.is_file():
            raise EditorError(f"The path {shown} does not exist.")
        lines = self._read(target, shown).split("\n")
        start = 1
        if view_range:
            if (not isinstance(view_range, (list, tuple)) or len(view_range) != 2
                    or not all(isinstance(v, int) and not isinstance(v, bool) for v in view_range)):
                raise EditorError("Invalid `view_range`. It should be a list of two integers.")
            first, last = view_range
            if not 1 <= first <= len(lines):
                raise EditorError(f"Invalid `view_range`: {view_range}. Its first element `{first}` should be within "
                                  f"the range of lines of the file: [1, {len(lines)}]")
            if last != -1 and not first <= last <= len(lines):
                raise EditorError(f"Invalid `view_range`: {view_range}. Its second element `{last}` should be -1 or "
                                  f"between {first} and {len(lines)}")
            start = first
            lines = lines[first - 1:] if last == -1 else lines[first - 1:last]
        return _truncate(f"Here's the result of running `cat -n` on {shown}:\n{_numbered(lines, start)}\n")

    # ------------------------------------------------------------------ tool dispatch
    def _describe(self, name: str, inp: Any) -> tuple[str, str]:
        """Kyora/judge tool name and a short summary for a Messages-API tool call."""
        inp = inp if isinstance(inp, Mapping) else {}
        if name == BASH_TOOL:
            if inp.get("restart"):
                return "Bash", "(restart)"
            return "Bash", " ".join(str(inp.get("command") or "").split())[:200]
        if name == EDITOR_TOOL:
            cmd = str(inp.get("command") or "")
            return EDITOR_JUDGE_NAMES.get(cmd, f"Editor:{cmd}"), str(inp.get("path") or "")[:200]
        return name, json.dumps(inp, ensure_ascii=False, default=str)[:200]

    def _judge_call(self, job: EngineJob, sink: EngineSink, tool_name: str, tool_input: dict, tool_use_id: str,
                    workspace: Path) -> dict | None:
        """Ask the judge (only for ``claude_code.judge_tools``); ``None`` = not judged."""
        if self.judge is None or tool_name not in self._judge_tools():
            return None
        try:
            return self.judge.decide(job.run_id, {"tool_name": tool_name, "tool_input": tool_input,
                                                  "tool_use_id": tool_use_id, "cwd": _posix(workspace)})
        except Exception as exc:  # noqa: BLE001 - fail open like the hook, and say so
            log.exception("judge.decide failed for %s", tool_name)
            # ran_unjudged: counted allowed + unavailable (record_unavailable below), and so in the UI
            self._emit(sink, JUDGE_UNAVAILABLE, {"reason": f"judge error: {type(exc).__name__}: {exc}; tool call ran unjudged",
                                                 "tool_name": tool_name, "tool_use_id": tool_use_id, "ran_unjudged": True})
            try:
                self.judge.record_unavailable(job.run_id)  # the call ran unjudged; count it on the run
            except Exception as exc2:  # noqa: BLE001
                log.warning("judge.record_unavailable failed: %s", exc2)
            return None

    def _handle_tool(self, job: EngineJob, sink: EngineSink, block: Any, workspace: Path, run_dir: Path,
                     state: dict) -> tuple[str, bool]:
        """Judge and execute one ``tool_use`` block; returns ``(content, is_error)``."""
        name = str(_get(block, "name") or "")
        tid = str(_get(block, "id") or "")
        inp = _get(block, "input")
        if not isinstance(inp, Mapping):
            return "Error: invalid tool input (expected a JSON object)", True
        if name == BASH_TOOL:
            if inp.get("restart"):
                return ("bash tool has been restarted. (Each command already runs in a fresh shell in the workspace "
                        "root.)"), False
            command = inp.get("command")
            if not isinstance(command, str) or not command.strip():
                return "Error: Parameter `command` is required", True
            judge_name, judge_input = "Bash", {"command": command}
        elif name == EDITOR_TOOL:
            command = str(inp.get("command") or "")
            judge_name = EDITOR_JUDGE_NAMES.get(command)
            if judge_name is None:
                return f"Error: Unrecognized command {command!r}. Allowed: view, create, str_replace, insert", True
            try:
                target = confine(workspace, inp.get("path"))
            except EditorError as exc:
                return f"Error: {exc}", True
            fp = _posix(target)
            if command == "create":
                judge_input = {"file_path": fp, "content": inp.get("file_text")}
            elif command == "str_replace":
                judge_input = {"file_path": fp, "old_string": inp.get("old_str"), "new_string": inp.get("new_str") or ""}
            elif command == "insert":
                judge_input = {"file_path": fp, "insert_line": inp.get("insert_line"),
                               "new_string": inp.get("insert_text") if inp.get("insert_text") is not None else inp.get("new_str")}
            else:
                judge_input = {"file_path": fp}
        else:
            return f"Error: unknown tool {name!r} (available: {BASH_TOOL}, {EDITOR_TOOL})", True

        verdict = self._judge_call(job, sink, judge_name, judge_input, tid, workspace)
        if verdict is not None and str(verdict.get("decision", "")).lower() == "deny":
            reason = str(verdict.get("reason") or "no reason given")
            state["denials"].append({"tool_name": judge_name, "tool_use_id": tid, "reason": reason,
                                     "source": verdict.get("source")})
            return f"Denied by the Kyora judge: {reason}", True

        if name == BASH_TOOL:
            return self._bash(str(inp.get("command")), workspace, job.env)
        try:
            out = self._editor(workspace, str(inp.get("command")), str(inp.get("path")),
                               file_text=inp.get("file_text"), old_str=inp.get("old_str"), new_str=inp.get("new_str"),
                               insert_line=inp.get("insert_line"), view_range=inp.get("view_range"),
                               insert_text=inp.get("insert_text"), backup_dir=run_dir / "engine" / "editor-backups")
            return out, False
        except EditorError as exc:
            return f"Error: {exc}", True

    def _run_tools(self, job: EngineJob, sink: EngineSink, cancel: threading.Event, tool_uses: list, workspace: Path,
                   run_dir: Path, state: dict) -> list[dict]:
        """Execute every ``tool_use`` of a turn in order; ALL results go back in one user message."""
        results: list[dict] = []
        for block in tool_uses:
            tid = str(_get(block, "id") or "")
            kyora_name, _summary = self._describe(str(_get(block, "name") or ""), _get(block, "input"))
            if cancel.is_set():
                content, is_error = "Cancelled by the user before this tool ran", True
            else:
                try:
                    content, is_error = self._handle_tool(job, sink, block, workspace, run_dir, state)
                except Exception as exc:  # noqa: BLE001 - report to Claude and the UI, keep the loop alive
                    log.exception("tool %s failed", kyora_name)
                    content, is_error = f"Error: {type(exc).__name__}: {exc}", True
            content = content or "(no output)"
            results.append({"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": bool(is_error)})
            excerpt = content if len(content) <= 400 else content[:400] + "..."
            self._emit(sink, ENGINE_TOOL_RESULT, {"tool_use_id": tid, "name": kyora_name, "is_error": bool(is_error),
                                                  "excerpt": excerpt})
        return results

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

    @staticmethod
    def _journal(path: Path, entry: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.warning("cannot append %s: %s", path, exc)

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
        try:
            atomic_write_text(run_dir / "prompt.txt", job.prompt or "")
            atomic_write_text(run_dir / "system_prompt.md", system)
        except OSError as exc:
            log.warning("cannot write prompt files in %s: %s", run_dir, exc)

        judge = self.judge
        if judge is not None:
            ctx = dict(job.judge_context or {})
            ctx.setdefault("workspace", str(workspace))
            ctx.setdefault("project_id", job.project_id)
            ctx.setdefault("run_dir", str(run_dir))
            ctx.setdefault("kind", job.kind)
            judge.register_context(job.run_id, ctx)
        else:
            self._emit(sink, JUDGE_UNAVAILABLE, {"reason": "judge service not configured; tool calls run unjudged"})
        try:
            return self._loop(anthropic, job, sink, cancel, workspace, run_dir, system, started)
        finally:
            if judge is not None:
                try:
                    judge.forget(job.run_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("judge.forget failed: %s", exc)

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
