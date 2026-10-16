"""Quality system: static checks, the Node runner, browser results and the gate.

Every number this module produces is measured from the generated workspace or
from results posted by a real browser (the in-browser harness under
``kyora/web/runner``). Nothing is estimated: a check that could not run is
``skip`` (not counted) or ``deferred`` (resolved later by the browser harness;
a required check still ``deferred`` at gate time counts as a failure).

Public API (see ``docs/architecture.md``):

* :class:`Check`, :class:`GateResult` - result records with ``to_dict()``.
* :class:`StaticChecker` - stdlib checks over a workspace (``run()``).
* :class:`NodeRunner` - ``node --check`` and ``node --test`` (TAP) when Node exists.
* :class:`BrowserResultsCollector` - receives harness posts, stores screenshots,
  lets the pipeline ``wait()`` for an iteration's results.
* :func:`merge_checks`, :class:`QualityGate`, :func:`find_node`,
  :func:`validate_manifest`, :func:`validate_browser_payload`.

Standard library only.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..util import procs
from ..util.fs import atomic_write_bytes, atomic_write_json, read_json, which
from ..util.ids import now_iso

log = logging.getLogger("kyora.quality")

STATUSES = ("pass", "fail", "skip", "deferred")
GROUPS = ("static", "browser", "node")
COUNTED_STATUSES = ("pass", "fail")
RESULT_STATUSES = ("pass", "fail", "skip")

MANIFEST_PATH = ".kyora/manifest.json"
BROWSER_TESTS_PATH = "tests/browser-tests.js"
LOGIC_TESTS_PATH = "tests/logic-tests.mjs"
DEFAULT_MAX_TOTAL_MB = 25
SKIP_DIRS = frozenset({".git", "node_modules"})
MAX_SCAN_FILES = 5000
MAX_SCAN_BYTES = 5 * 1024 * 1024
TEXT_SCAN_EXTS = frozenset({".html", ".htm", ".js", ".mjs", ".cjs", ".css"})
HTML_EXTS = frozenset({".html", ".htm"})
DEFERRED_DETAIL = "no syntax verification available"
DID_NOT_RUN_DETAIL = "did not run"

_ACCEPTANCE_ID_RE = re.compile(r"^([A-Za-z]{1,8}-\d+)(?=$|[\s:.,)\]-])")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def find_node() -> str | None:
    """Return the path of the ``node`` executable, or ``None`` when Node is not installed."""
    return which("node")


# ============================================================================ records
@dataclass
class Check:
    """One measured check.

    ``group`` is ``static`` | ``browser`` | ``node``; ``status`` is ``pass`` |
    ``fail`` | ``skip`` | ``deferred``. Only ``pass``/``fail`` are counted by the
    gate. ``runner`` names what produced the verdict (``python``, ``node --check``,
    ``browser:new Function`` ...).
    """

    id: str
    group: str
    name: str
    status: str
    required: bool = False
    detail: str = ""
    runner: str = ""

    def to_dict(self) -> dict:
        """JSON-ready dict with the dataclass fields."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Check":
        """Rebuild a check from :meth:`to_dict` output (e.g. ``iter-<n>/static.json`` on resume)."""
        return cls(id=str(d.get("id", "")), group=str(d.get("group", "static")), name=str(d.get("name", "")),
                   status=str(d.get("status", "skip")), required=bool(d.get("required", False)),
                   detail=str(d.get("detail") or ""), runner=str(d.get("runner") or ""))

    @property
    def counted(self) -> bool:
        """True when the gate counts this check (status pass or fail)."""
        return self.status in COUNTED_STATUSES


@dataclass
class GateResult:
    """Outcome of :meth:`QualityGate.evaluate`."""

    threshold: float
    passed: int
    total: int
    pass_rate: float
    ok: bool
    required_failures: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    missing_spec_tests: list[str] = field(default_factory=list)

    def to_dict(self, include_checks: bool = True) -> dict:
        """JSON-ready dict. ``pass_rate`` is floored to 4 decimals (never rounded up)."""
        out = {
            "threshold": self.threshold,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": math.floor(self.pass_rate * 10000) / 10000 if self.total else 0.0,
            "ok": self.ok,
            "required_failures": list(self.required_failures),
            "missing_spec_tests": list(self.missing_spec_tests),
        }
        if include_checks:
            out["checks"] = [c.to_dict() for c in self.checks]
        return out


# ============================================================================ helpers
def _plan_tests(plan: Mapping | None) -> list[dict]:
    tests = (plan or {}).get("acceptance_tests") or []
    out = []
    for t in tests:
        if isinstance(t, Mapping) and str(t.get("id") or "").strip():
            out.append({"id": str(t["id"]).strip(), "name": str(t.get("name") or t["id"]),
                        "kind": str(t.get("kind") or "browser").strip().lower(), "required": bool(t.get("required", False))})
    return out


def _plan_constraints(plan: Mapping | None) -> dict:
    c = (plan or {}).get("constraints")
    return dict(c) if isinstance(c, Mapping) else {}


def _rel_ok(rel: str) -> str | None:
    """Return a problem description when ``rel`` is not a safe workspace-relative path."""
    if not isinstance(rel, str) or not rel.strip():
        return "empty path"
    if "\\" in rel:
        return f"{rel!r} uses backslashes (use forward slashes)"
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return f"{rel!r} is absolute"
    if ".." in rel.split("/"):
        return f"{rel!r} escapes the workspace"
    return None


def _read_text(path: Path, limit: int = MAX_SCAN_BYTES) -> str:
    with open(path, "rb") as fh:
        data = fh.read(limit)
    return data.decode("utf-8-sig", errors="replace")


def _short(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _is_cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    try:
        if hasattr(cancel, "is_set"):
            return bool(cancel.is_set())
        if callable(cancel):
            return bool(cancel())
    except Exception as exc:  # noqa: BLE001 - a broken cancel token must not crash the wait
        log.warning("cancel token raised: %s", exc)
    return False


# ============================================================================ HTML
VOID_ELEMENTS = frozenset(
    "area base br col embed hr img input link meta param source track wbr keygen command basefont bgsound frame".split())
OPTIONAL_CLOSE = frozenset(
    "p li td tr th option optgroup thead tbody tfoot dt dd colgroup caption rp rt rb rtc html head body".split())
_P_CLOSERS = frozenset(
    ("address article aside blockquote details dialog div dl fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 "
     "header hgroup hr main menu nav ol p pre section table ul").split())
# open optional-close tag -> start tags that implicitly close it
_CLOSED_BY: dict[str, frozenset] = {
    "p": _P_CLOSERS,
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "option": frozenset({"option", "optgroup"}),
    "optgroup": frozenset({"optgroup"}),
    "tr": frozenset({"tr", "tbody", "thead", "tfoot"}),
    "td": frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"}),
    "th": frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"}),
    "thead": frozenset({"tbody", "tfoot"}),
    "tbody": frozenset({"tbody", "tfoot"}),
    "tfoot": frozenset({"tbody"}),
    "colgroup": frozenset({"colgroup", "thead", "tbody", "tfoot", "tr", "caption"}),
    "caption": frozenset({"colgroup", "thead", "tbody", "tfoot", "tr"}),
    "rp": frozenset({"rp", "rt", "rb", "rtc"}),
    "rt": frozenset({"rp", "rt", "rb", "rtc"}),
    "rb": frozenset({"rp", "rt", "rb", "rtc"}),
    "rtc": frozenset({"rb", "rtc"}),
    "head": frozenset({"body"}),
}


class WellFormedHTMLParser(HTMLParser):
    """Tag-stack HTML checker.

    Void elements never need closing, optional-close tags (``p``, ``li``, ``td`` ...)
    are closed implicitly the way browsers do, and a mismatched or unclosed
    element is reported with its line number. Also records ``src``/``href``
    attributes for the path-escape check and which tags were seen.
    """

    MAX_ERRORS = 10

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[tuple[int, str]] = []
        self.tags_seen: set[str] = set()
        self.links: list[tuple[int, str, str, str]] = []  # (line, tag, attr, value)

    def _err(self, line: int, msg: str) -> None:
        if len(self.errors) < self.MAX_ERRORS:
            self.errors.append((line, msg))

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags_seen.add(tag)
        line = self.getpos()[0]
        for name, value in attrs:
            if name in ("src", "href") and value is not None:
                self.links.append((line, tag, name, value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)
        while self.stack:
            top = self.stack[-1][0]
            if top in OPTIONAL_CLOSE and tag in _CLOSED_BY.get(top, ()):
                self.stack.pop()
                continue
            break
        if tag in VOID_ELEMENTS:
            return
        self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>, <img/>, and self-closed foreign elements (<path/> in inline SVG)
        self._record(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        line = self.getpos()[0]
        if tag in VOID_ELEMENTS:
            return
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                idx = i
                break
        if idx is None:
            if tag not in OPTIONAL_CLOSE:
                self._err(line, f"unexpected </{tag}> (no open <{tag}>)")
            return
        above = self.stack[idx + 1:]
        blocking = [(t, ln) for t, ln in above if t not in OPTIONAL_CLOSE]
        if blocking:
            t, ln = blocking[-1]
            self._err(line, f"mismatched </{tag}>: <{t}> opened at line {ln} is not closed")
        del self.stack[idx:]

    def finish(self) -> None:
        """Close the parser and report every non-optional element still open."""
        self.close()
        for tag, ln in self.stack:
            if tag not in OPTIONAL_CLOSE:
                self._err(ln, f"unclosed <{tag}> opened at line {ln}")
        self.stack.clear()


def check_html(text: str) -> WellFormedHTMLParser:
    """Parse ``text`` and return the finished parser (inspect ``.errors``, ``.tags_seen``, ``.links``)."""
    parser = WellFormedHTMLParser()
    try:
        parser.feed(text)
        parser.finish()
    except Exception as exc:  # noqa: BLE001 - html.parser rarely raises; report instead of crashing
        parser._err(parser.getpos()[0], f"parser error: {exc}")
    return parser


# ============================================================================ manifest
def validate_manifest(obj: Any, workspace: Path | None = None) -> list[str]:
    """Validate the game manifest (``.kyora/manifest.json``); return problems (empty = valid).

    Schema: ``entry`` str, ``engine`` str, ``scripts``/``modules``/``tests`` lists
    of workspace-relative paths (forward slashes); every listed file must exist
    when ``workspace`` is given.
    """
    if not isinstance(obj, Mapping):
        return ["manifest is not a JSON object"]
    problems: list[str] = []
    listed: list[str] = []
    entry = obj.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        problems.append("'entry' must be a non-empty string")
    else:
        listed.append(entry)
    engine = obj.get("engine")
    if not isinstance(engine, str) or not engine.strip():
        problems.append("'engine' must be a non-empty string")
    for key in ("scripts", "modules", "tests"):
        val = obj.get(key)
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            problems.append(f"'{key}' must be a list of strings")
            continue
        listed.extend(val)
    for rel in listed:
        bad = _rel_ok(rel)
        if bad:
            problems.append(bad)
            continue
        if workspace is not None and not (Path(workspace) / rel).is_file():
            problems.append(f"listed file {rel!r} does not exist")
    return problems


def _manifest_lists(obj: Any) -> tuple[list[str], list[str]]:
    if not isinstance(obj, Mapping):
        return [], []
    scripts = [s for s in obj.get("scripts") or [] if isinstance(s, str)] if isinstance(obj.get("scripts"), list) else []
    modules = [s for s in obj.get("modules") or [] if isinstance(s, str)] if isinstance(obj.get("modules"), list) else []
    return scripts, modules


# ============================================================================ static
_EXT_DEP_PATTERNS = [
    re.compile(r"""<\w+\b[^>]*?\bsrc\s*=\s*["']?((?:https?:)?//[^"'\s>]+)""", re.I),
    re.compile(r"""<link\b[^>]*?\bhref\s*=\s*["']?((?:https?:)?//[^"'\s>]+)""", re.I),
    re.compile(r"""\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?["']((?:https?:)?//[^"']+)["']"""),
    re.compile(r"""\bimport\s*\(\s*["'`]((?:https?:)?//[^"'`]+)["'`]"""),
    re.compile(r"""\bfetch\s*\(\s*["'`]((?:https?:)?//[^"'`]+)["'`]"""),
    re.compile(r"""@import\s+(?:url\(\s*)?["']?((?:https?:)?//[^"')\s;]+)""", re.I),
    re.compile(r"""\burl\(\s*["']?(https?://[^"')\s]+)""", re.I),
]
_IGNORED_LINK_PREFIXES = ("data:", "blob:", "#", "mailto:", "tel:", "javascript:", "about:")


class StaticChecker:
    """Standard-library checks over a generated game workspace.

    ``node`` is the path of a Node executable (``find_node()``) or ``None``; without
    Node the ``js_syntax`` checks are ``deferred`` and resolved by the browser
    harness (``new Function`` / ``import()``).
    """

    def __init__(self, workspace: Path, plan: Mapping | None = None, node: str | None = None):
        self.workspace = Path(workspace)
        self.plan = plan or {}
        self.node = node

    # ------------------------------------------------------------------ walk
    def _walk(self) -> tuple[list[str], int]:
        """Return (scan list of relative posix paths, total size in bytes excluding ``.git``)."""
        files: list[str] = []
        total = 0
        root = self.workspace
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            rel_dir = Path(dirpath).relative_to(root)
            in_skipped = any(part in SKIP_DIRS for part in rel_dir.parts)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                try:
                    if p.is_symlink():
                        continue
                    total += p.stat().st_size
                except OSError as exc:
                    log.debug("stat failed for %s: %s", p, exc)
                    continue
                if not in_skipped and len(files) < MAX_SCAN_FILES:
                    files.append((rel_dir / fn).as_posix())
        return files, total

    # ------------------------------------------------------------------ run
    def run(self) -> list[Check]:
        """Run every static check and return them in a stable order."""
        ws = self.workspace
        checks: list[Check] = []
        if not ws.is_dir():
            return [Check("file_exists:index.html", "static", "index.html exists", "fail", True,
                          f"workspace {ws} does not exist", "python")]
        files, total_bytes = self._walk()

        for rel, name in (("index.html", "Entry page index.html exists"),
                          (MANIFEST_PATH, "Game manifest .kyora/manifest.json exists"),
                          (BROWSER_TESTS_PATH, "Browser tests tests/browser-tests.js exist")):
            ok = (ws / rel).is_file()
            checks.append(Check(f"file_exists:{rel}", "static", name, "pass" if ok else "fail", True,
                                "found" if ok else "missing", "python"))

        manifest, manifest_check = self._manifest_check()
        checks.append(manifest_check)

        html_files = [f for f in files if Path(f).suffix.lower() in HTML_EXTS]
        parsed: dict[str, WellFormedHTMLParser] = {}
        checks.extend(self._html_checks(html_files, parsed))
        checks.extend(self._json_checks([f for f in files if f.lower().endswith(".json")]))
        checks.extend(self._js_syntax_checks(manifest, parsed))
        checks.append(self._external_deps_check(files))
        checks.append(self._size_check(total_bytes))
        checks.extend(self._tests_present_checks())
        checks.append(self._path_escape_check(html_files, parsed))
        return checks

    # ------------------------------------------------------------------ manifest
    def _manifest_check(self) -> tuple[Any, Check]:
        path = self.workspace / MANIFEST_PATH
        name = "Manifest is valid (entry, engine, scripts, modules, tests; listed files exist)"
        if not path.is_file():
            return None, Check("manifest_valid", "static", name, "fail", True, "manifest missing", "python")
        try:
            obj = json.loads(_read_text(path))
        except (ValueError, OSError) as exc:
            return None, Check("manifest_valid", "static", name, "fail", True, f"invalid JSON: {exc}", "python")
        problems = validate_manifest(obj, self.workspace)
        if problems:
            more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
            return obj, Check("manifest_valid", "static", name, "fail", True, "; ".join(problems[:3]) + more, "python")
        return obj, Check("manifest_valid", "static", name, "pass", True, "valid", "python")

    # ------------------------------------------------------------------ html
    def _html_checks(self, html_files: list[str], parsed: dict[str, WellFormedHTMLParser]) -> list[Check]:
        out: list[Check] = []
        targets = list(html_files)
        if "index.html" not in targets:
            out.append(Check("html_wellformed:index.html", "static", "index.html is well-formed", "skip", True,
                             "index.html missing (reported by file_exists)", "python"))
        else:
            targets.remove("index.html")
            targets.insert(0, "index.html")
        for rel in targets:
            required = rel == "index.html"
            cid = f"html_wellformed:{rel}"
            name = f"{rel} is well-formed"
            try:
                text = _read_text(self.workspace / rel)
            except OSError as exc:
                out.append(Check(cid, "static", name, "fail", required, f"unreadable: {exc}", "python"))
                continue
            p = check_html(text)
            parsed[rel] = p
            errors = list(p.errors)
            if required and not ({"canvas", "script"} & p.tags_seen):
                errors.append((1, "index.html contains neither <canvas> nor <script>"))
            if errors:
                line, msg = errors[0]
                more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                out.append(Check(cid, "static", name, "fail", required, f"line {line}: {msg}{more}", "python"))
            else:
                out.append(Check(cid, "static", name, "pass", required, "well-formed", "python"))
        return out

    # ------------------------------------------------------------------ json
    def _json_checks(self, json_files: list[str]) -> list[Check]:
        out: list[Check] = []
        for rel in json_files:
            cid = f"json_valid:{rel}"
            try:
                json.loads(_read_text(self.workspace / rel, limit=50 * 1024 * 1024))
                out.append(Check(cid, "static", f"{rel} is valid JSON", "pass", False, "valid", "python"))
            except json.JSONDecodeError as exc:
                out.append(Check(cid, "static", f"{rel} is valid JSON", "fail", False,
                                 f"line {exc.lineno} col {exc.colno}: {exc.msg}", "python"))
            except OSError as exc:
                out.append(Check(cid, "static", f"{rel} is valid JSON", "fail", False, f"unreadable: {exc}", "python"))
        return out

    # ------------------------------------------------------------------ js syntax
    def _script_files(self, manifest: Any, parsed: dict[str, WellFormedHTMLParser]) -> tuple[list[str], list[str]]:
        scripts, modules = _manifest_lists(manifest)
        if scripts or modules:
            return scripts, modules
        # No usable manifest: fall back to local <script src> of index.html.
        p = parsed.get("index.html")
        if not p:
            return [], []
        try:
            text = _read_text(self.workspace / "index.html")
        except OSError:
            return [], []
        for m in re.finditer(r"<script\b([^>]*)>", text, re.I):
            attrs = m.group(1)
            src = re.search(r"""\bsrc\s*=\s*["']?([^"'\s>]+)""", attrs, re.I)
            if not src:
                continue
            url = src.group(1).split("?", 1)[0].split("#", 1)[0]
            if url.startswith(("http:", "https:", "//", "data:", "/")) or ".." in url.split("/"):
                continue
            url = url[2:] if url.startswith("./") else url
            if re.search(r"""\btype\s*=\s*["']?module""", attrs, re.I):
                modules.append(url)
            else:
                scripts.append(url)
        return scripts, modules

    def _js_syntax_checks(self, manifest: Any, parsed: dict[str, WellFormedHTMLParser]) -> list[Check]:
        scripts, modules = self._script_files(manifest, parsed)
        files = list(dict.fromkeys(scripts + modules))
        if not files:
            return []
        return NodeRunner(self.node, self.workspace).syntax_check(files, modules=modules, group="static")

    # ------------------------------------------------------------------ external deps
    def find_external_refs(self, files: Sequence[str]) -> list[tuple[str, str]]:
        """Return ``(file, url)`` for every external reference found in html/js/css files."""
        found: list[tuple[str, str]] = []
        for rel in files:
            if Path(rel).suffix.lower() not in TEXT_SCAN_EXTS:
                continue
            try:
                text = _read_text(self.workspace / rel)
            except OSError as exc:
                log.debug("cannot read %s: %s", rel, exc)
                continue
            seen: set[str] = set()
            for pat in _EXT_DEP_PATTERNS:
                for m in pat.finditer(text):
                    url = m.group(1)
                    if url not in seen:
                        seen.add(url)
                        found.append((rel, url))
        return found

    def _external_deps_check(self, files: Sequence[str]) -> Check:
        refs = self.find_external_refs(files)
        forbid = bool(_plan_constraints(self.plan).get("no_external_deps", True))
        name = "No external dependencies" if forbid else "External dependencies (allowed by plan)"
        if not refs:
            return Check("external_deps", "static", name, "pass", False, "no external references", "python")
        listing = ", ".join(f"{f}: {u}" for f, u in refs[:5]) + (f" (+{len(refs) - 5} more)" if len(refs) > 5 else "")
        if forbid:
            return Check("external_deps", "static", name, "fail", False,
                         f"{len(refs)} external reference(s) but plan forbids them: {listing}", "python")
        return Check("external_deps", "static", name, "pass", False,
                     f"{len(refs)} external reference(s) allowed by plan: {listing}", "python")

    # ------------------------------------------------------------------ size
    def _size_check(self, total_bytes: int) -> Check:
        raw = _plan_constraints(self.plan).get("max_total_mb", DEFAULT_MAX_TOTAL_MB)
        try:
            limit_mb = float(raw)
            if not math.isfinite(limit_mb) or limit_mb <= 0:
                raise ValueError(raw)
        except (TypeError, ValueError):
            limit_mb = float(DEFAULT_MAX_TOTAL_MB)
        mb = total_bytes / (1024 * 1024)
        ok = total_bytes <= limit_mb * 1024 * 1024
        return Check("size_limit", "static", f"Total size <= {limit_mb:g} MB (excluding .git)", "pass" if ok else "fail",
                     False, f"{mb:.2f} MB of {limit_mb:g} MB", "python")

    # ------------------------------------------------------------------ tests present
    def _tests_present_checks(self) -> list[Check]:
        tests = [t for t in _plan_tests(self.plan) if t["kind"] == "browser"]
        if not tests:
            return []
        path = self.workspace / BROWSER_TESTS_PATH
        text = None
        if path.is_file():
            try:
                text = _read_text(path)
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
        out = []
        for t in tests:
            tid = t["id"]
            cid = f"tests_present:{tid}"
            name = f"Browser test {tid} is implemented"
            if text is None:
                out.append(Check(cid, "static", name, "fail", False, f"{BROWSER_TESTS_PATH} missing", "python"))
                continue
            present = re.search(r"(?<![\w-])" + re.escape(tid) + r"(?![\w-])", text) is not None
            out.append(Check(cid, "static", name, "pass" if present else "fail", False,
                             f"id found in {BROWSER_TESTS_PATH}" if present else f"id not found in {BROWSER_TESTS_PATH}",
                             "python"))
        return out

    # ------------------------------------------------------------------ path escape
    def _path_escape_check(self, html_files: list[str], parsed: dict[str, WellFormedHTMLParser]) -> Check:
        problems: list[str] = []
        for rel in html_files:
            p = parsed.get(rel)
            if p is None:
                continue
            for line, tag, attr, value in p.links:
                v = value.strip()
                if not v or v.lower().startswith(_IGNORED_LINK_PREFIXES) or re.match(r"^[a-z][a-z0-9+.-]*://", v, re.I) \
                        or v.startswith("//"):
                    continue
                if "../" in v or "..\\" in v or v == "..":
                    problems.append(f"{rel}:{line} <{tag} {attr}=\"{v}\"> climbs out with '../'")
                elif v.startswith("/"):
                    problems.append(f"{rel}:{line} <{tag} {attr}=\"{v}\"> is root-absolute (breaks under /preview/)")
        name = "No path escapes in src/href"
        if problems:
            more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
            return Check("no_path_escape", "static", name, "fail", False, "; ".join(problems[:3]) + more, "python")
        return Check("no_path_escape", "static", name, "pass", False, "all local references stay inside the game", "python")


# ============================================================================ node
_TAP_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<ok>not ok|ok)\s+(?P<num>\d+)(?:\s*-\s*(?P<rest>.*))?$")


def _tap_unescape(s: str) -> str:
    return s.replace("\\#", "#").replace("\\\\", "\\")


def _split_directive(rest: str) -> tuple[str, str]:
    """Split ``name # SKIP reason`` into (name, directive) honouring ``\\#`` escapes."""
    m = re.search(r"(?<!\\)#\s*(SKIP|TODO)\b.*$", rest, re.I)
    if m:
        return rest[: m.start()].rstrip(), m.group(1).upper()
    return rest.strip(), ""


def _yaml_field(lines: list[str], key: str) -> str | None:
    """Tiny extractor for ``key: value`` / ``key: |-`` blocks in node's TAP YAML diagnostics."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(key + ":"):
            continue
        indent = len(line) - len(line.lstrip())
        value = stripped[len(key) + 1:].strip()
        if value in ("|-", "|", ">-", ">"):
            block = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                block.append(nxt.strip())
            return "\n".join(block).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1].replace("''", "'")
        return value
    return None


def parse_tap(text: str) -> list[dict]:
    """Parse node's TAP output into leaf test records.

    Returns dicts ``{name, status, num, indent, error, duration_ms, type}``. Parents
    (suites or tests with subtests) are dropped unless they failed for a reason of
    their own (not merely ``subtestsFailed``).
    """
    entries: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _TAP_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group("indent").replace("\t", "    "))
        name, directive = _split_directive(m.group("rest") or "")
        name = _tap_unescape(name) or f"test {m.group('num')}"
        ok = m.group("ok") == "ok"
        yaml_lines: list[str] = []
        j = i + 1
        if j < len(lines) and lines[j].strip() == "---":
            j += 1
            while j < len(lines) and lines[j].strip() != "...":
                yaml_lines.append(lines[j])
                j += 1
            j += 1
        if directive in ("SKIP", "TODO"):
            status = "skip"
        else:
            status = "pass" if ok else "fail"
        dur = _yaml_field(yaml_lines, "duration_ms")
        try:
            duration = float(dur) if dur is not None else None
        except ValueError:
            duration = None
        entries.append({"name": name, "status": status, "num": int(m.group("num")), "indent": indent,
                        "error": _yaml_field(yaml_lines, "error") if not ok else None,
                        "failure_type": _yaml_field(yaml_lines, "failureType"),
                        "duration_ms": duration, "type": _yaml_field(yaml_lines, "type") or "test"})
        i = j if j > i + 1 else i + 1
    leaves: list[dict] = []
    for k, e in enumerate(entries):
        is_parent = k > 0 and entries[k - 1]["indent"] > e["indent"]
        if is_parent and not (e["status"] == "fail" and e["failure_type"] not in (None, "subtestsFailed")):
            continue
        leaves.append(e)
    return leaves


class NodeRunner:
    """Runs Node-based checks when a Node executable is available.

    ``syntax_check`` uses ``node --check`` on a temporary copy of each file (``.cjs``
    for classic scripts, ``.mjs`` for modules) so the result does not depend on a
    ``package.json`` ``type`` field. ``logic_tests`` runs ``node --test`` with the
    TAP reporter on ``tests/logic-tests.mjs``.
    """

    def __init__(self, node: str | None, workspace: Path, *, timeout_s: float = 120):
        self.node = node
        self.workspace = Path(workspace)
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        """True when a Node executable was given."""
        return bool(self.node)

    # ------------------------------------------------------------------ syntax
    def syntax_check(self, files: Iterable[str], modules: Iterable[str] = (), *, group: str = "node") -> list[Check]:
        """``js_syntax:<file>`` (required) per file; ``deferred`` without Node.

        A file is checked as an ES module when it is listed in ``modules`` or ends in ``.mjs``.
        """
        files = list(dict.fromkeys(files))
        module_set = set(modules)
        out: list[Check] = []
        tmpdir: tempfile.TemporaryDirectory | None = None
        try:
            for idx, rel in enumerate(files):
                cid = f"js_syntax:{rel}"
                name = f"JavaScript syntax of {rel}"
                bad = _rel_ok(rel)
                if bad:
                    out.append(Check(cid, group, name, "fail", True, bad, "python"))
                    continue
                src = self.workspace / rel
                if not src.is_file():
                    out.append(Check(cid, group, name, "fail", True, "file not found", "python"))
                    continue
                if not self.node:
                    out.append(Check(cid, group, name, "deferred", True,
                                     "node not available; verified later by the browser harness (new Function / import())",
                                     "none"))
                    continue
                is_module = rel in module_set or rel.lower().endswith(".mjs")
                if tmpdir is None:
                    tmpdir = tempfile.TemporaryDirectory(prefix="kyora-check-")
                tmp = Path(tmpdir.name) / f"{idx}_{re.sub(r'[^A-Za-z0-9_.-]', '_', Path(rel).stem)}{'.mjs' if is_module else '.cjs'}"
                try:
                    tmp.write_bytes(src.read_bytes())
                except OSError as exc:
                    out.append(Check(cid, group, name, "fail", True, f"unreadable: {exc}", "python"))
                    continue
                rc, _stdout, stderr = procs.run([self.node, "--check", str(tmp)], cwd=tmpdir.name, timeout=60)
                runner = "node --check" + (" (module)" if is_module else "")
                if rc == 0:
                    out.append(Check(cid, group, name, "pass", True, "ok", runner))
                elif rc == -9:
                    out.append(Check(cid, group, name, "fail", True, "node --check timed out", runner))
                else:
                    out.append(Check(cid, group, name, "fail", True, self._condense_error(stderr, str(tmp), rel), runner))
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()
        return out

    @staticmethod
    def _condense_error(stderr: str, tmp_path: str, rel: str) -> str:
        """Turn node's ``--check`` stderr into ``SyntaxError: ... (line N: code)``."""
        lines = [ln for ln in stderr.replace(tmp_path, rel).splitlines()]
        line_no = None
        code = None
        for k, ln in enumerate(lines):
            m = re.match(r"^" + re.escape(rel) + r":(\d+)\s*$", ln.strip())
            if m:
                line_no = m.group(1)
                if k + 1 < len(lines):
                    code = lines[k + 1].strip()
                break
        msg = next((ln.strip() for ln in lines if re.match(r"^\s*\w*Error\b.*:", ln)), None)
        if not msg:
            msg = _short(" ".join(ln.strip() for ln in lines if ln.strip() and not ln.startswith("Node.js")), 300) \
                or "node --check failed"
        where = f" (line {line_no}: {_short(code or '', 120)})" if line_no else ""
        return _short(msg + where, 600)

    # ------------------------------------------------------------------ logic tests
    def logic_tests(self) -> list[Check]:
        """Run ``tests/logic-tests.mjs`` with ``node --test`` and map TAP results to ``node`` checks."""
        test_file = self.workspace / LOGIC_TESTS_PATH
        if not self.node:
            return [Check("node:logic-tests", "node", "Node logic tests", "skip", False, "node not available", "none")]
        if not test_file.is_file():
            return [Check("node:logic-tests", "node", "Node logic tests", "skip", False,
                          f"{LOGIC_TESTS_PATH} not present", "node --test")]
        env = dict(os.environ)
        env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "NODE_DISABLE_COLORS": "1"})
        argv = [self.node, "--test", "--test-reporter=tap", LOGIC_TESTS_PATH]
        rc, stdout, stderr = procs.run(argv, cwd=self.workspace, env=env, timeout=self.timeout_s)
        if rc not in (0, -9) and "--test-reporter" in stderr and ("bad option" in stderr or "unknown" in stderr.lower()):
            # Node 18.0-18.14: TAP is the default reporter and the flag does not exist.
            rc, stdout, stderr = procs.run([self.node, "--test", LOGIC_TESTS_PATH], cwd=self.workspace, env=env,
                                           timeout=self.timeout_s)
        leaves = parse_tap(stdout)
        checks: list[Check] = []
        seen: dict[str, int] = {}
        for n, e in enumerate(leaves, start=1):
            m = _ACCEPTANCE_ID_RE.match(e["name"])
            cid = m.group(1) if m else f"lt-{n}"
            if cid in seen:
                seen[cid] += 1
                cid = f"{cid}#{seen[cid]}"
            else:
                seen[cid] = 1
            if e["status"] == "fail":
                detail = _short(e["error"] or "failed", 600)
            elif e["status"] == "skip":
                detail = "skipped/todo"
            else:
                detail = f"{e['duration_ms']:.1f} ms" if e["duration_ms"] is not None else "ok"
            checks.append(Check(cid, "node", e["name"], e["status"], False, detail, "node --test"))
        if rc == -9:
            checks.append(Check("node:logic-tests", "node", "Node logic tests finished", "fail", False,
                                f"timed out after {self.timeout_s:g} s", "node --test"))
        elif not leaves:
            tail = _short("\n".join((stderr or stdout).strip().splitlines()[-8:]), 600)
            checks.append(Check("node:logic-tests", "node", "Node logic tests", "fail", False,
                                f"no TAP results (exit {rc}): {tail}" if tail else f"no TAP results (exit {rc})",
                                "node --test"))
        return checks


# ============================================================================ browser results
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg)(?:;[^,;]*)*;base64,", re.I)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)) and math.isfinite(v)


def validate_browser_payload(payload: Any) -> list[str]:
    """Validate a harness result message (``docs/architecture.md``); return problems (empty = valid)."""
    if not isinstance(payload, Mapping):
        return ["payload is not a JSON object"]
    problems: list[str] = []
    if payload.get("type") != "kyora:test-result":
        problems.append("'type' must be 'kyora:test-result'")
    if "iteration" in payload and payload["iteration"] is not None and not (_is_int(payload["iteration"]) and payload["iteration"] >= 0):
        problems.append("'iteration' must be a non-negative integer")
    if "run_id" in payload and payload["run_id"] is not None and not isinstance(payload["run_id"], str):
        problems.append("'run_id' must be a string")
    results = payload.get("results")
    if not isinstance(results, list):
        problems.append("'results' must be a list")
    else:
        if len(results) > BrowserResultsCollector.MAX_RESULTS:
            problems.append(f"too many results ({len(results)} > {BrowserResultsCollector.MAX_RESULTS})")
        for i, r in enumerate(results[: BrowserResultsCollector.MAX_RESULTS]):
            if not isinstance(r, Mapping):
                problems.append(f"results[{i}] is not an object")
                continue
            if not isinstance(r.get("id"), str) or not r["id"].strip():
                problems.append(f"results[{i}].id must be a non-empty string")
            if "name" in r and r["name"] is not None and not isinstance(r["name"], str):
                problems.append(f"results[{i}].name must be a string")
            if r.get("status") not in RESULT_STATUSES:
                problems.append(f"results[{i}].status must be one of pass/fail/skip")
            if r.get("ms") is not None and not (_is_num(r["ms"]) and r["ms"] >= 0):
                problems.append(f"results[{i}].ms must be a non-negative number or null")
            if r.get("error") is not None and not isinstance(r["error"], str):
                problems.append(f"results[{i}].error must be a string or null")
            if len(problems) > 20:
                break
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        problems.append("'summary' must be an object")
    else:
        for key in ("passed", "failed", "skipped", "total"):
            if not (_is_int(summary.get(key)) and summary[key] >= 0):
                problems.append(f"summary.{key} must be a non-negative integer")
    syntax = payload.get("syntax_checks", [])
    if not isinstance(syntax, list):
        problems.append("'syntax_checks' must be a list")
    else:
        for i, s in enumerate(syntax[:500]):
            if not isinstance(s, Mapping) or not isinstance(s.get("file"), str) or not s["file"].strip():
                problems.append(f"syntax_checks[{i}] must be an object with a non-empty 'file'")
            elif not isinstance(s.get("ok"), bool):
                problems.append(f"syntax_checks[{i}].ok must be a boolean")
            elif s.get("error") is not None and not isinstance(s["error"], str):
                problems.append(f"syntax_checks[{i}].error must be a string or null")
    shots = payload.get("screenshots", [])
    if not isinstance(shots, list):
        problems.append("'screenshots' must be a list")
    else:
        for i, s in enumerate(shots[:50]):
            if not isinstance(s, Mapping) or not isinstance(s.get("name"), str):
                problems.append(f"screenshots[{i}] must be an object with a 'name'")
            elif s.get("data_url") is not None and not isinstance(s["data_url"], str):
                problems.append(f"screenshots[{i}].data_url must be a string or null")
    env = payload.get("env")
    if env is not None and not isinstance(env, Mapping):
        problems.append("'env' must be an object")
    return problems


def decode_screenshot(data_url: str, max_bytes: int) -> tuple[bytes, str]:
    """Decode a PNG/JPEG ``data:`` URL; return ``(bytes, extension)`` or raise ``ValueError``."""
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        raise ValueError("not a base64 PNG/JPEG data URL")
    b64 = data_url[m.end():]
    if len(b64) > (max_bytes * 4) // 3 + 8:
        raise ValueError(f"screenshot larger than {max_bytes // (1024 * 1024)} MB")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64: {exc}") from None
    if len(raw) > max_bytes:
        raise ValueError(f"screenshot larger than {max_bytes // (1024 * 1024)} MB")
    kind = m.group(1).lower()
    if kind == "png":
        if not raw.startswith(_PNG_MAGIC):
            raise ValueError("data is not a PNG image")
        return raw, ".png"
    if not raw.startswith(_JPEG_MAGIC):
        raise ValueError("data is not a JPEG image")
    return raw, ".jpg"


def _safe_name(name: str, idx: int, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "").strip("-")[:64] or f"shot-{idx + 1}"
    cand = base
    k = 2
    while cand.lower() in used:
        cand = f"{base}-{k}"
        k += 1
    used.add(cand.lower())
    return cand


class BrowserResultsCollector:
    """Receives browser-harness results for runs and hands them to the waiting pipeline.

    Thread-safe (one ``threading.Condition``). For each run the pipeline calls
    :meth:`expect` with the iteration and run directory, then :meth:`wait`.
    :meth:`submit` validates a posted payload, writes screenshots to
    ``<run_dir>/iter-<n>/screenshots/`` and the payload (data URLs replaced by
    paths) to ``<run_dir>/iter-<n>/browser.json``. A submission that arrives after
    ``wait`` gave up is still accepted for the latest expected iteration, so a
    resumed run picks it up.
    """

    MAX_SCREENSHOTS = 8
    MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
    MAX_RESULTS = 5000
    MAX_RUNS = 128
    POLL_S = 0.25

    def __init__(self, home: Path):
        self.home = Path(home)
        self._cond = threading.Condition()
        self._expected: "OrderedDict[str, dict]" = OrderedDict()
        self._results: "OrderedDict[str, dict[int, dict]]" = OrderedDict()
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------ registration
    def expect(self, run_id: str, iteration: int, run_dir: Path | None = None) -> None:
        """Announce that results for ``run_id``/``iteration`` are awaited (stored under ``run_dir``)."""
        with self._cond:
            entry = self._expected.get(run_id)
            if entry is None:
                entry = {"iterations": set(), "latest": int(iteration), "run_dir": None}
                self._expected[run_id] = entry
            entry["iterations"].add(int(iteration))
            entry["latest"] = max(entry["latest"], int(iteration)) if entry["iterations"] else int(iteration)
            if run_dir is not None:
                entry["run_dir"] = Path(run_dir)
            self._expected.move_to_end(run_id)
            while len(self._expected) > self.MAX_RUNS:
                old, _ = self._expected.popitem(last=False)
                self._results.pop(old, None)
                self._errors.pop(old, None)
            self._cond.notify_all()

    def forget(self, run_id: str) -> None:
        """Drop in-memory state for a finished run (files on disk are kept)."""
        with self._cond:
            self._expected.pop(run_id, None)
            self._results.pop(run_id, None)
            self._errors.pop(run_id, None)

    def last_error(self, run_id: str) -> str | None:
        """Reason the most recent submission for ``run_id`` was rejected (``None`` when accepted)."""
        with self._cond:
            return self._errors.get(run_id)

    def _run_dir(self, run_id: str) -> Path | None:
        entry = self._expected.get(run_id)
        if entry and entry.get("run_dir"):
            return entry["run_dir"]
        if not _SAFE_RUN_ID_RE.match(run_id or ""):
            return None
        projects = self.home / "projects"
        try:
            for cand in projects.glob(f"*/runs/{run_id}"):
                if cand.is_dir():
                    return cand
        except OSError as exc:
            log.debug("run dir discovery failed: %s", exc)
        return None

    # ------------------------------------------------------------------ submit
    def submit(self, run_id: str, payload: Any) -> bool:
        """Validate and store a harness result. Returns False (see :meth:`last_error`) when rejected."""
        if not isinstance(run_id, str) or not _SAFE_RUN_ID_RE.match(run_id):
            log.warning("browser result rejected: invalid run id %r", run_id)
            return False
        problems = validate_browser_payload(payload)
        if not problems and payload.get("run_id") not in (None, "", run_id):
            problems.append(f"payload run_id {payload.get('run_id')!r} does not match {run_id!r}")
        with self._cond:
            if not problems:
                entry = self._expected.get(run_id)
                piter = payload.get("iteration")
                if entry is not None:
                    target = piter if piter is not None else entry["latest"]
                    if target != entry["latest"]:
                        problems.append(f"iteration {target} is not the latest expected iteration ({entry['latest']})")
                else:
                    target = piter if piter is not None else 0
            if problems:
                self._errors[run_id] = "; ".join(problems[:5])
                log.warning("browser result for %s rejected: %s", run_id, self._errors[run_id])
                return False
            run_dir = self._run_dir(run_id)
        stored = self._sanitize(run_id, int(target), payload, run_dir)
        if run_dir is not None:
            try:
                atomic_write_json(run_dir / f"iter-{int(target)}" / "browser.json", stored)
            except OSError as exc:
                log.error("cannot write browser.json for %s: %s", run_id, exc)
        with self._cond:
            self._results.setdefault(run_id, {})[int(target)] = stored
            self._results.move_to_end(run_id)
            while len(self._results) > self.MAX_RUNS:
                self._results.popitem(last=False)
            self._errors.pop(run_id, None)
            self._cond.notify_all()
        return True

    def _sanitize(self, run_id: str, iteration: int, payload: Mapping, run_dir: Path | None) -> dict:
        results = []
        for r in payload.get("results") or []:
            results.append({"id": str(r["id"]).strip()[:200], "name": _short(str(r.get("name") or r["id"]), 500),
                            "status": r["status"], "ms": r.get("ms"),
                            "error": _short(r["error"], 4000) if r.get("error") else None})
        summary = {k: int(payload["summary"][k]) for k in ("passed", "failed", "skipped", "total")}
        syntax = []
        for s in payload.get("syntax_checks") or []:
            syntax.append({"file": str(s["file"])[:300], "ok": bool(s["ok"]),
                           "error": _short(s["error"], 2000) if s.get("error") else None,
                           "method": str(s.get("method") or "")[:40]})
        shots = self._store_screenshots(iteration, payload.get("screenshots") or [], run_dir)
        env = payload.get("env") if isinstance(payload.get("env"), Mapping) else {}
        return {"type": "kyora:test-result", "run_id": run_id, "iteration": iteration,
                "ref": str(payload.get("ref") or "")[:100], "results": results, "summary": summary,
                "syntax_checks": syntax, "screenshots": shots, "env": json.loads(json.dumps(env, default=str))
                if env else {}, "received_at": now_iso()}

    def _store_screenshots(self, iteration: int, shots: list, run_dir: Path | None) -> list[dict]:
        out: list[dict] = []
        used: set[str] = set()
        shot_dir = run_dir / f"iter-{iteration}" / "screenshots" if run_dir is not None else None
        if shot_dir is not None and shot_dir.is_dir():
            for old in shot_dir.iterdir():
                if old.is_file() and old.suffix.lower() in (".png", ".jpg"):
                    try:
                        old.unlink()
                    except OSError as exc:
                        log.warning("cannot remove old screenshot %s: %s", old, exc)
        for idx, s in enumerate(shots):
            name = str(s.get("name") or f"shot-{idx + 1}")[:100]
            rec: dict = {"name": name, "path": None}
            if idx >= self.MAX_SCREENSHOTS:
                rec["error"] = f"dropped: more than {self.MAX_SCREENSHOTS} screenshots"
                out.append(rec)
                continue
            data_url = s.get("data_url")
            if not data_url:
                rec["error"] = "no image (no canvas or tainted canvas)"
                out.append(rec)
                continue
            try:
                raw, ext = decode_screenshot(data_url, self.MAX_SCREENSHOT_BYTES)
            except ValueError as exc:
                rec["error"] = str(exc)
                out.append(rec)
                continue
            rec.update({"mime": "image/png" if ext == ".png" else "image/jpeg", "bytes": len(raw)})
            if shot_dir is None:
                rec["error"] = "not stored: run directory unknown"
                out.append(rec)
                continue
            fname = _safe_name(name, idx, used) + ext
            try:
                atomic_write_bytes(shot_dir / fname, raw)
                rec["path"] = f"iter-{iteration}/screenshots/{fname}"
            except OSError as exc:
                log.error("cannot write screenshot %s: %s", fname, exc)
                rec["error"] = f"write failed: {exc}"
            out.append(rec)
        return out

    # ------------------------------------------------------------------ read
    def _load_disk(self, run_id: str, iteration: int) -> dict | None:
        run_dir = self._run_dir(run_id)
        if run_dir is None:
            return None
        data = None
        try:
            data = read_json(run_dir / f"iter-{iteration}" / "browser.json")
        except (OSError, ValueError) as exc:
            log.warning("cannot read stored browser.json for %s/%s: %s", run_id, iteration, exc)
        return data if isinstance(data, dict) and data.get("type") == "kyora:test-result" else None

    def wait(self, run_id: str, iteration: int, timeout_s: float, cancel: Any = None) -> dict | None:
        """Block until results for ``run_id``/``iteration`` arrive; ``None`` on timeout or cancel.

        ``cancel`` may be a ``threading.Event`` (``is_set()``) or a callable returning True.
        Results already stored (in memory or in ``iter-<n>/browser.json``) return immediately.
        """
        iteration = int(iteration)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._cond:
            res = self._results.get(run_id, {}).get(iteration)
            if res is None:
                res = self._load_disk(run_id, iteration)
                if res is not None:
                    self._results.setdefault(run_id, {})[iteration] = res
            while res is None:
                if _is_cancelled(cancel):
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(self.POLL_S, remaining))
                res = self._results.get(run_id, {}).get(iteration)
            return res

    def latest(self, run_id: str) -> dict | None:
        """Most recent stored result for ``run_id`` (highest iteration), from memory or disk."""
        with self._cond:
            mem = self._results.get(run_id)
            if mem:
                return mem[max(mem)]
            run_dir = self._run_dir(run_id)
        if run_dir is None or not run_dir.is_dir():
            return None
        best: tuple[int, dict] | None = None
        for d in run_dir.glob("iter-*"):
            m = re.match(r"^iter-(\d+)$", d.name)
            if not m:
                continue
            data = self._load_disk(run_id, int(m.group(1)))
            if data is not None and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), data)
        return best[1] if best else None

    def screenshots(self, run_id: str, iteration: int) -> list[Path]:
        """Paths of the stored screenshots for ``run_id``/``iteration`` (payload order)."""
        with self._cond:
            res = self._results.get(run_id, {}).get(int(iteration))
            run_dir = self._run_dir(run_id)
        if run_dir is None:
            return []
        if res is None:
            res = self._load_disk(run_id, int(iteration))
        paths: list[Path] = []
        if res:
            for s in res.get("screenshots") or []:
                rel = s.get("path") if isinstance(s, Mapping) else None
                if rel and not _rel_ok(rel) and (run_dir / rel).is_file():
                    paths.append(run_dir / rel)
            if paths:
                return paths
        shot_dir = run_dir / f"iter-{int(iteration)}" / "screenshots"
        if shot_dir.is_dir():
            paths = sorted(p for p in shot_dir.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg"))
        return paths

    # ------------------------------------------------------------------ checks
    @staticmethod
    def to_checks(payload: Mapping | None) -> list[Check]:
        """One ``browser`` check per result id plus required ``js_syntax:<file>`` per syntax check."""
        if not payload:
            return []
        out: list[Check] = []
        seen: dict[str, int] = {}
        for r in payload.get("results") or []:
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            if rid in seen:
                seen[rid] += 1
                rid = f"{rid}#{seen[rid]}"
            else:
                seen[rid] = 1
            status = r.get("status") if r.get("status") in RESULT_STATUSES else "fail"
            if status == "fail":
                detail = _short(r.get("error") or "failed", 600)
            elif status == "skip":
                detail = _short(r.get("error") or "skipped", 300)
            else:
                ms = r.get("ms")
                detail = f"{ms:.0f} ms" if _is_num(ms) else "ok"
            out.append(Check(rid, "browser", str(r.get("name") or rid), status, False, detail, "browser"))
        if not out:
            out.append(Check("browser:no-tests", "browser", "Browser tests ran", "fail", False,
                             "the browser harness reported no test results", "browser"))
        for s in payload.get("syntax_checks") or []:
            f = str(s.get("file") or "").strip()
            if not f:
                continue
            method = str(s.get("method") or "browser")
            ok = bool(s.get("ok"))
            out.append(Check(f"js_syntax:{f}", "browser", f"JavaScript syntax of {f}", "pass" if ok else "fail", True,
                             "ok" if ok else _short(s.get("error") or "syntax check failed", 600), f"browser:{method}"))
        return out


# ============================================================================ merge + gate
# Ids produced by StaticChecker that a game's own test results must never replace.
_STATIC_ONLY_PREFIXES = ("file_exists:", "manifest_valid", "html_wellformed:", "json_valid:", "external_deps",
                         "size_limit", "tests_present:", "no_path_escape")


def merge_checks(static: Iterable[Check], node: Iterable[Check] = (), browser: Iterable[Check] = ()) -> list[Check]:
    """Merge check lists by id, keeping first-seen order.

    Node checks override static checks with the same id and browser checks override
    both - so a ``deferred`` ``js_syntax:<file>`` is replaced by the browser's
    ``js_syntax:<file>`` when the harness verified that file. A runner result whose id
    collides with one of the static checker's own checks (``manifest_valid``,
    ``file_exists:...`` ...) cannot mask it: it is kept as ``<group>:<id>`` instead.
    """
    merged: "OrderedDict[str, Check]" = OrderedDict()
    for group in (static, node, browser):
        for c in group or ():
            prev = merged.get(c.id)
            if (prev is not None and prev.group == "static" and c.group != "static"
                    and c.id.startswith(_STATIC_ONLY_PREFIXES)):
                alias = f"{c.group}:{c.id}"
                merged[alias] = Check(alias, c.group, c.name, c.status, c.required, c.detail, c.runner)
                continue
            merged[c.id] = c
    return list(merged.values())


class QualityGate:
    """Applies the measured pass-rate threshold and the hard requirements to a set of checks.

    ``ok`` iff at least one check was counted, ``passed/total >= cfg.min_pass_rate``,
    no required check failed (a required check still ``deferred`` or ``skip`` counts
    as failed) and every plan acceptance test id (kind browser or node) was reported
    with a pass/fail result by the browser or Node runner: a plan acceptance test
    that was skipped (TAP ``# SKIP`` / ``# TODO``, harness ``skip``) did not run and
    counts as a failure. Plan tests marked ``required: true`` make their checks
    required.
    """

    def __init__(self, cfg: Any, plan: Mapping | None = None):
        if isinstance(cfg, (int, float)) and not isinstance(cfg, bool):
            self.threshold = float(cfg)
        else:
            self.threshold = float(getattr(cfg, "min_pass_rate", 0.94))
        self.plan = plan or {}

    def evaluate(self, checks: Iterable[Check]) -> GateResult:
        """Compute the :class:`GateResult` for ``checks`` (inputs are not mutated)."""
        tests = [t for t in _plan_tests(self.plan) if t["kind"] in ("browser", "node")]
        required_ids = {t["id"].lower() for t in tests if t["required"]}
        plan_ids = {t["id"].lower() for t in tests}
        work: list[Check] = []
        for c in checks:
            c = Check(**asdict(c))
            is_test = c.group in ("browser", "node")
            if is_test and c.id.lower() in required_ids:
                c.required = True
            if c.status == "deferred" and c.required:
                c.status = "fail"
                c.detail = DEFERRED_DETAIL + (f" ({c.detail})" if c.detail else "")
            elif c.status in ("skip", "deferred") and (c.required or (is_test and c.id.lower() in plan_ids)):
                # a skipped/TODO required check or plan acceptance test never ran: that is a failure
                c.status = "fail"
                c.detail = DID_NOT_RUN_DETAIL + (f" ({c.detail})" if c.detail else "")
            work.append(c)
        # an acceptance id is reported only by a pass/fail result of the browser or Node runner
        reported = {c.id.lower() for c in work if c.group in ("browser", "node") and c.status in COUNTED_STATUSES}
        missing: list[str] = []
        for t in tests:
            if t["id"].lower() not in reported:
                missing.append(t["id"])
                work.append(Check(f"missing:{t['id']}", t["kind"], f"Acceptance test {t['id']} reported", "fail",
                                  t["required"], f"plan acceptance test {t['id']} ({t['name']}) was not reported by the "
                                                 f"{t['kind']} runner", "gate"))
        counted = [c for c in work if c.status in COUNTED_STATUSES]
        total = len(counted)
        passed = sum(1 for c in counted if c.status == "pass")
        pass_rate = passed / total if total else 0.0
        required_failures = [c.id for c in work if c.required and c.status == "fail"]
        ok = total > 0 and pass_rate + 1e-9 >= self.threshold and not required_failures and not missing
        return GateResult(threshold=self.threshold, passed=passed, total=total, pass_rate=pass_rate, ok=ok,
                          required_failures=required_failures, checks=work, missing_spec_tests=missing)


__all__ = [
    "Check", "GateResult", "StaticChecker", "NodeRunner", "BrowserResultsCollector", "QualityGate", "merge_checks",
    "find_node", "validate_manifest", "validate_browser_payload", "decode_screenshot", "parse_tap", "check_html",
    "WellFormedHTMLParser",
]
