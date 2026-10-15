#!/usr/bin/env python3
"""Heuristic PowerShell lint for Kyora's launchers (standard library only).

Usage
-----
  python scripts/ps1_lint.py [FILE.ps1 ...]      (default: launchers/install.ps1 launchers/kyora.ps1)

Exit status 0 when every file is clean, 1 with one ``file:line:col: message`` per problem otherwise.

This is not a PowerShell parser. It tokenizes just enough of the language to check what breaks
launchers in practice, on machines where no ``pwsh`` is available to parse them:

* ``{}``, ``()`` and ``[]`` are balanced outside strings and comments. Line comments (``#`` at the
  start of a token), block comments (``<# ... #>``), single-quoted strings (``''`` escape), double-quoted
  strings (backtick escapes, ``""``, nested ``$( ... )`` subexpressions that may contain strings),
  braced variables (``${env:ProgramFiles(x86)}``) and here-strings are skipped. A here-string header
  (``@"`` / ``@'``) must end its line and its terminator (``"@`` / ``'@``) must start a line; unpaired
  headers or terminators are reported.
* No tab characters; one line-ending style per file (all LF or all CRLF).
* Non-ASCII characters require a UTF-8 BOM (Windows PowerShell 5.1 reads BOM-less scripts as ANSI).
* No ``Invoke-Expression`` / ``iex`` anywhere outside comments, except a single
  ``irm https://claude.ai/install.ps1 | iex`` line (Anthropic's official Claude Code installer) that is
  documented by a comment mentioning "official" within the five lines above it.
* The first statement is the ``param(...)`` block (comment-based help, ``#requires`` and attributes such
  as ``[CmdletBinding()]`` may precede it).
* No PowerShell-7-only operators (``&&``, ``||``, ``??``, ``?.``) in code, so the scripts also run on
  Windows PowerShell 5.1.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FILES = ("launchers/install.ps1", "launchers/kyora.ps1")
SINGLE_QUOTES = "'‘’‚‛"  # PowerShell accepts typographic quotes too
DOUBLE_QUOTES = '"“”„'
OPENERS = {"(": ")", "{": "}", "[": "]"}
CLOSERS = {v: k for k, v in OPENERS.items()}
COMMENT_START_AFTER = set(" \t\r\n;(){}[],|=&")
CODE, COMMENT, STRING = 0, 1, 2
IEX_RE = re.compile(r"(?i)(?<![\w$-])(invoke-expression|iex)(?![\w-])")
OFFICIAL_INSTALLER_RE = re.compile(r"(?i)\birm\s+https://claude\.ai/install\.ps1\s*\|\s*iex\b")
PS7_OPERATORS_RE = re.compile(r"&&|\|\||\?\?|\?\.")
PARAM_RE = re.compile(r"(?i)param\s*\(")


@dataclass(frozen=True)
class Problem:
    """One lint finding (1-based line and column)."""

    name: str
    line: int
    col: int
    message: str

    def __str__(self) -> str:
        return f"{self.name}:{self.line}:{self.col}: {self.message}"


class _Pos:
    """Index -> (line, col) conversion."""

    def __init__(self, text: str) -> None:
        self.starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    def __call__(self, index: int) -> tuple[int, int]:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= index:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, index - self.starts[lo] + 1


def _header_ends_line(s: str, j: int) -> bool:
    """True when only spaces/tabs follow position ``j`` up to the end of the line."""
    k = j
    while k < len(s) and s[k] in " \t":
        k += 1
    return k >= len(s) or s[k] in "\r\n"


def scan(text: str, name: str = "<text>") -> tuple[list[Problem], list[int]]:
    """Tokenize ``text``; return ``(problems, kinds)`` where ``kinds[i]`` is CODE, COMMENT or STRING."""
    s = text
    n = len(s)
    pos = _Pos(s)
    kinds = [CODE] * n
    problems: list[Problem] = []
    brackets: list[tuple[str, int, bool]] = []  # (opener, index, closes-a-string-subexpression)
    modes: list[tuple[str, int]] = [("code", 0)]  # ("code"|"dq"|"dqhere", start index)

    def problem(index: int, msg: str) -> None:
        line, col = pos(index)
        problems.append(Problem(name, line, col, msg))

    def mark(a: int, b: int, kind: int) -> None:
        for k in range(a, min(b, n)):
            kinds[k] = kind

    i = 0
    while i < n:
        mode = modes[-1][0]
        c = s[i]
        if mode == "code":
            at_line_start = i == 0 or s[i - 1] == "\n"
            if at_line_start and s.startswith(('"@', "'@"), i):
                problem(i, f"here-string terminator {s[i:i + 2]} without a matching header")
                mark(i, i + 2, STRING)
                i += 2
                continue
            if s.startswith("<#", i):
                end = s.find("#>", i + 2)
                if end < 0:
                    problem(i, "unterminated block comment <#")
                    mark(i, n, COMMENT)
                    break
                mark(i, end + 2, COMMENT)
                i = end + 2
                continue
            if c == "#" and (i == 0 or s[i - 1] in COMMENT_START_AFTER):
                end = s.find("\n", i)
                end = n if end < 0 else end
                mark(i, end, COMMENT)
                i = end
                continue
            if c == "`":  # escape / line continuation
                i += 2
                continue
            if c == "@" and i + 1 < n and s[i + 1] in SINGLE_QUOTES + DOUBLE_QUOTES:
                q = s[i + 1]
                if not _header_ends_line(s, i + 2):
                    problem(i, f"here-string header @{q} must be the last thing on its line")
                    mark(i, i + 2, STRING)
                    i += 2
                    continue
                nl = s.find("\n", i)
                body = n if nl < 0 else nl + 1
                if q in SINGLE_QUOTES:
                    m = re.compile(r"(?m)^['‘’‚‛]@").search(s, body)
                    if not m:
                        problem(i, "unterminated here-string @' (terminator '@ must start a line)")
                        mark(i, n, STRING)
                        break
                    mark(i, m.end(), STRING)
                    i = m.end()
                else:
                    mark(i, body, STRING)
                    modes.append(("dqhere", i))
                    i = body
                    # an empty here-string closes on the very next line
                    if s.startswith(('"@', "“@", "”@"), i):
                        mark(i, i + 2, STRING)
                        modes.pop()
                        i += 2
                continue
            if c in SINGLE_QUOTES:
                j = i + 1
                while True:
                    if j >= n:
                        problem(i, "unterminated single-quoted string")
                        break
                    if s[j] in SINGLE_QUOTES:
                        if j + 1 < n and s[j + 1] in SINGLE_QUOTES:
                            j += 2
                            continue
                        break
                    j += 1
                mark(i, j + 1, STRING)
                i = j + 1
                continue
            if c in DOUBLE_QUOTES:
                mark(i, i + 1, STRING)
                modes.append(("dq", i))
                i += 1
                continue
            if c == "$" and s.startswith("${", i):
                j = i + 2
                while j < n and s[j] != "}":
                    j += 2 if s[j] == "`" else 1
                if j >= n:
                    problem(i, "unterminated braced variable ${...}")
                mark(i, j + 1, STRING)
                i = j + 1
                continue
            if c in OPENERS:
                brackets.append((c, i, False))
                i += 1
                continue
            if c in CLOSERS:
                if not brackets:
                    problem(i, f"unmatched closing {c!r}")
                else:
                    opener, at, closes_sub = brackets.pop()
                    if OPENERS[opener] != c:
                        line, col = pos(at)
                        problem(i, f"{c!r} closes {opener!r} opened at {line}:{col}")
                    if closes_sub:
                        modes.pop()  # back into the enclosing double-quoted string
                i += 1
                continue
            i += 1
            continue
        # inside a double-quoted string or here-string
        kinds[i] = STRING
        if c == "`":
            mark(i, i + 2, STRING)
            i += 2
            continue
        if c == "$" and s.startswith("$(", i):
            brackets.append(("(", i + 1, True))
            modes.append(("code", i))
            kinds[i] = CODE
            kinds[i + 1] = CODE
            i += 2
            continue
        if mode == "dq" and c in DOUBLE_QUOTES:
            if i + 1 < n and s[i + 1] in DOUBLE_QUOTES:
                mark(i, i + 2, STRING)
                i += 2
                continue
            modes.pop()
            i += 1
            continue
        if mode == "dqhere" and c == "\n" and s.startswith(('"@', "“@", "”@"), i + 1):
            mark(i, i + 3, STRING)
            modes.pop()
            i += 3
            continue
        i += 1

    for mode, start in reversed(modes[1:]):
        if mode == "dq":
            problem(start, "unterminated double-quoted string")
        elif mode == "dqhere":
            problem(start, 'unterminated here-string @" (terminator "@ must start a line)')
        elif mode == "code":
            problem(start, "unterminated $( subexpression inside a string")
    for opener, at, closes_sub in brackets:
        if not closes_sub:
            problem(at, f"unclosed {opener!r}")
    return problems, kinds


def _blank(text: str, kinds: list[int], drop: set[int]) -> str:
    """Replace characters of the given kinds by spaces (newlines kept so line numbers stay valid)."""
    return "".join(" " if kinds[i] in drop and ch not in "\r\n" else ch for i, ch in enumerate(text))


def _first_statement(code: str) -> int:
    """Index of the first statement after leading whitespace and ``[Attribute(...)]`` blocks."""
    i, n = 0, len(code)
    while True:
        while i < n and code[i].isspace():
            i += 1
        if i < n and code[i] == "[":
            depth = 0
            while i < n:
                if code[i] == "[":
                    depth += 1
                elif code[i] == "]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            continue
        return i


def lint_text(data: str | bytes, name: str = "<text>") -> list[Problem]:
    """Lint PowerShell source; ``data`` may be the raw file bytes (BOM and encoding are checked)."""
    problems: list[Problem] = []
    has_bom = False
    if isinstance(data, bytes):
        has_bom = data.startswith(b"\xef\xbb\xbf")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return [Problem(name, 1, 1, f"not valid UTF-8: {exc}")]
    else:
        has_bom = data.startswith("﻿")
        text = data.lstrip("﻿")
    pos = _Pos(text)

    for idx, ch in enumerate(text):
        if ord(ch) > 127 and not has_bom:
            line, col = pos(idx)
            problems.append(Problem(name, line, col, f"non-ASCII character {ch!r} in a file without a UTF-8 BOM "
                                                     "(Windows PowerShell 5.1 would read it as ANSI)"))
            break
    tab_lines = sorted({pos(i)[0] for i, ch in enumerate(text) if ch == "\t"})
    if tab_lines:
        problems.append(Problem(name, tab_lines[0], 1, f"tab character(s) on line(s) {tab_lines[:10]}; use spaces"))
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    lone_cr = text.count("\r") - crlf
    if lone_cr or (crlf and lf):
        problems.append(Problem(name, 1, 1, f"mixed line endings: {crlf} CRLF, {lf} LF, {lone_cr} bare CR"))

    scan_problems, kinds = scan(text, name)
    problems.extend(scan_problems)
    no_comments = _blank(text, kinds, {COMMENT})
    code_only = _blank(text, kinds, {COMMENT, STRING})
    lines = text.splitlines()
    nc_lines = no_comments.splitlines()

    allowed = 0
    for m in IEX_RE.finditer(no_comments):
        line, col = pos(m.start())
        line_text = nc_lines[line - 1] if line - 1 < len(nc_lines) else ""
        if OFFICIAL_INSTALLER_RE.search(line_text):
            context = lines[max(0, line - 6):line - 1]
            documented = any("#" in ln and "official" in ln.lower() for ln in context)
            allowed += 1
            if not documented:
                problems.append(Problem(name, line, col, "the official-installer 'irm ... | iex' line needs a comment "
                                                         "mentioning the official installer within 5 lines above"))
            if allowed > 1:
                problems.append(Problem(name, line, col, "more than one official-installer iex line"))
            continue
        problems.append(Problem(name, line, col, f"{m.group(1)} is not allowed (only the documented "
                                                 "'irm https://claude.ai/install.ps1 | iex' line is)"))

    for m in PS7_OPERATORS_RE.finditer(code_only):
        line, col = pos(m.start())
        problems.append(Problem(name, line, col, f"operator {m.group(0)!r} needs PowerShell 7; the launchers must "
                                                 "also run on Windows PowerShell 5.1"))

    first = _first_statement(code_only)
    if not PARAM_RE.match(code_only, first):
        line, col = pos(min(first, max(len(text) - 1, 0)))
        snippet = code_only[first:first + 30].strip().splitlines()[0] if code_only[first:].strip() else "<end of file>"
        problems.append(Problem(name, line, col, f"the param() block must be the first statement (found {snippet!r})"))

    problems.sort(key=lambda p: (p.line, p.col, p.message))
    return problems


def lint_file(path: Path | str) -> list[Problem]:
    """Lint one ``.ps1`` file."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        return [Problem(str(path), 1, 1, f"cannot read: {exc}")]
    return lint_text(data, str(path))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Heuristic PowerShell lint for Kyora's launchers.")
    ap.add_argument("files", nargs="*", help="files to lint (default: launchers/install.ps1 launchers/kyora.ps1)")
    args = ap.parse_args(argv)
    files = [Path(f) for f in args.files] or [REPO / f for f in DEFAULT_FILES]
    total = 0
    for f in files:
        problems = lint_file(f)
        total += len(problems)
        for p in problems:
            print(p)
        if not problems:
            lines = f.read_text(encoding="utf-8-sig").count("\n")
            print(f"OK: {f} ({lines} lines)")
    if total:
        print(f"{total} problem(s) found")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
