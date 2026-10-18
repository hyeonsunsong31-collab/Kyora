"""Shared types for model clients and code engines.

A *code engine* implements the game (Claude Code CLI, the optional Anthropic
Messages-API engine, Kyora's own agent engine on an OpenAI-compatible endpoint,
or a remote Kyora Hub). Engines never fabricate output: if none is available the
pipeline reports BLOCKED. :func:`classify_failure` and :func:`reset_time` tell an
account failure (usage limit, login, unreachable service, retired model) from a
task failure, so the pipeline can continue on the next engine or resume later.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol

log = logging.getLogger("kyora.engines")


@dataclass
class Metrics:
    """Measured, never estimated unless ``tokens_source`` says so."""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tps: float | None = None
    tokens_source: str = "none"  # "usage" | "estimated_chunks" | "none"

    def to_dict(self) -> dict:
        return {"ttft_ms": self.ttft_ms, "total_ms": self.total_ms, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "tps": self.tps, "tokens_source": self.tokens_source}


@dataclass
class EngineJob:
    run_id: str
    workspace: Path
    prompt: str
    system_prompt: str
    max_turns: int
    model: str
    fallback_models: list[str]
    effort: str
    resume_session_id: str | None = None
    judge_context: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    kind: str = "build"  # "build" | "fix"
    project_id: str | None = None


@dataclass
class EngineEvent:
    type: str  # engine.session|engine.model_resolved|engine.text|engine.thinking|engine.tool_use|engine.tool_result|engine.result|engine.error|engine.stderr|engine.compat_retry|stage.progress
    data: dict = field(default_factory=dict)


class EngineSink(Protocol):
    def on_event(self, ev: EngineEvent) -> None: ...


class BusSink:
    """EngineSink that forwards engine events to the EventBus for a run."""

    def __init__(self, bus, run_id: str, project_id: str | None = None, extra: dict | None = None):
        self.bus = bus
        self.run_id = run_id
        self.project_id = project_id
        self.extra = extra or {}

    def on_event(self, ev: EngineEvent) -> None:
        data = dict(ev.data)
        data.update(self.extra)
        self.bus.publish(ev.type, data, run_id=self.run_id, project_id=self.project_id)


@dataclass
class EngineResult:
    ok: bool
    backend: str
    requested_model: str
    active_model: str | None = None
    fallback_used: bool = False
    session_id: str | None = None
    num_turns: int | None = None
    duration_ms: int = 0
    cost_usd: float | None = None
    result_text: str = ""
    error: str | None = None
    permission_denials: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "backend": self.backend, "requested_model": self.requested_model,
                "active_model": self.active_model, "fallback_used": self.fallback_used, "session_id": self.session_id,
                "num_turns": self.num_turns, "duration_ms": self.duration_ms, "cost_usd": self.cost_usd,
                "result_text": self.result_text[:4000], "error": self.error,
                "permission_denials": self.permission_denials}


@dataclass
class Availability:
    available: bool
    backend: str
    reason: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"available": self.available, "backend": self.backend, "reason": self.reason, "details": self.details}


class CodeEngine(ABC):
    name: str = "abstract"

    @abstractmethod
    def availability(self) -> Availability: ...

    @abstractmethod
    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult: ...


# ----------------------------------------------------------------------------- engine selection
# settings.engines.order names -> attribute of the composition root (kyora.app.App)
ENGINE_APP_ATTRS: dict[str, str] = {"hub": "hub_client", "claude_code": "claude_engine",
                                    "anthropic_api": "anthropic_engine", "openai_agent": "agent_engine"}
DEFAULT_ENGINE_ORDER: tuple[str, ...] = ("hub", "claude_code", "anthropic_api", "openai_agent")


def engine_order(app) -> list[tuple[str, CodeEngine]]:
    """``(name, engine)`` of every engine ``app`` has, in ``settings.engines.order`` (availability not checked).

    The hub client only counts while ``hub.url`` is set. An app without an
    ``engines`` setting (tests, older configs) uses :data:`DEFAULT_ENGINE_ORDER`.
    """
    cfg = getattr(app, "config", None)
    order = list(getattr(getattr(cfg, "engines", None), "order", None) or DEFAULT_ENGINE_ORDER)
    out: list[tuple[str, CodeEngine]] = []
    for name in dict.fromkeys(str(n) for n in order):
        attr = ENGINE_APP_ATTRS.get(name)
        eng = getattr(app, attr, None) if attr else None
        if eng is None:
            continue
        if name == "hub" and not str(getattr(getattr(cfg, "hub", None), "url", "") or ""):
            continue
        out.append((name, eng))
    return out


def _available_engines(app, exclude: Iterable[Any] = ()) -> Iterator[CodeEngine]:
    names = {x for x in exclude if isinstance(x, str)}
    objs = {id(x) for x in exclude if not isinstance(x, str)}
    for name, eng in engine_order(app):
        if name in names or str(getattr(eng, "name", "")) in names or id(eng) in objs:
            continue
        try:
            if eng.availability().available:
                yield eng
        except Exception as exc:  # noqa: BLE001 - one broken engine must not hide the others
            log.warning("availability of engine %s failed: %s", name, exc)


def engine_candidates(app, exclude: Iterable[Any] = ()) -> list[CodeEngine]:
    """Every *available* engine of ``app`` in ``settings.engines.order``, without those in ``exclude``.

    ``exclude`` holds engine names (``"claude_code"``) or engine objects, e.g. the
    engines a BUILD/FIX already tried before it fails over to the next one.
    """
    return list(_available_engines(app, exclude))


def select_engine(app) -> CodeEngine | None:
    """The first available engine in ``settings.engines.order`` (default: hub, claude_code, anthropic_api, openai_agent).

    ``app`` is the composition root (kyora.app.App). Returns None when nothing
    can run code; callers must surface BLOCKED, never fake output.
    """
    return next(_available_engines(app), None)


# ----------------------------------------------------------------------------- account failures
# What failed when an engine result is not ok, as far as it matters for *switching engines*: "auth" (login /
# key refused), "quota" (usage or session limit, credits, subscription, rate limit), "unavailable" (cannot
# reach / overloaded / server error) and "model" (the model is gone or not served). None means a task
# failure (max turns, a timeout of the job, a refusal, malformed output, judge denials, cancel): another
# engine would not do better, so the pipeline's normal rules apply.
FAILURE_KINDS: tuple[str, ...] = ("auth", "quota", "unavailable", "model")
_TASK_FAILURE_RE = re.compile(
    r"error_max_turns|\bmax(?:imum)?[ _-]turns\b|wall timeout|idle timeout|\bjob timeout|"
    r"exceeded (?:the )?(?:\w+[ _])?timeout|hub job \S+ timed out|\brefusal\b|malformed|unparseable|"
    r"denied by (?:the )?(?:kyora )?judge|judge denied|max_tokens reached|output token (?:limit|maximum)|"
    r"context (?:window|length|limit)|context_length_exceeded|maximum context length|prompt is too long",
    re.IGNORECASE)
_AUTH_RE = re.compile(
    r"not logged in|please run /login|claude auth login|invalid[ _-]api[ _-]?key|invalid x-api-key|"
    r"incorrect api key|authentication_error|authentication failed|oauth token (?:has )?expired|"
    r"token (?:has )?expired|invalid bearer token|permission_error|authorization failed|\bunauthori[sz]ed\b|"
    r"rejected the (?:api )?(?:key|token)|api key (?:is )?(?:not set|missing|revoked)|key (?:was )?revoked",
    re.IGNORECASE)
_QUOTA_RE = re.compile(
    r"usage limit|session limit|weekly limit|hit your (?:\w+ )?limit|limit reached|out of (?:usage )?credits|"
    r"credits_required|requires_usage_credits|credit balance is too low|no credits left|subscription|"
    r"upgrade your plan|rate_limit_error|rate[ -]limited|rate limit exceeded|too many requests|"
    r"insufficient_quota|exceeded your (?:current )?quota|quota exceeded|asks for payment|payment required",
    re.IGNORECASE)
_MODEL_RE = re.compile(
    r"model[ _]not[ _]found|not_found_error|unknown model|invalid model|unsupported model|"
    r"model\b.{0,80}(?:not found|does not exist|not available|not supported|not served|retired|end of life)|"
    r"reached its end of life|was retired|not found for account|is not served at this endpoint",
    re.IGNORECASE)
_UNAVAILABLE_RE = re.compile(
    r"cannot reach|can't reach|could not reach|unable to reach|unreachable|overload|connection|network|"
    r"temporarily unavailable|service unavailable|server error|bad gateway|gateway time-?out|lost contact|"
    r"no code engine|not found on path|cannot start|cannot execute|cannot import|name or service not known|"
    r"getaddrinfo|\bdns\b|econnrefused|econnreset|\bssl\b|certificate verify",
    re.IGNORECASE)
_MODEL_WORD_RE = re.compile(r"\bmodel", re.IGNORECASE)
_AUTH_WORD_RE = re.compile(r"auth|token|api[ _-]?key|credential|forbidden|permission|login|logged in", re.IGNORECASE)
_STATUS_RE = re.compile(r"\bHTTP[ /]?(?:1\.[01] |2 )?([1-5]\d\d)\b|\bstatus(?: code)?[ =:]+([1-5]\d\d)\b|"
                        r"\((4\d\d|5\d\d)\)", re.IGNORECASE)


def _failure_text(result: EngineResult) -> str:
    raw = result.raw if isinstance(result.raw, dict) else {}
    parts = [str(result.error or "")]
    api = raw.get("api_error")
    if isinstance(api, dict):
        parts += [str(v) for v in api.values() if v is not None]
    elif api:
        parts.append(str(api))
    return "\n".join(p for p in parts if p)


def _failure_statuses(result: EngineResult, text: str) -> list[int]:
    raw = result.raw if isinstance(result.raw, dict) else {}
    found: list[int] = []
    api = raw.get("api_error") if isinstance(raw.get("api_error"), dict) else {}
    for val in (api.get("api_error_status"), raw.get("status"), raw.get("http_status"), raw.get("api_error_status")):
        if isinstance(val, int) and not isinstance(val, bool) and 100 <= val <= 599:
            found.append(val)
    for m in _STATUS_RE.finditer(text):
        found.append(int(next(g for g in m.groups() if g)))
    return list(dict.fromkeys(found))


def classify_failure(result: EngineResult | None) -> str | None:
    """``"auth"`` / ``"quota"`` / ``"unavailable"`` / ``"model"`` when a failed result is an account or service
    failure that another engine might not have, else None (ok, cancelled, or a task failure).

    ``result.raw["failure_kind"]`` (set by the engine that knows) wins; otherwise the
    error text, ``raw["api_error"]`` and Claude Code's ``raw["rate_limits"]`` decide.
    """
    if result is None or result.ok:
        return None
    raw = result.raw if isinstance(result.raw, dict) else {}
    if raw.get("failure_kind") in FAILURE_KINDS:
        return str(raw["failure_kind"])
    if str(result.error or "").strip().lower().startswith("cancelled"):
        return None
    text = _failure_text(result)
    if not text.strip() or _TASK_FAILURE_RE.search(text):
        return None
    if _AUTH_RE.search(text):
        return "auth"
    if _QUOTA_RE.search(text):
        return "quota"
    limits = raw.get("rate_limits")
    if isinstance(limits, list) and any(isinstance(e, dict) and str(e.get("status") or "").lower() == "rejected"
                                        for e in limits):
        return "quota"  # Claude Code's rate_limit_event said the account's limit rejected the request
    if _MODEL_RE.search(text):
        return "model"
    for status in _failure_statuses(result, text):
        if status in (402, 429):
            return "quota"
        if status == 401:
            return "auth"
        if status == 403:
            return "model" if _MODEL_WORD_RE.search(text) and not _AUTH_WORD_RE.search(text) else "auth"
        if status == 410 or (status == 404 and _MODEL_WORD_RE.search(text)):
            return "model"
        if 500 <= status <= 599:
            return "unavailable"
    if _UNAVAILABLE_RE.search(text):
        return "unavailable"
    return None


# ----------------------------------------------------------------------------- reset times
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"), start=1)}
_ISO_RE = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
_RESET_EPOCH_RE = re.compile(r"limit reached\s*\|\s*(\d{10,13})\b", re.IGNORECASE)  # older CLIs: "...reached|<epoch>"
_RESET_ISO_RE = re.compile(r"\bresets?(?:_at|At| at| on)?\s*[:=]?\s*[\"']?(" + _ISO_RE + ")", re.IGNORECASE)
_DURATION = (r"(?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s+)?)+")
_RESET_IN_RE = re.compile(r"\b(?:try again|retry|resets?|available again|again)\s+(?:in|after)\s+(" + _DURATION + ")",
                          re.IGNORECASE)
_IN_RE = re.compile(r"\bin\s+(" + _DURATION + ")", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"\bretry[- ]after[\"']?\s*[:=]\s*[\"']?(\d+)\b", re.IGNORECASE)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)", re.IGNORECASE)
_RESET_CLOCK_RE = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?(?![\w:])"
    r"(?:\s*\(([^)]{1,64})\)|\s+(UTC|GMT|Z)\b)?", re.IGNORECASE)
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _epoch(value: Any) -> float | None:
    """Epoch seconds from a number (seconds or milliseconds) or an ISO 8601 string; None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            value = float(text)
        else:
            return _iso_epoch(text)
    if isinstance(value, (int, float)):
        val = float(value)
        if val > 1e11:  # milliseconds
            val /= 1000.0
        return _sane_epoch(val)
    return None


def _sane_epoch(val: float | None) -> float | None:
    """``val`` when it is a plausible time (2001..2100), else None: garbage never reaches a timer or a date format."""
    return val if val is not None and 1e9 < val < 4102444800.0 else None


def _iso_epoch(text: str) -> float | None:
    text = text.strip().replace(" ", "T", 1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    m = re.match(r"^(.*T\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?([+-]\d{2}:?\d{2})?$", text)
    if m:  # Python 3.10's fromisoformat wants 0/3/6 fraction digits and a colon in the offset
        frac = f".{(m.group(2) + '000000')[:6]}" if m.group(2) else ""
        off = m.group(3) or ""
        if off and ":" not in off:
            off = off[:3] + ":" + off[3:]
        text = m.group(1) + frac + off
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _sane_epoch(dt.timestamp())


def _duration_s(text: str) -> float:
    return sum(float(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u.lower()]
               for n, u in _DURATION_PART_RE.findall(text))


def _zone(name: str | None):
    """tzinfo for a zone named in a reset message: UTC/GMT, a fixed offset, an IANA name (zoneinfo), else UTC.

    No name gives None: naive local time (the CLI prints the reset in this device's local time, and naive
    datetimes follow the local daylight-saving rules of the reset's own date).
    """
    if not name:
        return None
    label = name.strip()
    if label.upper() in ("UTC", "GMT", "Z", "UTC+0", "GMT+0", "UTC0"):
        return timezone.utc
    m = _OFFSET_RE.match(label)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(-delta if m.group(1) == "-" else delta)
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415 - stdlib since 3.9; its tz database may be missing
        return ZoneInfo(label)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, no tzdata on Windows/iSH
        log.warning("reset time zone %r is unknown on this device (no tz database entry); treating it as UTC", label)
        return timezone.utc


def _clock_epoch(m: re.Match, now: float) -> float | None:
    """The next future occurrence of a "resets 1:20pm (UTC)" / "resets Oct 9, 10am (Europe/Paris)" time."""
    month, day, hour, minute, ampm, zone_paren, zone_word = m.groups()
    if not ampm and minute is None:
        return None  # "resets 5" is not a time of day
    hour, minute = int(hour), int(minute or 0)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower().startswith("p") else 0)
    if hour > 23 or minute > 59:
        return None
    tz = _zone(zone_paren or zone_word)
    now_dt = datetime.fromtimestamp(now, tz)
    try:
        if month:
            cand = now_dt.replace(month=_MONTHS[month.lower()[:3]], day=int(day), hour=hour, minute=minute,
                                  second=0, microsecond=0)
            if cand.timestamp() < now - 12 * 3600:
                cand = cand.replace(year=cand.year + 1)
        else:
            cand = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand.timestamp() <= now:
                cand = cand + timedelta(days=1)  # wall-clock arithmetic: the same local time tomorrow
    except ValueError:
        return None
    return cand.timestamp()


def parse_reset_text(text: str, now: float | None = None) -> float | None:
    """Epoch seconds of the reset a limit message names, or None.

    Understands ``... limit reached|1760000000``, ``resets at 2026-10-18T13:20:00Z``,
    ``resets 1:20pm (UTC)`` / ``resets 13:20 (UTC)`` / ``resets 1pm (Europe/Paris)`` /
    ``resets Oct 9, 10am (Europe/Berlin)`` (the next future occurrence), ``try again in 3h 20m``,
    ``in 45 minutes`` and ``Retry-After: 120``.
    """
    now = time.time() if now is None else float(now)
    text = str(text or "")
    return _sane_epoch(_parse_reset(text, now)) if text.strip() else None


def _parse_reset(text: str, now: float) -> float | None:
    m = _RESET_EPOCH_RE.search(text)
    if m:
        return _epoch(m.group(1))
    m = _RESET_ISO_RE.search(text)
    if m:
        at = _iso_epoch(m.group(1))
        if at is not None:
            return at
    for m in _RESET_CLOCK_RE.finditer(text):
        at = _clock_epoch(m, now)
        if at is not None:
            return at
    for rx in (_RESET_IN_RE, _IN_RE):
        m = rx.search(text)
        if m:
            secs = _duration_s(m.group(1))
            if secs > 0:
                return now + secs
    m = _RETRY_AFTER_RE.search(text)
    if m and int(m.group(1)) > 0:
        return now + int(m.group(1))
    return None


def _rate_limit_reset(limits: Any, now: float) -> float | None:
    """The reset of Claude Code's ``rate_limit_event`` infos: the latest rejected window, else the latest one."""
    if not isinstance(limits, list):
        return None
    rejected, other = [], []
    for e in limits:
        if not isinstance(e, dict):
            continue
        at = None
        for key in ("resetsAt", "resets_at", "resetAt", "reset_at"):
            at = _epoch(e.get(key))
            if at is not None:
                break
        if at is None or at <= now:
            continue
        (rejected if str(e.get("status") or "").lower() == "rejected" else other).append(at)
    pick = rejected or other
    return max(pick) if pick else None


def reset_time(result: EngineResult | None, now: float | None = None) -> float | None:
    """When the limit that failed ``result`` resets (epoch seconds), or None when nothing says so.

    Sources in order: ``raw["resets_at"]`` (epoch or ISO 8601), Claude Code's
    ``raw["rate_limits"]`` (``resetsAt``), then the error / result text and
    ``raw["api_error"]`` (see :func:`parse_reset_text`).
    """
    if result is None:
        return None
    now = time.time() if now is None else float(now)
    raw = result.raw if isinstance(result.raw, dict) else {}
    at = _epoch(raw.get("resets_at"))
    if at is not None:
        return at
    at = _rate_limit_reset(raw.get("rate_limits"), now)
    if at is not None:
        return at
    return parse_reset_text("\n".join(p for p in (_failure_text(result), str(result.result_text or "")) if p), now)
