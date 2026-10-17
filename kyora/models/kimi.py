"""Moonshot Kimi-K3 wrapper: Kyora's planner, instant judge and reviewer (role ``kimi``).

Kimi-K3 is reached through the NVIDIA API Catalog (OpenAI-compatible, model
``moonshotai/kimi-k3``) with :class:`~kyora.models.nim_client.NIMClient`. It
does four jobs in the pipeline:

* :meth:`Kimi.intake` - turns the user's prompt and attachments (reference
  images are sent as ``image_url`` data URLs, documents as excerpts, ZIPs as
  listings) into the intake record;
* :meth:`Kimi.plan` - writes ``plan.json`` (file list, public contract, task
  DAG, acceptance tests), validated strictly with one repair round-trip;
* :meth:`Kimi.judge` - the instant verdict on one code-engine tool call
  (called by :class:`kyora.pipeline.judge.JudgeService`, which fails open);
* :meth:`Kimi.review` - the fix/stop decision after a failed gate, with an
  honesty guard: ``complete`` is never accepted while the measured gate failed.

Sampling is fixed at ``temperature 1.0, top_p 1.0``; the reasoning effort is
sent as the top-level ``reasoning_effort`` field or, with
``settings.kimi.effort_field = extra_body``, as the chat-template keyword
``chat_template_kwargs.reasoning_effort`` (see :meth:`Kimi.effort_extra`). Kimi's preserved thinking requires the
COMPLETE assistant message (``reasoning_content`` and ``tool_calls``) to be
echoed back on the next turn; :class:`KimiConversation` stores exactly what
:attr:`ChatResult.message` returns.

Nothing here fabricates output: API failures raise :class:`NIMError`, and an
answer that cannot be parsed or validated (after one repair request) raises
``ValueError``.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .nim_client import (
    ChatResult, NIMClient, NIMError, chat_with_events, extract_structured, force_tool, function_tool,
    is_tool_rejection, json_for_prompt, render_prompt, tool_call_arguments,
)

log = logging.getLogger("kyora.models.kimi")

ROLE = "kimi"
ENGINES: tuple[str, ...] = ("vanilla", "phaser", "three", "zip-only")
TEST_KINDS: tuple[str, ...] = ("browser", "node")
DEFAULT_CONSTRAINTS: dict = {"no_external_deps": True, "max_total_mb": 25, "touch_controls": True}
DEFAULT_PLATFORM = "web (touch + keyboard)"
MAX_INTAKE_IMAGES = 6
MAX_REVIEW_SCREENSHOTS = 4
JUDGE_STRING_LIMIT = 4000
MAX_IMAGE_DATA_URL_CHARS = 12_000_000
EXCERPT_CHARS = 3000
EXCERPT_BUDGET = 24_000
LISTING_ITEMS = 80
GATE_NOT_PASSED_REASON = "gate not passed; complete is not allowed"

# Request shapes tried for structured output, most specific first. A gateway that
# rejects a forced ``tool_choice`` (Kimi thinking mode accepts only ``auto``) is
# retried with ``auto``; one that rejects ``tools`` altogether, without tools.
TOOL_MODES: tuple[str, ...] = ("forced", "auto", "none")

_ENGINE_ALIASES = {
    "html5": "vanilla", "canvas": "vanilla", "vanilla-js": "vanilla", "vanillajs": "vanilla",
    "phaser3": "phaser", "phaser.js": "phaser", "phaserjs": "phaser",
    "three.js": "three", "threejs": "three",
    "zip_only": "zip-only", "ziponly": "zip-only", "zip only": "zip-only",
}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TRUE_STRINGS = {"true", "yes", "y", "1", "on"}
_FALSE_STRINGS = {"false", "no", "n", "0", "off", ""}

# ----------------------------------------------------------------------------- tool schemas
_STR_LIST = {"type": "array", "items": {"type": "string"}}

INTAKE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "description": "One or two sentences: what the user wants built or changed."},
        "genre": {"type": "string"},
        "platform": {"type": "string"},
        "must_haves": {**_STR_LIST, "description": "Concrete, testable requirements (at most 12)."},
        "references": {**_STR_LIST, "description": "Attachments/sources to honour and what they show."},
        "risks": {**_STR_LIST, "description": "Ambiguities, scope or feasibility risks."},
        "imported_base": {"type": "boolean", "description": "True only when a ZIP marked for import is attached."},
        "notes": {"type": "string"},
    },
    "required": ["goal", "genre", "platform", "must_haves", "references", "risks", "imported_base", "notes"],
}

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "engine": {"type": "string", "enum": list(ENGINES)},
        "preview": {"type": "boolean"},
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}},
                      "required": ["path", "purpose"]},
            "description": "Every file the engine must create; must include index.html.",
        },
        "contract": {
            "type": "object",
            "properties": {"globals": _STR_LIST,
                           "api": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["globals", "api"],
        },
        "tasks": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                     "depends_on": _STR_LIST, "files": _STR_LIST, "done_when": {"type": "string"}},
                      "required": ["id", "title", "depends_on", "files", "done_when"]},
        },
        "acceptance_tests": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                     "kind": {"type": "string", "enum": list(TEST_KINDS)},
                                     "steps": {"type": "string"}, "required": {"type": "boolean"}},
                      "required": ["id", "name", "kind", "steps", "required"]},
        },
        "constraints": {
            "type": "object",
            "properties": {"no_external_deps": {"type": "boolean"}, "max_total_mb": {"type": "number"},
                           "touch_controls": {"type": "boolean"}},
        },
        "design_brief_for_nemotron": {"type": "string"},
    },
    "required": ["title", "engine", "files", "contract", "tasks", "acceptance_tests", "constraints",
                 "design_brief_for_nemotron"],
}

VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "deny"]},
        "reason": {"type": "string", "description": "One or two sentences the engine can act on."},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["decision", "reason", "risk"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["complete", "fix", "stop"]},
        "reasons": _STR_LIST,
        "fix_instructions": {"type": "string", "description": "Concrete ordered steps for the code engine."},
        "suspected_files": _STR_LIST,
    },
    "required": ["decision", "reasons", "fix_instructions", "suspected_files"],
}

INTAKE_TOOL = function_tool("emit_intake", "Emit the intake record for the planner.", INTAKE_SCHEMA)
NODE_RULES = {
    True: "- Node.js is available on the device that runs the tests: kind \"node\" tests run with `node --test`.",
    False: ("- Node.js is NOT available on the device that runs the tests (for example iSH on iPhone, or Windows "
            "without Node): every acceptance test must have kind \"browser\". Do not plan kind \"node\" tests; "
            "they could never run and would block completion."),
    None: "",
}
PLAN_TOOL = function_tool("emit_plan", "Emit the complete plan: files, contract, task DAG, acceptance tests, constraints, "
                                       "design brief.", PLAN_SCHEMA)
VERDICT_TOOL = function_tool("verdict", "Allow or deny the code engine's pending tool call.", VERDICT_SCHEMA)
REVIEW_TOOL = function_tool("emit_review", "Emit the review decision after a failed quality gate.", REVIEW_SCHEMA)


# ----------------------------------------------------------------------------- small coercions
def _str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_str(v) for v in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            s = _str(v).strip()
            if s:
                out.append(s)
        return out
    s = _str(value).strip()
    return [s] if s else []


def _bool(value: Any, default: bool) -> bool:
    """Coerce booleans the way models write them; raises ``ValueError`` on nonsense."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRINGS:
            return True
        if low in _FALSE_STRINGS:
            return False
    raise ValueError(f"not a boolean: {value!r}")


def _human_size(size: Any) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return ""


def normalize_rel_path(raw: Any) -> str:
    """Normalise a project-relative path: forward slashes, no leading ``./`` or ``/``, no ``..``.

    Raises ``ValueError`` for empty paths, drive letters, home-relative paths and
    any ``..`` segment (a plan path must stay inside the project root).
    """
    text = _str(raw).strip().replace("\\", "/")
    if not text:
        raise ValueError("empty path")
    if "\x00" in text:
        raise ValueError(f"path contains a NUL byte: {text!r}")
    if _DRIVE_RE.match(text):
        raise ValueError(f"absolute path not allowed: {text!r}")
    parts = [seg for seg in text.split("/") if seg not in ("", ".")]
    if not parts:
        raise ValueError(f"empty path: {text!r}")
    if any(seg == ".." for seg in parts):
        raise ValueError(f"path escapes the project root: {text!r}")
    if parts[0] == "~" or parts[0].startswith("~"):
        raise ValueError(f"home-relative path not allowed: {text!r}")
    return "/".join(parts)


def _norm_engine(value: Any) -> str:
    eng = _str(value).strip().lower()
    return _ENGINE_ALIASES.get(eng, eng)


# ----------------------------------------------------------------------------- normalisers
def normalize_intake(obj: Mapping, *, has_import: bool) -> dict:
    """Validate and normalise an ``emit_intake`` payload.

    ``imported_base`` is set from the facts Kyora knows (an attachment marked for
    import exists) - the model's claim cannot import a ZIP that is not there.
    Raises ``ValueError`` when ``goal`` is missing.
    """
    if not isinstance(obj, Mapping):
        raise ValueError("intake payload is not a JSON object")
    goal = _str(obj.get("goal")).strip()
    if not goal:
        raise ValueError("missing required field 'goal' (one or two sentences describing what to build or change)")
    claimed = obj.get("imported_base")
    try:
        claimed_bool = _bool(claimed, has_import)
    except ValueError:
        claimed_bool = has_import
    if claimed_bool != has_import:
        log.info("kimi intake: imported_base=%r from the model corrected to %r (attachment facts)", claimed, has_import)
    return {
        "goal": goal,
        "genre": _str(obj.get("genre")).strip(),
        "platform": _str(obj.get("platform")).strip() or DEFAULT_PLATFORM,
        "must_haves": _str_list(obj.get("must_haves")),
        "references": _str_list(obj.get("references")),
        "risks": _str_list(obj.get("risks")),
        "imported_base": bool(has_import),
        "notes": _str(obj.get("notes")).strip(),
    }


def _norm_files(raw: Any, problems: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        problems.append("'files' must be a list of {path, purpose} objects")
        return []
    files: list[dict] = []
    index: dict[str, int] = {}
    for i, item in enumerate(raw):
        if isinstance(item, Mapping):
            path_raw = item.get("path") or item.get("file") or item.get("name")
            purpose = _str(item.get("purpose") or item.get("description")).strip()
        else:
            path_raw, purpose = item, ""
        try:
            path = normalize_rel_path(path_raw)
        except ValueError as exc:
            problems.append(f"files[{i}]: {exc}")
            continue
        if path in index:
            if purpose and not files[index[path]]["purpose"]:
                files[index[path]]["purpose"] = purpose
            continue
        index[path] = len(files)
        files.append({"path": path, "purpose": purpose})
    if "index.html" not in index:
        problems.append("'files' must include 'index.html' (the entry point at the project root)")
    return files


def _norm_contract(raw: Any, problems: list[str]) -> dict:
    if not isinstance(raw, Mapping) or not raw:
        problems.append("'contract' must be a non-empty object with 'globals' and 'api' (window.Game with "
                        "Game.start() and Game.getState())")
        return {}
    contract = copy.deepcopy(dict(raw))
    if "globals" in contract:
        contract["globals"] = _str_list(contract.get("globals"))
    api = contract.get("api")
    if isinstance(api, Mapping):
        contract["api"] = {str(k): _str(v) for k, v in api.items()}
    elif isinstance(api, list):
        contract["api"] = {_str(v).strip(): "" for v in api if _str(v).strip()}
    return contract


def _norm_tasks(raw: Any, problems: list[str]) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        problems.append("'tasks' must be a non-empty list of {id, title, depends_on, files, done_when}")
        return []
    tasks: list[dict] = []
    seen: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            problems.append(f"tasks[{i}] is not an object")
            continue
        tid = _str(item.get("id")).strip()
        if not tid:
            problems.append(f"tasks[{i}] has no 'id'")
            continue
        if tid in seen:
            problems.append(f"duplicate task id {tid!r}")
            continue
        deps = _str_list(item.get("depends_on") if "depends_on" in item else item.get("deps"))
        for dep in deps:
            if dep not in seen:
                problems.append(f"task {tid!r} depends on {dep!r}, which is not an earlier task id "
                                f"(depends_on may only reference earlier tasks)")
        files: list[str] = []
        for f in _str_list(item.get("files")):
            try:
                files.append(normalize_rel_path(f))
            except ValueError as exc:
                problems.append(f"task {tid!r} files: {exc}")
        seen.append(tid)
        tasks.append({"id": tid, "title": _str(item.get("title")).strip() or tid, "depends_on": deps,
                      "files": files, "done_when": _str(item.get("done_when")).strip()})
    return tasks


def _norm_tests(raw: Any, min_tests: int, problems: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        problems.append("'acceptance_tests' must be a list of {id, name, kind, steps, required}")
        return []
    tests: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            problems.append(f"acceptance_tests[{i}] is not an object")
            continue
        tid = _str(item.get("id")).strip()
        if not tid:
            problems.append(f"acceptance_tests[{i}] has an empty id")
            continue
        if tid in seen:
            problems.append(f"duplicate acceptance test id {tid!r}")
            continue
        kind = _str(item.get("kind")).strip().lower() or "browser"
        if kind not in TEST_KINDS:
            problems.append(f"acceptance test {tid!r} has kind {kind!r}; allowed: {', '.join(TEST_KINDS)}")
            continue
        try:
            required = _bool(item.get("required"), False)
        except ValueError:
            problems.append(f"acceptance test {tid!r}: 'required' must be true or false")
            continue
        seen.add(tid)
        tests.append({"id": tid, "name": _str(item.get("name") or item.get("title")).strip() or tid, "kind": kind,
                      "steps": _str(item.get("steps")).strip(), "required": required})
    if len(tests) < min_tests:
        problems.append(f"only {len(tests)} valid acceptance tests; at least {min_tests} are required")
    return tests


def _norm_constraints(raw: Any, problems: list[str]) -> dict:
    out = dict(DEFAULT_CONSTRAINTS)
    if raw is None:
        return out
    if not isinstance(raw, Mapping):
        problems.append("'constraints' must be an object")
        return out
    for key, value in raw.items():
        key = str(key)
        if key in ("no_external_deps", "touch_controls"):
            try:
                out[key] = _bool(value, DEFAULT_CONSTRAINTS[key])
            except ValueError:
                problems.append(f"constraints.{key} must be true or false")
        elif key == "max_total_mb":
            try:
                num = float(value)
            except (TypeError, ValueError):
                problems.append("constraints.max_total_mb must be a number")
                continue
            if not math.isfinite(num) or num <= 0:
                problems.append("constraints.max_total_mb must be a positive number")
                continue
            out[key] = int(num) if num.is_integer() else num
        else:
            out[key] = copy.deepcopy(value)
    return out


def normalize_plan(obj: Mapping, *, min_tests: int = 6) -> dict:
    """Validate and normalise an ``emit_plan`` payload into the ``plan.json`` shape.

    Raises ``ValueError`` listing every problem found (missing keys, unknown
    engine, no ``index.html``, path escapes, forward task dependencies, duplicate
    or too few acceptance tests ...). The message is sent back to Kimi verbatim
    in the repair request.
    """
    if not isinstance(obj, Mapping):
        raise ValueError("plan payload is not a JSON object")
    problems: list[str] = []
    for key in PLAN_SCHEMA["required"]:
        if key not in obj or obj.get(key) is None:
            problems.append(f"missing required key {key!r}")
    title = _str(obj.get("title")).strip()
    if "title" in obj and not title:
        problems.append("'title' must be a non-empty string")
    engine = _norm_engine(obj.get("engine"))
    if "engine" in obj and engine not in ENGINES:
        problems.append(f"'engine' is {obj.get('engine')!r}; allowed: {', '.join(ENGINES)}")
    files = _norm_files(obj["files"], problems) if obj.get("files") is not None else []
    contract = _norm_contract(obj["contract"], problems) if obj.get("contract") is not None else {}
    tasks = _norm_tasks(obj["tasks"], problems) if obj.get("tasks") is not None else []
    tests = (_norm_tests(obj["acceptance_tests"], int(min_tests), problems)
             if obj.get("acceptance_tests") is not None else [])
    constraints = _norm_constraints(obj.get("constraints"), problems)
    brief = _str(obj.get("design_brief_for_nemotron")).strip()
    if "design_brief_for_nemotron" in obj and not brief:
        problems.append("'design_brief_for_nemotron' must be a non-empty string")
    try:
        preview = _bool(obj.get("preview"), engine != "zip-only")
    except ValueError:
        problems.append("'preview' must be true or false")
        preview = engine != "zip-only"
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "title": title,
        "engine": engine,
        "preview": preview,
        "summary": _str(obj.get("summary")).strip(),
        "files": files,
        "contract": contract,
        "tasks": tasks,
        "acceptance_tests": tests,
        "constraints": constraints,
        "design_brief_for_nemotron": brief,
    }


def normalize_verdict(obj: Mapping) -> dict:
    """Validate a ``verdict`` payload strictly. Raises ``ValueError`` on any enum violation."""
    if not isinstance(obj, Mapping):
        raise ValueError("verdict payload is not a JSON object")
    problems: list[str] = []
    decision = _str(obj.get("decision")).strip().lower()
    if decision not in ("allow", "deny"):
        problems.append(f"'decision' must be 'allow' or 'deny', got {obj.get('decision')!r}")
    risk = _str(obj.get("risk")).strip().lower()
    if risk not in ("low", "medium", "high"):
        problems.append(f"'risk' must be 'low', 'medium' or 'high', got {obj.get('risk')!r}")
    reason = _str(obj.get("reason")).strip()
    if decision == "deny" and not reason:
        problems.append("a 'deny' verdict needs a 'reason' the engine can act on")
    if problems:
        raise ValueError("; ".join(problems))
    return {"decision": decision, "reason": reason, "risk": risk}


def normalize_review(obj: Mapping) -> dict:
    """Validate and normalise an ``emit_review`` payload (before the gate honesty guard)."""
    if not isinstance(obj, Mapping):
        raise ValueError("review payload is not a JSON object")
    problems: list[str] = []
    decision = _str(obj.get("decision")).strip().lower()
    if decision not in ("complete", "fix", "stop"):
        problems.append(f"'decision' must be 'complete', 'fix' or 'stop', got {obj.get('decision')!r}")
    fix_instructions = _str(obj.get("fix_instructions")).strip()
    if decision == "fix" and not fix_instructions:
        problems.append("a 'fix' decision needs concrete 'fix_instructions'")
    if problems:
        raise ValueError("; ".join(problems))
    suspected: list[str] = []
    for f in _str_list(obj.get("suspected_files")):
        try:
            path = normalize_rel_path(f)
        except ValueError as exc:
            log.info("kimi review: dropping suspected file %r (%s)", f, exc)
            continue
        if path not in suspected:
            suspected.append(path)
    return {"decision": decision, "reasons": _str_list(obj.get("reasons")), "fix_instructions": fix_instructions,
            "suspected_files": suspected}


def apply_gate_guard(review: dict, gate: Mapping | None) -> dict:
    """Honesty guard: ``complete`` is not allowed unless the measured gate says ``ok: True``."""
    out = dict(review)
    out["reasons"] = list(out.get("reasons") or [])
    out["coerced"] = False
    ok = isinstance(gate, Mapping) and gate.get("ok") is True
    if out.get("decision") == "complete" and not ok:
        out["decision"] = "fix"
        out["reasons"].append(GATE_NOT_PASSED_REASON)
        out["coerced"] = True
    return out


def truncate_strings(value: Any, limit: int = JUDGE_STRING_LIMIT) -> tuple[Any, bool]:
    """Deep-copy ``value`` cutting every string longer than ``limit`` (with an explicit marker).

    Returns ``(copy, truncated_anything)``.
    """
    truncated = False

    def walk(v: Any) -> Any:
        nonlocal truncated
        if isinstance(v, str):
            if len(v) > limit:
                truncated = True
                return v[:limit] + f"… [truncated {len(v) - limit} characters]"
            return v
        if isinstance(v, Mapping):
            return {str(k): walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        if v is None or isinstance(v, (bool, int, float)):
            return v
        return walk(str(v))

    return walk(value), truncated


_ENVELOPE_KEYS = ("arguments", "parameters", "input", "data", "result", "output")


def unwrap_payload(obj: Any, tool_name: str) -> Any:
    """Unwrap a single-key envelope such as ``{"plan": {...}}`` or ``{"arguments": {...}}``.

    Only an object with exactly one key, whose value is itself an object and
    whose key names the tool (``emit_plan``/``plan``) or a generic envelope,
    is unwrapped; anything else is returned unchanged.
    """
    if not isinstance(obj, Mapping) or len(obj) != 1:
        return obj
    (key, value), = obj.items()
    short = tool_name[5:] if tool_name.startswith("emit_") else tool_name
    if isinstance(value, Mapping) and str(key) in (tool_name, short, *_ENVELOPE_KEYS):
        return dict(value)
    return obj


# ----------------------------------------------------------------------------- attachments
def _is_image(att: Mapping) -> bool:
    mime = _str(att.get("mime") or att.get("content_type")).lower()
    return bool(att.get("image_data_url")) or mime.startswith("image/")


def _listing_of(att: Mapping) -> list[str] | None:
    listing = att.get("zip_listing")
    if listing is None:
        listing = att.get("listing")
    if listing is None:
        return None
    if isinstance(listing, (list, tuple)):
        return [_str(x.get("path") or x.get("name")) if isinstance(x, Mapping) else _str(x) for x in listing]
    return [ln for ln in _str(listing).splitlines() if ln.strip()]


def _valid_image_url(url: Any) -> bool:
    return isinstance(url, str) and (url.startswith("data:image/") or url.startswith("https://")
                                     or url.startswith("http://"))


def build_intake_content(prompt: str, attachments: Sequence[Mapping] | None, *,
                         import_attachment_id: str | None = None,
                         max_images: int = MAX_INTAKE_IMAGES) -> tuple[list[dict], str, str, bool]:
    """Build the intake user message and the prompt's attachment fields.

    Returns ``(content_parts, attachment_summary, imported_base_hint, has_import)``.
    The first part is the user's prompt; every image attachment carrying an
    ``image_data_url`` becomes an ``image_url`` part (at most ``max_images``),
    preceded by a short text label naming it. The summary lists every
    attachment with its name, MIME type, size, role and excerpt/ZIP listing,
    and says honestly which images were not shown.
    """
    atts = [a for a in (attachments or []) if isinstance(a, Mapping)]
    text = _str(prompt).strip() or "(the user gave no text prompt; rely on the attachments)"
    parts: list[dict] = [{"type": "text", "text": f"User request:\n{text}"}]
    lines: list[str] = []
    budget = EXCERPT_BUDGET
    shown_images = 0
    import_name: str | None = None
    for i, att in enumerate(atts):
        name = _str(att.get("name") or att.get("id") or f"attachment-{i + 1}").strip()
        mime = _str(att.get("mime") or att.get("content_type")).strip()
        role = _str(att.get("role")).strip()
        size = _human_size(att.get("size")) if att.get("size") is not None else ""
        meta = ", ".join(x for x in (mime, size) if x)
        head = f"- {name}" + (f" ({meta})" if meta else "") + (f" [role: {role}]" if role else "")
        is_import = role.lower() == "import" or (import_attachment_id is not None
                                                 and _str(att.get("id")) == _str(import_attachment_id))
        if is_import and import_name is None:
            import_name = name
            head += " - marked for import as the game base"
        if _is_image(att):
            url = att.get("image_data_url")
            if _valid_image_url(url) and len(url) <= MAX_IMAGE_DATA_URL_CHARS and shown_images < max_images:
                shown_images += 1
                parts.append({"type": "text", "text": f"Attached image {shown_images}: {name}"})
                parts.append({"type": "image_url", "image_url": {"url": url}})
                head += f": image, shown to you as attached image {shown_images}"
            elif _valid_image_url(url) and shown_images >= max_images:
                head += f": image NOT shown (limit of {max_images} images per request reached); do not describe it"
            elif _valid_image_url(url):
                head += ": image NOT shown (too large to send); do not describe it"
            else:
                head += ": image NOT included in this request; do not describe it"
        lines.append(head)
        excerpt = _str(att.get("text_excerpt") or att.get("excerpt"))
        if excerpt.strip():
            if budget > 0:
                cut = excerpt[:min(EXCERPT_CHARS, budget)]
                budget -= len(cut)
                more = len(excerpt) - len(cut)
                body = "\n".join("    " + ln for ln in cut.splitlines())
                lines.append("  excerpt:\n" + body + (f"\n    … [{more} more characters not shown]" if more > 0 else ""))
            else:
                lines.append("  excerpt: not shown (excerpt budget for this request exhausted)")
        listing = _listing_of(att)
        if listing:
            shown = listing[:LISTING_ITEMS]
            more = len(listing) - len(shown)
            lines.append(f"  zip listing ({len(listing)} entries): " + ", ".join(shown)
                         + (f" … (+{more} more)" if more > 0 else ""))
    if import_name is None and import_attachment_id:
        import_name = _str(import_attachment_id)
    has_import = import_name is not None
    summary = "\n".join(lines) if lines else "(no attachments)"
    hint = f"present: {import_name}" if has_import else "absent"
    return parts, summary, hint, has_import


def _image_data_url(shot: Any) -> str | None:
    """PNG/JPEG bytes (or an existing image data URL) -> data URL; ``None`` when unusable."""
    if isinstance(shot, str):
        return shot if shot.startswith("data:image/") and len(shot) <= MAX_IMAGE_DATA_URL_CHARS else None
    if not isinstance(shot, (bytes, bytearray, memoryview)):
        return None
    data = bytes(shot)
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    return url if len(url) <= MAX_IMAGE_DATA_URL_CHARS else None


# ----------------------------------------------------------------------------- conversation
class KimiConversation:
    """An ordered, thread-safe OpenAI-style message list for multi-turn Kimi calls.

    Kimi's preserved thinking requires the COMPLETE assistant message to be
    echoed back: :meth:`add_assistant` stores exactly what
    :attr:`ChatResult.message` gives (``content``, ``reasoning_content``,
    ``tool_calls``), deep-copied so later mutation by the caller cannot alter it.
    """

    def __init__(self, system: str | None = None):
        self._lock = threading.Lock()
        self._messages: list[dict] = []
        if system:
            self.add_system(system)

    def add_system(self, text: str) -> None:
        """Append a system message."""
        with self._lock:
            self._messages.append({"role": "system", "content": _str(text)})

    def add_user(self, content: str | list) -> None:
        """Append a user message (plain text or a list of content parts such as ``image_url``)."""
        if not isinstance(content, (str, list)):
            raise TypeError("user content must be a string or a list of content parts")
        with self._lock:
            self._messages.append({"role": "user", "content": copy.deepcopy(content)})

    def add_assistant(self, message: Mapping) -> None:
        """Append the complete assistant message (as returned in ``ChatResult.message``)."""
        if not isinstance(message, Mapping):
            raise TypeError("assistant message must be a dict (ChatResult.message)")
        msg = copy.deepcopy(dict(message))
        role = msg.setdefault("role", "assistant")
        if role != "assistant":
            raise ValueError(f"add_assistant expects role 'assistant', got {role!r}")
        with self._lock:
            self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str, name: str | None = None) -> None:
        """Append the result of one tool call (``role: tool``)."""
        msg: dict = {"role": "tool", "tool_call_id": _str(tool_call_id), "content": _str(content)}
        if name:
            msg["name"] = name
        with self._lock:
            self._messages.append(msg)

    def messages(self) -> list[dict]:
        """A deep copy of the messages, ready to send."""
        with self._lock:
            return copy.deepcopy(self._messages)

    def last(self) -> dict | None:
        """A copy of the most recent message, or ``None``."""
        with self._lock:
            return copy.deepcopy(self._messages[-1]) if self._messages else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


# ----------------------------------------------------------------------------- the role
class Kimi:
    """Planner, instant judge and reviewer. Constructed by ``kyora.app`` as ``Kimi(client, model, kimi_cfg, bus)``."""

    ROLE = ROLE
    MAX_TOKENS: dict[str, int] = {"intake": 16384, "plan": 32768, "judge": 8192, "review": 16384}
    TIMEOUT_S: dict[str, float] = {"judge": 240.0}

    def __init__(self, client: NIMClient, model: str, kimi_cfg, bus):
        if client is None:
            raise ValueError("Kimi needs a NIMClient")
        if not model:
            raise ValueError("Kimi needs a model id")
        self.client = client
        self.model = model
        self.cfg = kimi_cfg
        self.bus = bus
        self._lock = threading.Lock()
        self._last_metrics: dict[str, dict] = {}
        self._tool_mode = TOOL_MODES[0]

    # ------------------------------------------------------------------ settings
    @property
    def reasoning_effort(self) -> str:
        return str(getattr(self.cfg, "reasoning_effort", None) or "max")

    @property
    def judge_reasoning_effort(self) -> str:
        return str(getattr(self.cfg, "judge_reasoning_effort", None) or self.reasoning_effort)

    @property
    def effort_field(self) -> str:
        return str(getattr(self.cfg, "effort_field", None) or "top_level")

    @property
    def tool_mode(self) -> str:
        """The structured-output request shape this gateway accepted last: ``forced``, ``auto`` or ``none``."""
        with self._lock:
            return self._tool_mode

    def effort_extra(self, effort: str | None) -> dict:
        """Body fields carrying the reasoning effort, placed per ``kimi.effort_field``."""
        if effort is None:
            effort = self.reasoning_effort
        if not effort:
            return {}
        field = self.effort_field
        if field == "extra_body":
            # "extra_body" is the OpenAI SDK argument NVIDIA's samples use for template switches; the SDK
            # merges it into the top level of the body, so a literal "extra_body" key reaches no server.
            # The top-level field a server reads instead is the chat template's own keyword.
            return {"chat_template_kwargs": {"reasoning_effort": effort}}
        if field != "top_level":
            log.warning("kimi: unknown effort_field %r; sending reasoning_effort top-level", field)
        return {"reasoning_effort": effort}

    # ------------------------------------------------------------------ bookkeeping
    def _remember(self, purpose: str, res: ChatResult) -> None:
        with self._lock:
            self._last_metrics[purpose] = res.metrics.to_dict()

    def last_metrics(self, purpose: str) -> dict | None:
        """Measured metrics of the most recent call for ``purpose`` (``intake``, ``plan``, ``judge``, ``review`` ...)."""
        with self._lock:
            m = self._last_metrics.get(purpose)
            return dict(m) if m else None

    # ------------------------------------------------------------------ core call
    def think(self, conv: KimiConversation, *, tools: list[dict] | None = None, tool_choice: Any = None,
              effort: str | None = None, run_id: str | None = None, project_id: str | None = None,
              cancel: threading.Event | None = None, max_tool_rounds: int = 4,
              tool_impl: Mapping[str, Callable[[dict], str]] | None = None, purpose: str | None = None,
              max_tokens: int | None = None, timeout_s: float | None = None,
              publish_deltas: bool = True) -> ChatResult:
        """Send ``conv`` to Kimi (``temperature 1.0, top_p 1.0``) and run a bounded tool loop.

        Every answer's complete assistant message is appended to ``conv``. When
        the answer calls tools and ``tool_impl`` implements all of them, each is
        run with its parsed arguments, its string result is appended as a
        ``tool`` message and Kimi is called again. The loop stops and returns the
        last :class:`ChatResult` when the answer has no tool calls, when a called
        tool has no implementation (structured-output tools are read by the
        caller), or after ``max_tool_rounds`` executed rounds. A forced
        ``tool_choice`` applies to the first request only; later rounds use
        ``auto`` so a forced, implemented tool cannot loop by construction.
        """
        extra = self.effort_extra(effort)
        label = purpose or "think"
        rounds = 0
        choice = tool_choice
        while True:
            res = chat_with_events(self.client, self.bus, role=ROLE, model=self.model, run_id=run_id,
                                   project_id=project_id, purpose=label, publish_deltas=publish_deltas,
                                   messages=conv.messages(), tools=tools, tool_choice=choice if tools else None,
                                   max_tokens=max_tokens, extra=extra or None, cancel=cancel, timeout_s=timeout_s,
                                   temperature=1.0, top_p=1.0)
            self._remember(label, res)
            conv.add_assistant(res.message)
            if not res.tool_calls or not tool_impl:
                return res
            names = [(tc.get("function") or {}).get("name") or "" for tc in res.tool_calls]
            missing = [n for n in names if n not in tool_impl]
            if missing:
                return res
            if rounds >= max(0, int(max_tool_rounds)):
                log.info("kimi %s: tool loop stopped after %d rounds (pending calls: %s)", label, rounds, names)
                return res
            for tc, name in zip(res.tool_calls, names):
                args = tool_call_arguments(tc)
                try:
                    out = tool_impl[name](args)
                except Exception as exc:  # noqa: BLE001 - the model sees the failure and can react
                    log.warning("kimi %s: tool %s raised %s: %s", label, name, type(exc).__name__, exc)
                    out = f"error: tool {name} failed: {type(exc).__name__}: {exc}"
                if not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False, default=str)
                conv.add_tool_result(tc.get("id") or "", out)
            rounds += 1
            if isinstance(choice, Mapping) or choice == "required":
                choice = "auto"

    # ------------------------------------------------------------------ structured output
    def _think_tool(self, conv: KimiConversation, tool: dict, **kw: Any) -> ChatResult:
        """One structured-output request, degrading the request shape when the gateway rejects tools."""
        name = tool["function"]["name"]
        with self._lock:
            start = self._tool_mode
        modes = TOOL_MODES[TOOL_MODES.index(start):]
        for i, mode in enumerate(modes):
            tools = None if mode == "none" else [tool]
            choice = force_tool(name) if mode == "forced" else ("auto" if mode == "auto" else None)
            try:
                res = self.think(conv, tools=tools, tool_choice=choice, **kw)
            except NIMError as exc:
                if i == len(modes) - 1 or not is_tool_rejection(exc):
                    raise
                log.warning("kimi %s: gateway rejected the %s tool request (%s); retrying with %s",
                            kw.get("purpose"), mode, exc, modes[i + 1])
                continue
            if mode != start:
                with self._lock:
                    self._tool_mode = mode
                log.info("kimi: structured output now uses tool mode %r", mode)
            return res
        raise AssertionError("unreachable")  # pragma: no cover

    def _structured(self, tool: dict, *, system: str, user_content: str | list, purpose: str,
                    effort: str | None = None, validate: Callable[[dict], dict] | None = None,
                    max_tokens: int | None = None, run_id: str | None = None, project_id: str | None = None,
                    cancel: threading.Event | None = None, publish_deltas: bool = True,
                    timeout_s: float | None = None, repair: bool = True) -> tuple[dict, ChatResult]:
        """Ask for ``tool``'s payload, validate it, and repair once when it is missing or invalid.

        The tool is forced when the gateway accepts it; otherwise the request
        degrades to ``tool_choice: auto`` and then to a tool-less request whose
        JSON answer is extracted from the text. On a missing/invalid payload the
        model's full assistant message is kept in the conversation (answered by
        a ``tool`` message per tool call, as the chat protocol requires), followed
        by a user message describing the validation error and asking for the
        corrected JSON only. Raises ``ValueError`` when the repair fails too.
        """
        name = tool["function"]["name"]
        conv = KimiConversation(system)
        conv.add_user(user_content)
        kw = dict(effort=effort, run_id=run_id, project_id=project_id, cancel=cancel, purpose=purpose,
                  max_tokens=max_tokens, timeout_s=timeout_s, publish_deltas=publish_deltas)
        attempts = 2 if repair else 1
        error = ""
        res: ChatResult | None = None
        for attempt in range(attempts):
            if attempt > 0 and res is not None:
                for tc in res.tool_calls:
                    conv.add_tool_result(tc.get("id") or "", f"Rejected by Kyora validation: {error}")
                via = (f"Call {name} again with" if self.tool_mode != "none" else "Reply with ONLY")
                conv.add_user(f"Your {name} output was rejected by Kyora's validation: {error}\n"
                              f"{via} the corrected, complete JSON object. JSON only: no prose, no code fence.")
                log.info("kimi %s: requesting one repair (%s)", purpose, error)
            res = self._think_tool(conv, tool, **kw)
            obj = unwrap_payload(extract_structured(res, name), name)
            if obj is None:
                error = (f"no parsable {name} JSON object in the answer (finish_reason={res.finish_reason!r}, "
                         f"content_chars={len(res.content)})")
                continue
            try:
                return (validate(obj) if validate else obj), res
            except ValueError as exc:
                error = str(exc)
        suffix = " after one repair attempt" if repair else ""
        raise ValueError(f"Kimi {purpose}: invalid {name} payload{suffix}: {error}")

    # ------------------------------------------------------------------ intake
    def intake(self, prompt: str, attachments: list[dict] | None, run_id: str | None = None,
               project_id: str | None = None, cancel: threading.Event | None = None, *,
               import_attachment_id: str | None = None) -> dict:
        """Prompt + attachments -> ``intake.json`` content.

        Returns ``{goal, genre, platform, must_haves[], references[], risks[], imported_base, notes}``.
        Reference images are sent to Kimi as ``image_url`` parts (at most
        :data:`MAX_INTAKE_IMAGES`); documents/code contribute ``text_excerpt``,
        ZIPs ``zip_listing`` (or ``listing``). Raises ``NIMError`` on API failure
        and ``ValueError`` when no valid intake comes back after one repair.
        """
        parts, summary, hint, has_import = build_intake_content(prompt, attachments,
                                                                import_attachment_id=import_attachment_id)
        system = render_prompt("kimi_intake.md", attachment_summary=summary, imported_base_hint=hint)
        parts = parts + [{"type": "text", "text": "Analyse this request now and call emit_intake with the intake record."}]
        obj, _ = self._structured(INTAKE_TOOL, system=system, user_content=parts, purpose="intake",
                                  validate=lambda o: normalize_intake(o, has_import=has_import),
                                  max_tokens=self.MAX_TOKENS["intake"], run_id=run_id, project_id=project_id,
                                  cancel=cancel)
        return obj

    # ------------------------------------------------------------------ plan
    def plan(self, intake: dict, run_id: str | None = None, project_id: str | None = None,
             cancel: threading.Event | None = None, min_tests: int = 6, node_available: bool | None = None) -> dict:
        """Intake -> validated ``plan.json`` content (see :func:`normalize_plan`).

        ``node_available`` is what Kyora measured on the device that runs TEST
        (``find_node()``); ``False`` forbids kind ``node`` acceptance tests, which
        could never run there. One repair request is made when the first answer is
        missing or invalid; afterwards ``ValueError`` is raised with the problems.
        """
        min_tests = max(1, int(min_tests))
        system = render_prompt("kimi_plan.md", intake_json=json_for_prompt(intake or {}),
                               allowed_engines=", ".join(f'"{e}"' for e in ENGINES), min_tests=min_tests,
                               node_rule=NODE_RULES[node_available])
        user = (f"Write the plan for this intake now (at least {min_tests} acceptance tests, index.html in files"
                + ("; every acceptance test of kind \"browser\"" if node_available is False else "")
                + ") and call emit_plan with the complete plan.")

        def validate(o: Mapping) -> dict:
            out = normalize_plan(o, min_tests=min_tests)
            node_ids = [t["id"] for t in out["acceptance_tests"] if t["kind"] == "node"]
            if node_available is False and node_ids:
                raise ValueError(f"Node.js is not available where the tests run, so kind 'node' acceptance tests "
                                 f"can never run: change {', '.join(node_ids)} to kind 'browser'")
            return out
        obj, _ = self._structured(PLAN_TOOL, system=system, user_content=user, purpose="plan", validate=validate,
                                  max_tokens=self.MAX_TOKENS["plan"], run_id=run_id, project_id=project_id,
                                  cancel=cancel)
        return obj

    # ------------------------------------------------------------------ judge
    @staticmethod
    def _plan_files(ctx: Mapping, plan: Mapping) -> list[str]:
        raw = ctx.get("files")
        if raw is None:
            raw = plan.get("files")
        out: list[str] = []
        for item in raw or []:
            path = item.get("path") if isinstance(item, Mapping) else item
            if path:
                out.append(_str(path))
        return out

    def judge(self, ctx: dict, call: dict, run_id: str | None = None) -> dict:
        """Instant verdict on one code-engine tool call.

        Returns ``{decision: 'allow'|'deny', reason, risk: 'low'|'medium'|'high', source: 'kimi',
        judge_model, latency_ms}``. Raises on any failure (``NIMError``,
        ``ValueError``); :class:`~kyora.pipeline.judge.JudgeService` fails open
        and reports ``judge.unavailable`` itself. Deltas are not published
        (the judge runs once per tool call).
        """
        t0 = time.monotonic()
        ctx = ctx if isinstance(ctx, Mapping) else {}
        call = call if isinstance(call, Mapping) else {}
        plan = ctx.get("plan") if isinstance(ctx.get("plan"), Mapping) else {}
        system = render_prompt("kimi_judge.md", workspace=_str(ctx.get("workspace")) or "(unknown workspace)",
                               iteration=ctx.get("iteration", 0),
                               plan_title=_str(ctx.get("plan_title") or plan.get("title")) or "(untitled)")
        tool_input, truncated = truncate_strings(call.get("tool_input") or {})
        constraints = ctx.get("constraints")
        if constraints is None:
            constraints = plan.get("constraints") or {}
        test_ids = ctx.get("acceptance_test_ids")
        if test_ids is None:
            test_ids = [t.get("id") for t in plan.get("acceptance_tests") or [] if isinstance(t, Mapping) and t.get("id")]
        payload: dict = {"tool_name": _str(call.get("tool_name")), "tool_input": tool_input}
        if truncated:
            payload["tool_input_truncated"] = True
        if call.get("cwd"):
            payload["cwd"] = _str(call.get("cwd"))
        payload["plan_files"] = self._plan_files(ctx, plan)
        payload["constraints"] = constraints
        payload["acceptance_test_ids"] = list(test_ids or [])
        recent = ctx.get("recent_calls") if ctx.get("recent_calls") is not None else call.get("recent_calls")
        if recent:
            payload["recent_calls"], _ = truncate_strings(recent)
        user = ("Judge this pending tool call now and call verdict exactly once.\n\n"
                + json_for_prompt(payload, limit=48_000))
        verdict, _ = self._structured(VERDICT_TOOL, system=system, user_content=user, purpose="judge",
                                      effort=self.judge_reasoning_effort, validate=normalize_verdict,
                                      max_tokens=self.MAX_TOKENS["judge"], run_id=run_id,
                                      project_id=ctx.get("project_id"), publish_deltas=False,
                                      timeout_s=self.TIMEOUT_S.get("judge"))
        return {**verdict, "source": "kimi", "judge_model": self.model,
                "latency_ms": int((time.monotonic() - t0) * 1000)}

    # ------------------------------------------------------------------ review
    def review(self, run_summary: dict, gate: dict, screenshots: list[bytes] | None, run_id: str | None = None,
               project_id: str | None = None, cancel: threading.Event | None = None) -> dict:
        """Decide ``complete``/``fix``/``stop`` after a gate, looking at up to 4 screenshots.

        Returns ``{decision, reasons[], fix_instructions, suspected_files[], coerced}``.
        Honesty guard: when ``gate.ok`` is not ``True`` a ``complete`` answer is
        coerced to ``fix`` with the reason ``'gate not passed; complete is not allowed'``
        (``coerced: True``).
        """
        urls: list[str] = []
        for shot in list(screenshots or []):
            if len(urls) >= MAX_REVIEW_SCREENSHOTS:
                break
            url = _image_data_url(shot)
            if url:
                urls.append(url)
            else:
                log.info("kimi review: skipping an empty, unsupported or oversized screenshot")
        system = render_prompt("kimi_review.md", screenshot_count=len(urls),
                               run_summary_json=json_for_prompt(run_summary or {}),
                               gate_json=json_for_prompt(gate or {}))
        intro = f"Review this iteration now. {len(urls)} screenshot(s) from the browser test harness follow"
        parts: list[dict] = [{"type": "text", "text": intro + (":" if urls else ".")}]
        for i, url in enumerate(urls, 1):
            parts.append({"type": "text", "text": f"Screenshot {i}:"})
            parts.append({"type": "image_url", "image_url": {"url": url}})
        parts.append({"type": "text", "text": "Call emit_review with your decision."})
        obj, _ = self._structured(REVIEW_TOOL, system=system, user_content=parts, purpose="review",
                                  validate=normalize_review, max_tokens=self.MAX_TOKENS["review"], run_id=run_id,
                                  project_id=project_id, cancel=cancel)
        out = apply_gate_guard(obj, gate)
        if out["coerced"]:
            log.warning("kimi review answered 'complete' although the gate did not pass; coerced to 'fix'")
        return out


__all__ = [
    "Kimi", "KimiConversation", "ROLE", "ENGINES", "TEST_KINDS", "DEFAULT_CONSTRAINTS", "TOOL_MODES",
    "INTAKE_SCHEMA", "PLAN_SCHEMA", "VERDICT_SCHEMA", "REVIEW_SCHEMA",
    "INTAKE_TOOL", "PLAN_TOOL", "VERDICT_TOOL", "REVIEW_TOOL",
    "MAX_INTAKE_IMAGES", "MAX_REVIEW_SCREENSHOTS", "JUDGE_STRING_LIMIT", "GATE_NOT_PASSED_REASON",
    "normalize_rel_path", "normalize_intake", "normalize_plan", "normalize_verdict", "normalize_review",
    "apply_gate_guard", "truncate_strings", "build_intake_content", "unwrap_payload",
]
