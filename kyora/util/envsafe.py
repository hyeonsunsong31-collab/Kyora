"""Secret-free child environments and redaction of known secrets.

:func:`safe_env` builds the environment of commands a model asks for (the
agent engines' ``bash`` tool, the Node logic tests) from an ALLOWLIST of the
variables a shell, Git Bash, node and npm need on Windows, macOS, Linux,
Termux and iSH; nothing that looks like a secret passes, even when allowlisted.

:func:`register_secret` / :func:`redact` keep a small thread-safe registry of
configured keys and tokens; :func:`redact` replaces every registered value and
the value of every secret-looking variable of this process with
``[redacted <label>]`` before text reaches a model, an event or a journal.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Mapping

# Variable NAMES that hold secrets: keys, tokens, passwords, credentials, auth sockets, DSNs, ...
SECRET_ENV_RE = re.compile(
    r"API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|(?:^|_)KEYS?(?:$|_)|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|(?:^|_)PASS(?:$|_)"
    r"|(?:^|_)PAT(?:$|_)|AUTH(?!OR)|CREDENTIAL|COOKIE|(?:^|_)DSN(?:$|_)|CONNECTION_?STRING|WEBHOOK",
    re.IGNORECASE)
# VALUES that are secrets whatever the name: URLs with a password (DATABASE_URL=postgres://u:pw@h) and
# well-known key prefixes
_SECRET_VALUE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@"
    r"|^(?:sk-|nvapi-|ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|hf_|xox[abprs]-|AKIA)[A-Za-z0-9_\-]{8,}")
# What a POSIX shell (also Git Bash on Windows), node and npm need; compared upper-case because Windows
# names are case-insensitive (os.environ spells them upper-case there)
SAFE_ENV_NAMES = frozenset(n.upper() for n in (
    "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
    "TMPDIR", "TEMP", "TMP", "PWD",
    "SystemRoot", "WINDIR", "COMSPEC", "SystemDrive", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "CommonProgramFiles",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "MSYSTEM",
    "PREFIX", "NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH", "PYTHONIOENCODING", "PYTHONUTF8",
    "NO_COLOR", "FORCE_COLOR", "CI",
    # npm/node behind a proxy or a private CA keep working; a proxy URL with a password is dropped by value
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
))
SAFE_ENV_PREFIXES = ("LC_", "ANDROID_", "TERMUX_")
MIN_SECRET_LEN = 8  # shorter values ("1", "true") would redact ordinary text

_lock = threading.Lock()
_secrets: dict[str, str] = {}  # value -> label
_pattern_cache: tuple[tuple, re.Pattern | None] = ((), None)


def is_secret_name(name: str) -> bool:
    """True for a variable name that holds a secret (``OPENAI_KEY``, ``GITHUB_PAT``, ``SSH_AUTH_SOCK``...)."""
    return bool(SECRET_ENV_RE.search(str(name)))


def looks_secret(name: str, value: str | None = None) -> bool:
    """True when the name is secret-looking or the value is a credential URL / a well-known key."""
    return is_secret_name(name) or (isinstance(value, str) and bool(_SECRET_VALUE_RE.search(value.strip())))


def _allowed(name: str, value: str) -> bool:
    up = name.upper()
    if up == "LD_PRELOAD":
        return "termux-exec" in value  # Termux rewrites shebangs (#!/usr/bin/env node) through it
    return up in SAFE_ENV_NAMES or up.startswith(SAFE_ENV_PREFIXES)


def safe_env(extra: Mapping | None = None, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted part of ``environ`` (default ``os.environ``) plus ``extra``, never a secret.

    ``extra`` values are set as given (``None`` removes the variable); a secret-looking
    name or value is dropped from both.
    """
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if _allowed(k, v) and not looks_secret(k, v)}
    for k, v in (extra or {}).items():
        k = str(k)
        if v is None:
            env.pop(k, None)
        elif not looks_secret(k, str(v)):
            env[k] = str(v)
    return env


# --------------------------------------------------------------------------- redaction
# placeholder keys local OpenAI-compatible servers accept; replacing them would mangle ordinary text
PLACEHOLDER_KEYS = frozenset({"password", "not-needed", "notneeded", "sk-no-key-required", "no-key", "none", "null",
                              "empty", "dummy", "changeme", "placeholder", "your-api-key", "api-key", "secret"})


def register_secret(label: str, value: str | None) -> None:
    """Remember ``value`` (at least 8 characters) so :func:`redact` replaces it with ``[redacted <label>]``."""
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) < MIN_SECRET_LEN or value.casefold() in PLACEHOLDER_KEYS:
        return
    if len(value) < 16 and not _SECRET_VALUE_RE.search(value):
        return  # "password", "not-needed": a local endpoint's placeholder, and an ordinary word in a game's code
    with _lock:
        _secrets[value] = str(label or "secret")


def clear_secrets() -> None:
    """Forget every registered value (tests)."""
    with _lock:
        _secrets.clear()


def _pattern() -> tuple[re.Pattern | None, dict[str, str]]:
    global _pattern_cache
    labels: dict[str, str] = {}
    for name, value in list(os.environ.items()):
        value = (value or "").strip()
        if len(value) >= MIN_SECRET_LEN and looks_secret(name, value):
            labels.setdefault(value, name)
    with _lock:
        labels.update(_secrets)  # a configured key keeps its config label
        key = tuple(sorted(labels.items()))
        if key != _pattern_cache[0]:
            # longest first: a value that contains another is replaced whole
            alts = sorted(labels, key=len, reverse=True)
            _pattern_cache = (key, re.compile("|".join(re.escape(v) for v in alts)) if alts else None)
        return _pattern_cache[1], labels


def redact(text: str) -> str:
    """``text`` with every registered secret and every secret-looking env value replaced by ``[redacted <label>]``."""
    if not isinstance(text, str) or len(text) < MIN_SECRET_LEN:
        return text
    pattern, labels = _pattern()
    if pattern is None:
        return text
    return pattern.sub(lambda m: f"[redacted {labels.get(m.group(0), 'secret')}]", text)


__all__ = ["SECRET_ENV_RE", "SAFE_ENV_NAMES", "SAFE_ENV_PREFIXES", "MIN_SECRET_LEN", "is_secret_name",
           "looks_secret", "safe_env", "PLACEHOLDER_KEYS", "register_secret", "clear_secrets", "redact"]
