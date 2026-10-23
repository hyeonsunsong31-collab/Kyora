"""Shared machinery of Kyora's own tool-loop code engines.

Two engines run their own agent loop instead of a CLI: the optional Anthropic
Messages-API engine (:mod:`kyora.models.anthropic_api`) and the Kyora agent
engine on any OpenAI-compatible endpoint (:mod:`kyora.models.openai_agent`).
Both give the model the same two client tools and both are subclasses of
:class:`ToolLoopEngine`:

* ``bash``: every command runs in a fresh POSIX shell in the workspace root
  (Git Bash on Windows, never System32's WSL launcher), with an allowlisted,
  secret-free environment (:func:`kyora.util.envsafe.safe_env`), a time limit
  and bounded output; what it leaves running in the background is stopped when
  it returns (POSIX; on Windows only a timeout or cancel ends it, and the tool
  description says so);
* ``str_replace_based_edit_tool``: view / create / str_replace / insert,
  confined to the workspace (symlink escapes, ``~`` and URL-encoded traversal
  rejected, writes under ``.git/`` refused); the previous content of every
  edited file is backed up outside the workspace (``<run>/engine/editor-backups``).

Every Write/Edit/Bash call is judged by Kyora's
:class:`~kyora.pipeline.judge.JudgeService` before it runs (reads are not); a
judge error fails open, is reported as ``judge.unavailable`` and counted on the
run. Tool results and journal lines pass :func:`kyora.util.envsafe.redact`, so a
configured key never reaches the model. Nothing here talks to a model.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..pipeline.events import ENGINE_TOOL_RESULT, JUDGE_UNAVAILABLE
from ..util.envsafe import SECRET_ENV_RE, redact, safe_env
from ..util.fs import atomic_write_text, which
from ..util.procs import CANCELLED_NOTE, run as run_cmd
from .base import CodeEngine, EngineEvent, EngineJob, EngineSink

log = logging.getLogger("kyora.agent_tools")

BASH_TOOL = "bash"
EDITOR_TOOL = "str_replace_based_edit_tool"
BASH_TIMEOUT_S = 120
MAX_TOOL_OUTPUT = 30000
MAX_BASH_CAPTURE = 2 * MAX_TOOL_OUTPUT  # per stream, before the result is cut to MAX_TOOL_OUTPUT
MAX_VIEW_BYTES = 4 * 1024 * 1024
LISTING_LIMIT = 1000
EDITOR_JUDGE_NAMES: dict[str, str] = {"create": "Write", "str_replace": "Edit", "insert": "Edit", "view": "Read"}
DEFAULT_JUDGE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")
STRIP_ENV = ("CLAUDECODE",)  # never passed: safe_env is an allowlist
GIT_BASH_ENV = "CLAUDE_CODE_GIT_BASH_PATH"  # the variable Claude Code itself reads on Windows
NO_SHELL = "Error: no POSIX shell (bash or sh) found on this device"
NO_SHELL_WINDOWS = ("Error: no POSIX shell found on this Windows device: install Git for Windows (Git Bash) or set "
                    f"{GIT_BASH_ENV} to its bash.exe (WSL's bash.exe in System32 is never used)")
DEADLINE_PASSED = "Error: the run deadline passed; the call was not run"
# for the bash tool description. POSIX: the shell's session is killed when the call returns, so background jobs
# do not outlive it. Windows: only a timeout or cancel ends the tree (taskkill /T), so the model must not leave any.
BASH_BACKGROUND_POSIX = "background processes are stopped when the command returns"
BASH_BACKGROUND_WINDOWS = "do not leave background processes running: they are not stopped on this device"
BASH_BACKGROUND_NOTE = BASH_BACKGROUND_WINDOWS if os.name == "nt" else BASH_BACKGROUND_POSIX


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


def find_posix_shell(*, windows: bool | None = None, environ: Mapping[str, str] | None = None,
                     which_fn: Callable[[str], str | None] | None = None) -> str | None:
    """The shell for the ``bash`` tool. On Windows that is Git Bash, as Claude Code uses: ``CLAUDE_CODE_GIT_BASH_PATH``,
    then the ``bash.exe`` next to ``git.exe`` (``<Git>\\cmd\\git.exe`` -> ``<Git>\\bin\\bash.exe``), then ``bash``/``sh``
    on PATH and the default install folders - never System32's ``bash.exe``, which runs the command inside WSL.
    ``which_fn`` replaces the PATH lookup (default :func:`kyora.util.fs.which`)."""
    windows = os.name == "nt" if windows is None else windows
    environ = os.environ if environ is None else environ
    find = which_fn or which
    if not windows:
        shell = find("bash") or find("sh")
        if shell is None and Path("/bin/sh").exists():
            shell = "/bin/sh"
        return shell
    candidates: list[str | Path | None] = []
    if environ.get(GIT_BASH_ENV):
        candidates.append(environ[GIT_BASH_ENV])
    git = find("git")
    if git:
        g = Path(git)
        for root in (g.parent.parent, g.parent.parent.parent):  # cmd\git.exe, bin\git.exe, mingw64\bin\git.exe
            candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    candidates += [find("bash"), find("sh")]
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if environ.get(var):
            candidates.append(Path(environ[var]) / "Git" / "bin" / "bash.exe")
    if environ.get("LOCALAPPDATA"):
        candidates.append(Path(environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c and Path(c).is_file() and not _is_wsl_launcher(c, environ):
            return str(c)
    return None


class EditorError(Exception):
    """A text-editor or bash tool call that cannot be carried out (returned to the model as an error)."""


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
    """The allowlisted, secret-free part of ``os.environ`` plus ``extra`` (:func:`kyora.util.envsafe.safe_env`)."""
    return safe_env(extra)


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


# --------------------------------------------------------------------------- engine base
class ToolLoopEngine(CodeEngine):
    """A code engine that runs its own agent loop with Kyora's judged ``bash`` and text-editor tools.

    Subclasses implement :meth:`availability` and :meth:`run` (the model protocol);
    this class carries the run directory, the judge calls, the tool execution
    (:meth:`_handle_tool` for one ``{"type": "tool_use", "id", "name", "input"}``
    block, :meth:`_run_tools` for a turn's blocks in order) and the journal.
    """

    name = "tool_loop"

    def __init__(self, judge, home: Path | str):
        self.judge = judge
        self.home = Path(home)

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
        """Kyora tool names the judge decides (the rest - Read, KyoraCheck - run unjudged)."""
        return set(DEFAULT_JUDGE_TOOLS)

    def _available_tools(self) -> tuple[str, ...]:
        """Tool names named in the "unknown tool" error."""
        return (BASH_TOOL, EDITOR_TOOL)

    @staticmethod
    def _write_prompt_files(run_dir: Path, job: EngineJob, system: str) -> None:
        """``<run>/prompt.txt`` and ``<run>/system_prompt.md``: what the engine was asked, for the audit trail."""
        try:
            atomic_write_text(run_dir / "prompt.txt", job.prompt or "")
            atomic_write_text(run_dir / "system_prompt.md", system)
        except OSError as exc:
            log.warning("cannot write prompt files in %s: %s", run_dir, exc)

    def _register_judge(self, job: EngineJob, sink: EngineSink, workspace: Path, run_dir: Path):
        """Register the run's judge context (workspace, project, run dir, kind); returns the judge or ``None``."""
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
        return judge

    @staticmethod
    def _forget_judge(judge, run_id: str) -> None:
        if judge is None:
            return
        try:
            judge.forget(run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("judge.forget failed: %s", exc)

    @staticmethod
    def _journal(path: Path, entry: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(redact(json.dumps(entry, ensure_ascii=False, default=str)) + "\n")
        except OSError as exc:
            log.warning("cannot append %s: %s", path, exc)

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _shell_argv(command: str) -> list[str] | None:
        shell = find_posix_shell()
        return [shell, "-c", command] if shell else None

    def _bash(self, command: str, workspace: Path | str, extra_env: Mapping | None = None,
              timeout_s: float = BASH_TIMEOUT_S, *, cancel: threading.Event | None = None,
              deadline: float | None = None) -> tuple[str, bool]:
        """Run ``command`` in ``workspace`` with a scrubbed environment; returns ``(output, is_error)``.

        ``deadline`` (``time.monotonic()``) caps the time limit; ``cancel`` kills the command when set.
        """
        argv = self._shell_argv(command)
        if argv is None:
            return (NO_SHELL_WINDOWS if os.name == "nt" else NO_SHELL), True
        capped = deadline is not None and deadline - time.monotonic() < timeout_s
        if capped:
            timeout_s = deadline - time.monotonic()
            if timeout_s <= 0:
                return DEADLINE_PASSED, True
        try:
            rc, out, err = run_cmd(argv, cwd=Path(workspace), env=scrubbed_env(extra_env), timeout=timeout_s,
                                   max_output_chars=MAX_BASH_CAPTURE, cancel=cancel)
        except OSError as exc:
            return f"Error: cannot start {argv[0]}: {exc}", True
        text = out
        if err:
            text = (text + ("\n" if text and not text.endswith("\n") else "") + err) if text else err
        text = _truncate(text.rstrip("\n"))
        if rc == -9 and capped and time.monotonic() >= deadline:  # also when a cancel event tracks the deadline
            return (text + "\n" if text else "") + (f"[command killed after {timeout_s:.1f} s: the run deadline "
                                                     "was reached]"), True
        if rc == -9 and cancel is not None and cancel.is_set():
            return text or CANCELLED_NOTE, True  # procs.run already ended stderr with "[cancelled]"
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
        """Kyora/judge tool name and a short summary for a tool call."""
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
        """Ask the judge (only for :meth:`_judge_tools`); ``None`` = not judged."""
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
            return f"Error: unknown tool {name!r} (available: {', '.join(self._available_tools())})", True

        verdict = self._judge_call(job, sink, judge_name, judge_input, tid, workspace)
        if verdict is not None and str(verdict.get("decision", "")).lower() == "deny":
            reason = str(verdict.get("reason") or "no reason given")
            state["denials"].append({"tool_name": judge_name, "tool_use_id": tid, "reason": reason,
                                     "source": verdict.get("source")})
            return f"Denied by the Kyora judge: {reason}", True

        if name == BASH_TOOL:
            return self._bash(str(inp.get("command")), workspace, job.env, cancel=state.get("tool_cancel"),
                              deadline=state.get("tool_deadline"))
        try:
            out = self._editor(workspace, str(inp.get("command")), str(inp.get("path")),
                               file_text=inp.get("file_text"), old_str=inp.get("old_str"), new_str=inp.get("new_str"),
                               insert_line=inp.get("insert_line"), view_range=inp.get("view_range"),
                               insert_text=inp.get("insert_text"), backup_dir=run_dir / "engine" / "editor-backups")
            return out, False
        except EditorError as exc:
            return f"Error: {exc}", True

    @staticmethod
    def _tool_timeout(state: dict, default_s: float) -> float:
        """``default_s`` capped by the run deadline :meth:`_run_tools` was given (``0.0`` once it passed)."""
        deadline = state.get("tool_deadline")
        if deadline is None:
            return float(default_s)
        return max(0.0, min(float(default_s), deadline - time.monotonic()))

    def _run_tools(self, job: EngineJob, sink: EngineSink, cancel: threading.Event, tool_uses: list, workspace: Path,
                   run_dir: Path, state: dict, *, deadline: float | None = None) -> list[dict]:
        """Execute every ``tool_use`` block in order; returns one ``tool_result`` dict per block, in the same order.

        ``deadline`` (``time.monotonic()``): a call reached after it is not run (it gets an error result, so
        every call keeps its result); it also caps the bash time limit (:meth:`_tool_timeout`). ``cancel`` and
        ``deadline`` are kept in ``state`` (``tool_cancel``, ``tool_deadline``) for :meth:`_handle_tool`.
        Every result is redacted (:func:`kyora.util.envsafe.redact`) before it is stored, shown or returned.
        """
        state["tool_cancel"] = cancel
        state["tool_deadline"] = deadline
        results: list[dict] = []
        for block in tool_uses:
            tid = str(_get(block, "id") or "")
            kyora_name, _summary = self._describe(str(_get(block, "name") or ""), _get(block, "input"))
            if cancel.is_set():
                content, is_error = "Cancelled by the user before this tool ran", True
            elif deadline is not None and time.monotonic() >= deadline:
                content, is_error = DEADLINE_PASSED, True
            else:
                try:
                    content, is_error = self._handle_tool(job, sink, block, workspace, run_dir, state)
                except Exception as exc:  # noqa: BLE001 - report to the model and the UI, keep the loop alive
                    log.exception("tool %s failed", kyora_name)
                    content, is_error = f"Error: {type(exc).__name__}: {exc}", True
            content = redact(content or "(no output)")
            results.append({"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": bool(is_error)})
            excerpt = content if len(content) <= 400 else content[:400] + "..."
            self._emit(sink, ENGINE_TOOL_RESULT, {"tool_use_id": tid, "name": kyora_name, "is_error": bool(is_error),
                                                  "excerpt": excerpt})
        return results


__all__ = [
    "ToolLoopEngine", "EditorError", "find_posix_shell", "scrubbed_env", "confine",
    "BASH_TOOL", "EDITOR_TOOL", "BASH_TIMEOUT_S", "MAX_TOOL_OUTPUT", "MAX_BASH_CAPTURE", "MAX_VIEW_BYTES",
    "LISTING_LIMIT", "EDITOR_JUDGE_NAMES", "DEFAULT_JUDGE_TOOLS", "SECRET_ENV_RE", "STRIP_ENV", "GIT_BASH_ENV",
    "NO_SHELL", "NO_SHELL_WINDOWS", "DEADLINE_PASSED", "BASH_BACKGROUND_NOTE", "BASH_BACKGROUND_POSIX",
    "BASH_BACKGROUND_WINDOWS",
]
