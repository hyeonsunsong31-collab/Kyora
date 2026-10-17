"""Instant judgement of every code-engine tool call: rules -> cache -> Kimi-K3.

The Claude Code PreToolUse hook (``kyora/runners/kyora_hook.py``) POSTs each
matched tool call to ``POST /api/judge`` (loopback only); the route handler
calls :meth:`JudgeService.handle_request`. The optional Messages-API engine
calls :meth:`JudgeService.decide` directly.

Decision order (documented in ``docs/architecture.md``):

1. deterministic local rules (cheap, no model): allow read-only tools and
   workspace-internal writes to plan/standard paths, deny path escapes, writes
   to ``.git/``/``.claude/``, destructive or privilege-escalating shell
   commands, network installs when the plan forbids external dependencies;
2. a per-run verdict cache keyed by ``(tool_name, normalized-input hash)`` for
   verdicts that came from Kimi;
3. Kimi-K3 (``kimi.judge(ctx, call, run_id)``) when configured.

When Kimi is missing or fails the service **fails open** with
``{"decision": "allow", "source": "unavailable"}`` and publishes
``judge.unavailable`` - it never fabricates a verdict. Every verdict is appended
to ``<run_dir>/engine/judge.jsonl`` (or ``KYORA_HOME/logs/judge.jsonl``) and
published as ``judge.request`` / ``judge.verdict`` events.

Context dict (``register_context(run_id, ctx)``) keys the rules read:
``workspace`` (absolute path of the engine cwd; required for path rules),
``project_id``, ``run_dir``, ``files`` (plan file list: ``[{"path": ...}]`` or
``["src/game.js", ...]``), ``constraints`` (``{"no_external_deps": bool}``) or
a whole ``plan`` dict containing ``files``/``constraints``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..util.ids import now_iso
from .events import JUDGE_REQUEST, JUDGE_UNAVAILABLE, JUDGE_VERDICT

log = logging.getLogger("kyora.judge")

READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "TodoRead", "TodoWrite", "Task", "TaskOutput",
    "AskUserQuestion", "ExitPlanMode", "BashOutput", "KillShell", "ListMcpResourcesTool", "ReadMcpResourceTool",
})
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
DEFAULT_WRITE_PREFIXES = ("src/", "assets/", "tests/", ".kyora/")
DEFAULT_WRITE_FILES = ("index.html",)
DEFAULT_WRITE_FILE_PREFIXES = ("README",)
PROTECTED_PREFIXES = (".git/", ".claude/")
CACHE_MAX = 512
STATS_KEEP = 256
SUMMARY_LEN = 200

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]?")
_UNC_RE = re.compile(r"^[\\/]{2}[^\\/]")
# "&&", "||", ";", "|", newline and a lone "&" (background job) separate commands; the "&" of
# redirections such as "2>&1", "&>" and "|&" is not a separator.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|&?|(?<![<>&])&(?![>&])|\n")
_REDIRECT_NOISE_RE = re.compile(r"\s(2>&1|2>\s*/dev/null|>\s*/dev/null|&>\s*/dev/null)")
_RM_RE = re.compile(r"(?:^|[\s;&|(])rm\s+(?P<args>[^;&|]*)")
_DENY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:^|[\s;&|(`$])sudo(?:\s|$)"), "sudo / privilege escalation is not allowed"),
    (re.compile(r"(?:^|[\s;&|(`$])doas(?:\s|$)"), "doas / privilege escalation is not allowed"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b"), "git push is not allowed from the engine"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?remote\s+(?:add|set-url)\b"), "changing git remotes is not allowed"),
    (re.compile(r"\bchmod\s+(?:-[A-Za-z]+\s+)*[0-7]*777\b"), "chmod 777 is not allowed"),
    (re.compile(r"\bchmod\s+(?:-[A-Za-z]+\s+)*[augo]*\+[rwx]*w[rwx]*\s+/(?:\s|$)"), "world-writable root is not allowed"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "formatting devices is not allowed"),
    (re.compile(r"\bdd\s+(?:\S+\s+)*(?:if|of)=/dev/"), "raw device access is not allowed"),
    (re.compile(r">\s*/dev/(?:sd|nvme|hd|disk|mmcblk)"), "writing to block devices is not allowed"),
    (re.compile(r"(?:^|[\s;&|(])(?:shutdown|reboot|halt|poweroff)(?:\s|$)"), "power commands are not allowed"),
    (re.compile(r"\bkill\s+-9\s+-1\b|\bkillall\b|\bpkill\s+-9\s+\S*(?:python|node|claude)"), "killing host processes is not allowed"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:tag\s+(?:\S+\s+)*?(?:-d|--delete|-f|--force)(?:\s|$)"
                r"|branch\s+(?:\S+\s+)*?(?:-d|-D|--delete|-m|-M|--move|-f|--force)(?:\s|$)"
                r"|update-ref\s+(?:\S+\s+)*?-d(?:\s|$)|reflog\s+(?:expire|delete)\b|filter-branch\b|filter-repo\b)"),
     "deleting, moving or rewriting git refs would corrupt Kyora's slide history"),
)
_EXTERNAL_DEP_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:^|[\s;&|(`$])(?:curl|wget|aria2c)(?:\s|$)"), "network downloads (curl/wget)"),
    (re.compile(r"\bpip3?\s+(?:-\S+\s+)*install\b"), "pip install"),
    (re.compile(r"\bpython3?\s+-m\s+pip\s+(?:-\S+\s+)*install\b"), "pip install"),
    (re.compile(r"\bnpm\s+(?:-\S+\s+)*(?:i|install|add|ci|exec|x)\b"), "npm install"),
    (re.compile(r"(?:^|[\s;&|(`$])npx(?:\s|$)"), "npx"),
    (re.compile(r"\b(?:yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:add|install|dlx|x)\b"), "yarn/pnpm/bun install"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:clone|fetch|pull|submodule\s+update)\b"), "git network operations"),
    (re.compile(r"\b(?:apt(?:-get)?|apk|brew|pkg|choco|winget|scoop|dnf|yum|pacman)\s+(?:-\S+\s+)*(?:install|add)\b"), "system package install"),
    (re.compile(r"\bcargo\s+(?:install|add)\b|\bgo\s+(?:get|install)\b|\bgem\s+install\b"), "language package install"),
)
_GIT = r"^git\s+(?:-C\s+\S+\s+)?"
# Allow-list of safe argument shapes. A read-only segment is one whose command matches ONE of these and
# whose arguments carry none of the options that would run another program or write a file. Every entry is
# deliberately shaped so the fast path cannot be turned into an arbitrary-program / file-write primitive:
#   - no leading variable assignment (``FOO=bar cmd`` / a bare ``NAME=value``): deferred to Kimi, because an
#     assignment can set GIT_*, LD_*, PATH, IFS, BASH_ENV, PYTHON*, NODE_OPTIONS, ... and steer the tool;
#   - ``sort`` without ``-o``/``--output``/``--compress-program`` (the last spawns a program);
#   - ``rg`` without ``--pre``/``--pre-glob``/``--hostname-bin`` (preprocessor / hostname program);
#   - ``node``/``python`` only in their syntax-check and ``--version`` shapes, with no ``-e``/``--require``/
#     ``-r``/``--import`` or any other flag that loads and runs code;
#   - ``git`` read subcommands without ``--output``/``--ext-diff``/``--textconv`` (external diff / textconv
#     filters run a program); ``git -c KEY=VAL`` / ``git --config-env`` / an explicit ``git --exec-path`` never
#     match (only an optional ``-C DIR`` prefix is allowed), so a pager/fsmonitor/diff override is deferred;
#   - ``find`` without ``-exec``/``-execdir``/``-ok``/``-okdir``/``-delete``/``-fprint*``/``-fls``;
#   - ``date`` without ``-s``/``--set`` (setting the clock); pagers (``less``/``more``/``man``) are not here at
#     all because they can spawn an editor or shell (``!cmd``, ``v``); ``env CMD`` / ``xargs`` / ``awk`` /
#     ``sed`` / ``tar`` are likewise absent and always deferred.
_READ_ONLY_CMD_RES: tuple[re.Pattern, ...] = (
    # commands that only print; none of these has an option that runs a program or writes a file
    re.compile(r"^(?:ls|dir|cat|head|tail|wc|pwd|printf|echo|stat|file|du|df|which|type|whoami|uname|true|false|:|nl|cut|tr|od|hexdump|md5sum|sha1sum|sha256sum|basename|dirname|realpath|readlink|comm)(?:\s|$)"),
    re.compile(r"^date(?!.*(?:\s-s(?:\s|$)|--set))(?:\s|$)"),  # date -s / --set changes the clock
    re.compile(r"^env\s*$|^printenv(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s*$"),  # "env CMD" runs CMD
    re.compile(r"^sort(?!.*\s-(?:-output|-compress-program|[A-Za-z]*o))(?:\s|$)"),  # -o/--output write; --compress-program spawns
    re.compile(r"^(?:uniq|xxd)(?:\s+-\S+)*(?:\s+[^\s-]\S*)?(?:\s+-\S+)*\s*$"),  # a 2nd operand is an output file
    re.compile(r"^tree(?!.*\s-o(?:\s|$))(?:\s|$)"),  # tree -o FILE writes
    re.compile(r"^(?:grep|egrep|fgrep)(?:\s|$)"),
    re.compile(r"^rg(?!.*(?:--pre(?:=|\s|-glob)|--hostname-bin))(?:\s|$)"),  # --pre / --hostname-bin run a program
    re.compile(r"^find(?:\s|$)(?!.*(?:-delete|-exec|-execdir|-ok|-okdir|-fprint|-fprintf|-fls))"),
    re.compile(r"^node\s+(?:--check|-c)(?:\s+[^\s-]\S*)+\s*$"),  # only a syntax check of files; no -e/--require
    re.compile(r"^node\s+(?:--version|-v)\s*$"),
    re.compile(r"^(?:python3?|py)\s+-m\s+py_compile(?:\s+[^\s-]\S*)+\s*$"),  # only compile files; no trailing -c/-e
    re.compile(r"^(?:python3?|py)\s+(?:--version|-V)\s*$"),
    re.compile(r"^npm\s+(?:--version|-v|ls|list)(?:\s|$)"),
    re.compile(_GIT + r"(?:status|diff|log|show|rev-parse|ls-files|describe|blame|shortlog)(?:\s|$)(?!.*(?:--output|--ext-diff|--textconv))"),
    re.compile(_GIT + r"branch(?:\s+(?:-a|-r|-l|-v|-vv|--list|--all|--remotes|--verbose|--show-current|--no-color|--contains|--merged|--no-merged))*\s*$"),
    re.compile(_GIT + r"tag(?:\s+(?:-l|--list)(?:\s+\S+)?)?\s*$"),
    re.compile(_GIT + r"remote(?:\s+-v)?\s*$"),
    re.compile(r"^(?:test|\[)\s"),
    re.compile(r"^sleep\s+\d+(?:\.\d+)?\s*$"),
)
# A leading environment assignment turns any command into a non-read-only one (deferred to Kimi): it can set
# GIT_CONFIG_*, GIT_EXTERNAL_DIFF, GIT_PAGER, LD_PRELOAD, PATH, IFS, BASH_ENV, ENV, PYTHON*, NODE_OPTIONS,
# PERL5*, RUBYOPT, ... and make an otherwise read-only tool run an arbitrary program.
_ASSIGN_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")


# --------------------------------------------------------------------------- helpers
def tool_summary(tool_name: str, tool_input: Any) -> str:
    """Short human-readable description of a tool call (file path or command head)."""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:SUMMARY_LEN]
    for key in ("file_path", "notebook_path", "path"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val[:SUMMARY_LEN]
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd:
        return cmd.strip().replace("\n", " ")[:SUMMARY_LEN]
    for key in ("pattern", "query", "url", "description", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val[:SUMMARY_LEN]
    try:
        return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)[:SUMMARY_LEN]
    except (TypeError, ValueError):
        return str(tool_input)[:SUMMARY_LEN]


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _is_abs(path: str) -> bool:
    return path.startswith("/") or bool(_WIN_DRIVE_RE.match(path)) or bool(_UNC_RE.match(path)) or path.startswith("~")


def _norm_abs(path: str) -> str:
    """Normalise an absolute posix-ish path: collapse ``.``/``..``, keep the root/drive."""
    path = _posix(path)
    drive = ""
    m = _WIN_DRIVE_RE.match(path)
    if m:
        drive = path[:2].upper()
        path = path[2:]
    elif _UNC_RE.match(path):
        drive = "//"
        path = path[2:]
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return drive + "/" + "/".join(parts)


def _casefold_for(ws: str, other: str) -> bool:
    return bool(_WIN_DRIVE_RE.match(ws) or _WIN_DRIVE_RE.match(other) or ws.startswith("//") or other.startswith("//"))


def _relative_to(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` (both normalised posix-ish), ``None`` when outside."""
    a, b = (root.casefold(), path.casefold()) if _casefold_for(root, path) else (root, path)
    a = a.rstrip("/") or "/"
    if b == a:
        return ""
    if a == "/":
        return path.lstrip("/") if b.startswith("/") else None
    if b.startswith(a + "/"):
        return path[len(a):].lstrip("/")
    return None


def _real_posix(candidate: str) -> str:
    """Resolve symlinks of the deepest existing ancestor of ``candidate`` and re-append the rest.

    ``lexists`` is used so a dangling symlink still counts: writing through it
    creates its target, which may lie outside the workspace.
    """
    probe = candidate
    tail: list[str] = []
    while probe and not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        tail.append(os.path.basename(probe))
        probe = parent
    real = os.path.realpath(probe) if probe else probe
    for seg in reversed(tail):
        real = os.path.join(real, seg)
    return _norm_abs(real)


def resolve_in_workspace(raw: str, workspace: str, cwd: str | None = None) -> str | None:
    """Return the workspace-relative posix path of ``raw`` or ``None`` when it escapes.

    String logic first (works for Windows paths on POSIX and vice versa). When
    the workspace exists on this machine the real paths are compared as well:
    a symlink (also a dangling one) that leads outside is an escape, and a path
    spelled through a symlinked prefix of the workspace (macOS
    ``/private/var`` vs ``/var``) is accepted. Relative paths are resolved
    against ``cwd`` when it is absolute (even if it already left the
    workspace), else against the workspace. The workspace root is ``""``.
    """
    if not isinstance(raw, str) or not workspace:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("~") or "$" in raw or "%" in raw:
        return None  # home or environment expansion: cannot prove it stays inside
    ws = _norm_abs(workspace)
    if _is_abs(raw):
        candidate = _norm_abs(raw)
    else:
        base = _norm_abs(cwd) if (cwd and _is_abs(cwd) and not cwd.startswith("~")) else ws
        candidate = _norm_abs(base.rstrip("/") + "/" + _posix(raw))
    rel = _relative_to(candidate, ws)
    try:
        local = os.path.isabs(workspace) and os.path.isdir(workspace) and os.path.isabs(candidate)
    except (OSError, ValueError):
        local = False
    if not local:
        return rel
    try:
        ws_real = _real_posix(workspace)
        real_rel = _relative_to(_real_posix(candidate), ws_real)
    except (OSError, ValueError) as exc:
        log.warning("symlink check failed for %s: %s", raw, exc)
        return rel
    if real_rel is None:
        return None  # escapes once symlinks are resolved
    # the resolved path is authoritative: "ws/gitlink/config" with gitlink -> .git is ".git/config"
    return real_rel


def _normalized_input(tool_name: str, tool_input: Any, workspace: str, cwd: str | None) -> Any:
    """Input shape used for the cache key: relative paths, trimmed commands, content hashes."""
    if not isinstance(tool_input, dict):
        return tool_input
    out: dict[str, Any] = {}
    for key, val in sorted(tool_input.items()):
        if key in ("file_path", "notebook_path", "path") and isinstance(val, str):
            rel = resolve_in_workspace(val, workspace, cwd)
            out[key] = rel if rel is not None else _posix(val)
        elif key == "command" and isinstance(val, str):
            out[key] = " ".join(val.split())
        elif isinstance(val, str) and len(val) > 256:
            out[key] = "sha256:" + hashlib.sha256(val.encode("utf-8")).hexdigest()
        else:
            out[key] = val
    return out


def input_hash(tool_name: str, tool_input: Any, workspace: str = "", cwd: str | None = None) -> str:
    norm = _normalized_input(tool_name, tool_input, workspace, cwd)
    blob = json.dumps([tool_name, norm], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _plan_files(ctx: dict) -> list[str]:
    files = ctx.get("files")
    if files is None and isinstance(ctx.get("plan"), dict):
        files = ctx["plan"].get("files")
    out: list[str] = []
    for item in files or []:
        path = item.get("path") if isinstance(item, dict) else item
        if isinstance(path, str) and path:
            out.append(_strip_dot_prefix(_posix(path)))
    return out


def _strip_dot_prefix(path: str) -> str:
    """Drop leading ``./`` segments and slashes; keep dot-files such as ``.kyora/manifest.json``."""
    path = path.strip()
    while True:
        if path.startswith("./"):
            path = path[2:]
        elif path.startswith("/"):
            path = path[1:]
        else:
            return path


def _constraints(ctx: dict) -> dict:
    cons = ctx.get("constraints")
    if cons is None and isinstance(ctx.get("plan"), dict):
        cons = ctx["plan"].get("constraints")
    return cons if isinstance(cons, dict) else {}


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


class _Verdict(dict):
    """A verdict dict; ``dict`` subclass so the HTTP layer can JSON-encode it directly."""


def make_verdict(decision: str, reason: str, source: str, *, risk: str = "low", latency_ms: int = 0,
                 judge_model: str | None = None, rule: str | None = None) -> dict:
    v = _Verdict(decision=decision, reason=reason, risk=risk, source=source, judge_model=judge_model,
                 latency_ms=int(latency_ms))
    if rule:
        v["rule"] = rule
    return v


# --------------------------------------------------------------------------- rules
def rule_verdict(ctx: dict, call: dict) -> dict | None:
    """Deterministic local rules. Returns a verdict dict or ``None`` to defer to cache/Kimi."""
    tool_name = str(call.get("tool_name") or "")
    tool_input = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
    workspace = str(ctx.get("workspace") or "")
    cwd = call.get("cwd") if isinstance(call.get("cwd"), str) else None

    if tool_name in READ_ONLY_TOOLS:
        return make_verdict("allow", f"{tool_name} is read-only", "rule", rule="read_only_tool")
    if tool_name in NETWORK_TOOLS:
        return make_verdict("deny", f"{tool_name} is disabled: the game must not depend on network access during the build",
                            "rule", risk="medium", rule="network_tool")

    if tool_name in FILE_TOOLS:
        raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return make_verdict("deny", f"{tool_name} without a file path", "rule", risk="medium", rule="missing_path")
        if not workspace:
            return None
        rel = resolve_in_workspace(raw_path, workspace, cwd)
        if rel is None:
            return make_verdict("deny", f"path escapes the project workspace: {raw_path}", "rule", risk="high",
                                rule="path_escape")
        rel_cf = rel.casefold()
        for prot in PROTECTED_PREFIXES:
            if rel_cf == prot.rstrip("/") or rel_cf.startswith(prot):
                return make_verdict("deny", f"writes under {prot} are not allowed: {raw_path}", "rule", risk="high",
                                    rule="protected_path")
        if rel == "":
            return make_verdict("deny", "cannot write the workspace root itself", "rule", risk="high", rule="protected_path")
        if rel in _plan_files(ctx):
            return make_verdict("allow", f"planned file: {rel}", "rule", rule="planned_file")
        if rel in DEFAULT_WRITE_FILES or any(rel.startswith(p) for p in DEFAULT_WRITE_PREFIXES) \
                or any(rel.startswith(p) for p in DEFAULT_WRITE_FILE_PREFIXES):
            return make_verdict("allow", f"standard project path: {rel}", "rule", rule="standard_path")
        return None  # other in-workspace writes: let Kimi judge

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return make_verdict("deny", "empty Bash command", "rule", risk="low", rule="empty_command")
        return _bash_rule(ctx, command, workspace, cwd)
    return None


def _bash_rule(ctx: dict, command: str, workspace: str, cwd: str | None) -> dict | None:
    cmd = command.strip()
    for pattern, why in _DENY_PATTERNS:
        if pattern.search(cmd):
            return make_verdict("deny", why, "rule", risk="high", rule="dangerous_command")
    if _constraints(ctx).get("no_external_deps"):
        for pattern, why in _EXTERNAL_DEP_PATTERNS:
            if pattern.search(cmd):
                return make_verdict("deny", f"{why} is not allowed: the plan forbids external dependencies and network access",
                                    "rule", risk="medium", rule="external_deps")
    rm_reason = _rm_outside_workspace(cmd, workspace, cwd)
    if rm_reason:
        return make_verdict("deny", rm_reason, "rule", risk="high", rule="rm_outside_workspace")
    if _is_read_only_command(cmd, workspace, cwd):
        return make_verdict("allow", "read-only shell command", "rule", rule="read_only_command")
    return None


def _segments(cmd: str) -> list[str]:
    """Split a shell command into simple commands; subshell/group braces are stripped."""
    out: list[str] = []
    for part in _SEGMENT_SPLIT_RE.split(cmd):
        part = (part or "").strip()
        # "( cd .. && rm -rf x )" -> "cd ..", "rm -rf x"; "$(" keeps its "$" so it stays detectable
        while part and part[0] in "({":
            part = part[1:].strip()
        while part and part[-1] in ")}" and "$(" not in part:
            part = part[:-1].strip()
        if part:
            out.append(part)
    return out


def _virtual_cwds(cmd: str, workspace: str, cwd: str | None) -> list[tuple[str, str | None]]:
    """Yield ``(segment, virtual_cwd_rel)`` pairs, tracking ``cd`` (``None`` = outside workspace)."""
    out: list[tuple[str, str | None]] = []
    vcwd: str | None = resolve_in_workspace(cwd, workspace) if (cwd and workspace) else ""
    for seg in _segments(cmd):
        out.append((seg, vcwd))
        m = re.match(r"^cd(?:\s+(?P<target>\S+))?\s*$", seg)
        if m:
            target = m.group("target")
            if not target or target in ("~", "-"):
                vcwd = None
            elif workspace:
                target = _strip_quotes(target)
                if vcwd is None and not _is_abs(target):
                    continue  # already outside; a relative cd keeps us outside
                base = (workspace.rstrip("/\\") + "/" + vcwd) if vcwd else None
                vcwd = resolve_in_workspace(target, workspace, base)
    return out


def _rm_outside_workspace(cmd: str, workspace: str, cwd: str | None) -> str | None:
    for seg, vcwd in _virtual_cwds(cmd, workspace, cwd):
        for m in _RM_RE.finditer(" " + seg):
            tokens = m.group("args").split()
            flags = [t for t in tokens if t.startswith("-")]
            targets = [_strip_quotes(t) for t in tokens if not t.startswith("-")]
            recursive = any(("r" in f.lower() and not f.startswith("--")) or f == "--recursive" for f in flags)
            if not recursive or not targets:
                continue
            for target in targets:
                if target in ("/", "/*", "~", "~/", "*", ".", "./", "..", "../") or target.startswith("~"):
                    return f"recursive rm of '{target}' would delete outside or all of the workspace"
                if "$" in target or "%" in target:
                    return f"recursive rm with an environment-expanded target ('{target}') cannot be verified"
                if not workspace:
                    if _is_abs(target):
                        return f"recursive rm of absolute path '{target}' is not allowed"
                    continue
                if vcwd is None and not _is_abs(target):
                    return f"recursive rm of '{target}' after leaving the workspace is not allowed"
                base = (workspace.rstrip("/\\") + ("/" + vcwd if vcwd else "")) if not _is_abs(target) else None
                rel = resolve_in_workspace(target, workspace, base)
                if rel is None:
                    return f"recursive rm outside the workspace is not allowed: {target}"
                if rel == "" or rel.casefold() == ".git" or rel.casefold().startswith(".git/"):
                    return f"recursive rm of '{target}' would delete the workspace or its git history"
    return None


def _is_read_only_command(cmd: str, workspace: str, cwd: str | None) -> bool:
    segs = _virtual_cwds(cmd, workspace, cwd)
    if not segs:
        return False
    for seg, _vcwd in segs:
        clean = _REDIRECT_NOISE_RE.sub(" ", " " + seg).strip()
        if ">" in clean or "<<" in clean or "<(" in clean:
            return False
        if "`" in clean or "$(" in clean:
            return False
        if re.match(r"^cd(?:\s+\S+)?\s*$", clean):
            m = re.match(r"^cd\s+(\S+)", clean)
            if m and workspace and resolve_in_workspace(_strip_quotes(m.group(1)), workspace, cwd) is None:
                return False
            continue
        if _ASSIGN_PREFIX_RE.match(clean):
            return False  # "FOO=bar ..." (or a bare assignment): an env var could steer a later tool
        if not any(r.match(clean) for r in _READ_ONLY_CMD_RES):
            return False
    return True


# --------------------------------------------------------------------------- service
class JudgeService:
    """Judges code-engine tool calls; see module docstring for the decision order."""

    def __init__(self, kimi, bus, config, home: Path):
        self.kimi = kimi
        self.bus = bus
        self.config = config
        self.home = Path(home)
        self._lock = threading.RLock()
        self._contexts: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self._cache: dict[str, OrderedDict[str, dict]] = {}
        self._stats: OrderedDict[str, dict] = OrderedDict()
        self._verdict_ids: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ context lifecycle
    def register_context(self, run_id: str, ctx: dict) -> str:
        """Register the judging context of a run and return its per-run hook token."""
        if not run_id:
            raise ValueError("run_id required")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._contexts[run_id] = dict(ctx or {})
            self._tokens[run_id] = token
            self._cache.setdefault(run_id, OrderedDict())
            self._verdict_ids.setdefault(run_id, set())
            self._stats.setdefault(run_id, self._empty_stats())
            self._stats.move_to_end(run_id)
            while len(self._stats) > STATS_KEEP:
                old, _ = self._stats.popitem(last=False)
                self._verdict_ids.pop(old, None)
        return token

    def forget(self, run_id: str) -> None:
        """Drop the context, token and cache of a run (stats and verdict ids are kept)."""
        with self._lock:
            self._contexts.pop(run_id, None)
            self._tokens.pop(run_id, None)
            self._cache.pop(run_id, None)

    def context(self, run_id: str) -> dict | None:
        with self._lock:
            ctx = self._contexts.get(run_id)
            return dict(ctx) if ctx is not None else None

    def has_verdict(self, run_id: str, tool_use_id: str) -> bool:
        """True when a verdict for ``tool_use_id`` was journaled for this run."""
        with self._lock:
            return tool_use_id in self._verdict_ids.get(run_id, set())

    def record_unavailable(self, run_id: str, *, allowed: bool = True) -> None:
        """Count a judged tool call that never reached the judge (hook unreachable -> ran unjudged).

        The engine calls this when a Write/Edit/Bash produced no journaled verdict, so the run's
        ``unavailable`` counter matches the ``judge.unavailable`` events the engine emits. The call still
        ran (fail-open), so it is also counted as ``allowed`` unless a block is reported.
        """
        if not run_id:
            return
        with self._lock:
            stats = self._stats.setdefault(run_id, self._empty_stats())
            stats["allowed" if allowed else "denied"] += 1
            stats["unavailable"] += 1

    def prime_stats(self, run_id: str, counters: Any) -> None:
        """Seed a run's counters from a persisted record (on resume), without double-counting.

        A resumed run starts a fresh process whose in-memory counters are empty; restoring the counts
        saved in ``run.json`` keeps the total across the restart. It is a no-op once counting has begun in
        this process (non-zero counters), so repeated build/fix calls never re-seed.
        """
        if not run_id or not isinstance(counters, dict):
            return
        seeded = self._empty_stats()
        has_value = False
        for key in seeded:
            val = counters.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                seeded[key] = int(val)
                has_value = has_value or val > 0
        if not has_value:
            return
        with self._lock:
            cur = self._stats.get(run_id)
            if cur is not None and any(cur.values()):
                return  # already counting in this process; keep the live totals
            self._stats[run_id] = seeded
            self._stats.move_to_end(run_id)

    # ------------------------------------------------------------------ HTTP entry point
    def handle_request(self, payload: Any) -> tuple[int, dict]:
        """Validate a hook request and return ``(http_status, body)``."""
        if not isinstance(payload, dict):
            return 400, {"error": "payload must be a JSON object", "decision": "deny"}
        run_id = str(payload.get("run_id") or "")
        token = payload.get("hook_token")
        with self._lock:
            expected = self._tokens.get(run_id)
        # compare bytes: hmac.compare_digest raises TypeError for non-ASCII str arguments
        if not run_id or not expected or not isinstance(token, str) \
                or not hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8", "replace")):
            return 403, {"error": "invalid or unknown hook token", "decision": "deny"}
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return 400, {"error": "tool_name required", "decision": "deny"}
        call = {
            "tool_name": tool_name,
            "tool_input": payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {},
            "tool_use_id": str(payload.get("tool_use_id") or ""),
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "hook_event_name": payload.get("hook_event_name") or "PreToolUse",
        }
        # The hook sends the budget (ms) it will wait before failing open, a little under its own timeout.
        # A Kimi verdict that arrives after it never governed the call, so it is recorded as late only.
        deadline_ms = payload.get("deadline_ms")
        if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, (int, float)) or deadline_ms <= 0:
            deadline_ms = None
        return 200, self.decide(run_id, call, deadline_ms=deadline_ms)

    # ------------------------------------------------------------------ decision
    def decide(self, run_id: str, call: dict, *, deadline_ms: float | None = None) -> dict:
        """Judge one tool call. Always returns a verdict dict (fail-open on judge errors).

        ``deadline_ms`` (sent by the hook, a little under its own timeout) bounds how long a Kimi verdict
        may take before the hook gives up and the tool runs. A Kimi verdict that arrives after it did not
        govern the call: it is journaled as late (and still seeds the cache) but is never counted and its
        ``tool_use_id`` is never registered, so :meth:`has_verdict` stays ``False`` and the engine counts
        the call as unavailable. ``None`` (the direct Messages-API path, which waits synchronously) applies
        no deadline. The deterministic rules and the cache are instant, so only the Kimi path can be late.
        """
        started = time.monotonic()
        with self._lock:
            ctx = dict(self._contexts.get(run_id) or {})
        tool_name = str(call.get("tool_name") or "")
        tool_input = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
        tool_use_id = str(call.get("tool_use_id") or "")
        summary = tool_summary(tool_name, tool_input)
        project_id = ctx.get("project_id")
        self._publish(JUDGE_REQUEST, {"tool_name": tool_name, "summary": summary, "tool_use_id": tool_use_id},
                      run_id, project_id)

        verdict: dict | None = None
        try:
            verdict = rule_verdict(ctx, call)
        except Exception as exc:  # noqa: BLE001 - a rule bug must not block the engine silently
            log.exception("judge rule error for %s: %s", tool_name, exc)
            verdict = None

        key = None
        if verdict is None:
            key = input_hash(tool_name, tool_input, str(ctx.get("workspace") or ""), call.get("cwd"))
            with self._lock:
                cache = self._cache.get(run_id)
                hit = cache.get(key) if cache is not None else None
                if hit is not None:
                    cache.move_to_end(key)
            if hit is not None:
                verdict = make_verdict(hit["decision"], hit.get("reason", ""), "cache", risk=hit.get("risk", "low"),
                                       judge_model=hit.get("judge_model"))
                verdict["cached_from"] = hit.get("source", "kimi")

        if verdict is None:
            verdict = self._ask_kimi(run_id, ctx, call, summary)
            if verdict.get("source") == "kimi" and key is not None:
                with self._lock:
                    cache = self._cache.setdefault(run_id, OrderedDict())
                    cache[key] = dict(verdict)
                    while len(cache) > CACHE_MAX:
                        cache.popitem(last=False)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        verdict["latency_ms"] = elapsed_ms
        verdict.setdefault("judge_model", None)
        # A Kimi verdict that only came back after the hook's deadline no longer governs the call: the hook
        # has already failed open and the tool ran. Record it as a late audit entry (the cache was seeded
        # above) and return a non-governing allow, so the call is counted as unavailable, never as a Kimi
        # decision for a call that actually ran.
        if deadline_ms is not None and verdict.get("source") == "kimi" and elapsed_ms >= deadline_ms:
            self._record_late(run_id, ctx, call, summary, verdict, deadline_ms)
            return make_verdict("allow", f"Kimi verdict arrived after the {int(deadline_ms)} ms hook deadline "
                                f"({elapsed_ms} ms); the tool call ran unjudged", "timeout",
                                risk="unknown", latency_ms=elapsed_ms, judge_model=verdict.get("judge_model"))
        self._record(run_id, ctx, call, summary, verdict)
        return verdict

    def _ask_kimi(self, run_id: str, ctx: dict, call: dict, summary: str) -> dict:
        if self.kimi is None:
            reason = "Kimi judge not configured"
            self._publish(JUDGE_UNAVAILABLE, {"reason": reason, "tool_name": call.get("tool_name"),
                                              "tool_use_id": call.get("tool_use_id")}, run_id, ctx.get("project_id"))
            return make_verdict("allow", f"{reason}; tool call allowed unjudged", "unavailable", risk="unknown")
        try:
            raw = self.kimi.judge(ctx, call, run_id)
        except Exception as exc:  # noqa: BLE001 - model/network failures fail open, visibly
            reason = f"Kimi judge failed: {type(exc).__name__}: {str(exc)[:300]}"
            log.warning("%s (run %s, %s)", reason, run_id, summary)
            self._publish(JUDGE_UNAVAILABLE, {"reason": reason, "tool_name": call.get("tool_name"),
                                              "tool_use_id": call.get("tool_use_id")}, run_id, ctx.get("project_id"))
            return make_verdict("allow", f"{reason}; tool call allowed unjudged", "unavailable", risk="unknown")
        if not isinstance(raw, dict) or str(raw.get("decision", "")).lower() not in ("allow", "deny"):
            reason = "Kimi judge returned no usable verdict"
            self._publish(JUDGE_UNAVAILABLE, {"reason": reason, "tool_name": call.get("tool_name"),
                                              "tool_use_id": call.get("tool_use_id")}, run_id, ctx.get("project_id"))
            return make_verdict("allow", f"{reason}; tool call allowed unjudged", "unavailable", risk="unknown")
        model = raw.get("judge_model") or getattr(self.kimi, "model", None)
        return make_verdict(str(raw["decision"]).lower(), str(raw.get("reason") or ""), "kimi",
                            risk=str(raw.get("risk") or "low"), judge_model=model)

    # ------------------------------------------------------------------ bookkeeping
    def _record(self, run_id: str, ctx: dict, call: dict, summary: str, verdict: dict) -> None:
        tool_name = str(call.get("tool_name") or "")
        tool_use_id = str(call.get("tool_use_id") or "")
        source = verdict.get("source", "")
        with self._lock:
            stats = self._stats.setdefault(run_id, self._empty_stats())
            stats["allowed" if verdict.get("decision") == "allow" else "denied"] += 1
            if source == "unavailable":
                stats["unavailable"] += 1
            elif source == "cache":
                stats["cached"] += 1
            elif source == "rule":
                stats["rule"] += 1
            elif source == "kimi":
                stats["kimi"] += 1
            if tool_use_id:
                self._verdict_ids.setdefault(run_id, set()).add(tool_use_id)
        entry = {
            "ts": now_iso(), "run_id": run_id, "tool_use_id": tool_use_id, "tool_name": tool_name, "summary": summary,
            "decision": verdict.get("decision"), "reason": verdict.get("reason"), "risk": verdict.get("risk"),
            "source": source, "rule": verdict.get("rule"), "judge_model": verdict.get("judge_model"),
            "latency_ms": verdict.get("latency_ms"), "session_id": call.get("session_id"),
            "tool_input_excerpt": self._excerpt(call.get("tool_input")),
        }
        self._append_journal(ctx, entry)
        self._publish(JUDGE_VERDICT, {"decision": verdict.get("decision"), "reason": verdict.get("reason"),
                                      "source": source, "latency_ms": verdict.get("latency_ms"), "tool_name": tool_name,
                                      "tool_use_id": tool_use_id, "risk": verdict.get("risk"), "summary": summary},
                      run_id, ctx.get("project_id"))

    def _record_late(self, run_id: str, ctx: dict, call: dict, summary: str, verdict: dict,
                     deadline_ms: float) -> None:
        """Journal a Kimi verdict that arrived after the hook deadline, without letting it govern the call.

        The hook already failed open and the tool ran, so this verdict is NOT counted in the run's stats and
        its ``tool_use_id`` is NOT registered: :meth:`has_verdict` stays ``False``, and the engine emits
        ``judge.unavailable`` and counts the call as unavailable, exactly as for any call the hook could not
        reach. Only the journal keeps the late verdict, for audit.
        """
        tool_use_id = str(call.get("tool_use_id") or "")
        entry = {
            "ts": now_iso(), "run_id": run_id, "tool_use_id": tool_use_id,
            "tool_name": str(call.get("tool_name") or ""), "summary": summary,
            "decision": verdict.get("decision"), "reason": verdict.get("reason"), "risk": verdict.get("risk"),
            "source": verdict.get("source"), "rule": verdict.get("rule"), "judge_model": verdict.get("judge_model"),
            "latency_ms": verdict.get("latency_ms"), "session_id": call.get("session_id"),
            "late": True, "governing": False, "deadline_ms": int(deadline_ms),
            "tool_input_excerpt": self._excerpt(call.get("tool_input")),
        }
        self._append_journal(ctx, entry)
        log.warning("judge verdict for %s arrived after the %d ms hook deadline (%s ms); recorded late, not "
                    "governing; the call ran unjudged", tool_use_id or "?", int(deadline_ms),
                    verdict.get("latency_ms"))

    @staticmethod
    def _excerpt(tool_input: Any, limit: int = 1200) -> str:
        try:
            text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(tool_input)
        return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"

    def journal_path(self, ctx: dict) -> Path:
        run_dir = ctx.get("run_dir")
        if run_dir:
            return Path(run_dir) / "engine" / "judge.jsonl"
        return self.home / "logs" / "judge.jsonl"

    def _append_journal(self, ctx: dict, entry: dict) -> None:
        path = self.journal_path(ctx)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.error("cannot append judge journal %s: %s", path, exc)

    def _publish(self, type_: str, data: dict, run_id: str | None, project_id: str | None) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(type_, data, run_id=run_id or None, project_id=project_id or None)
        except Exception as exc:  # noqa: BLE001
            log.error("judge event publish failed (%s): %s", type_, exc)

    @staticmethod
    def _empty_stats() -> dict:
        return {"allowed": 0, "denied": 0, "unavailable": 0, "cached": 0, "rule": 0, "kimi": 0}

    def stats(self, run_id: str) -> dict:
        """Verdict counters for a run: ``{allowed, denied, unavailable, cached, rule, kimi}``."""
        with self._lock:
            return dict(self._stats.get(run_id) or self._empty_stats())
