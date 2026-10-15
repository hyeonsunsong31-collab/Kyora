"""OpenAI-compatible chat client for the NVIDIA API Catalog (``integrate.api.nvidia.com/v1``).

Both NVIDIA Nemotron-3-Ultra and Moonshot Kimi-K3 are reached through this
client. It is standard-library only (``urllib``), streams Server-Sent Events,
accumulates tool calls across chunks and measures honest metrics: time to
first token, total latency and tokens per second taken from the server's
``usage`` block or, when the server sends none, an estimate that is labelled
as such (``tokens_source='estimated_chunks'``).

Nothing in this module fabricates output. Every failure surfaces as
:class:`NIMError` (HTTP status, malformed payload, cancellation) or
:class:`NIMUnreachable` (network / timeout). The API key is never logged.

Besides the client, this module hosts the small helpers shared by the two
model wrappers (``nemotron.py``, ``kimi.py``): function-tool builders,
structured-output extraction, prompt-template loading and
:func:`chat_with_events`, which publishes ``model.*`` events on the EventBus
around one chat call.
"""
from __future__ import annotations

import email.utils
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..util.sse import parse_sse
from .base import Metrics

log = logging.getLogger("kyora.models.nim")

DEFAULT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_TIMEOUT_S = 600.0
MAX_RETRY_AFTER_S = 60.0
_ERROR_EXCERPT = 400

DELTA_CONTENT, DELTA_REASONING, DELTA_TOOL_CALL = "content", "reasoning", "tool_call"


# ----------------------------------------------------------------------------- errors
class NIMError(Exception):
    """An error reported by the API or the protocol (HTTP status, bad payload, cancellation)."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class NIMUnreachable(NIMError):
    """The endpoint could not be reached (DNS, connection refused, TLS, timeout, connection lost)."""


class NIMCancelled(NIMError):
    """The caller's cancel event was set while a request was in flight."""

    def __init__(self, message: str = "cancelled"):
        super().__init__(message)


# ----------------------------------------------------------------------------- data types
@dataclass
class Delta:
    """One streamed fragment. ``kind`` is ``content``, ``reasoning`` or ``tool_call``.

    For ``tool_call`` deltas ``text`` holds the arguments fragment and
    ``tool_call`` the partial call ``{"index", "id", "name"}``.
    """

    kind: str
    text: str = ""
    tool_call: dict | None = None


@dataclass
class ChatResult:
    """The complete answer of one chat completion.

    ``message`` is the COMPLETE assistant message dict to echo back in a
    multi-turn conversation (``reasoning_content`` only when non-empty,
    ``tool_calls`` only when any). ``tool_calls`` keep the OpenAI shape
    ``{"id", "type": "function", "function": {"name", "arguments": "<json str>"}}``.
    """

    content: str
    reasoning: str
    tool_calls: list[dict]
    finish_reason: str
    usage: dict
    metrics: Metrics
    message: dict

    def to_dict(self) -> dict:
        return {"content": self.content, "reasoning": self.reasoning, "tool_calls": self.tool_calls,
                "finish_reason": self.finish_reason, "usage": self.usage, "metrics": self.metrics.to_dict()}


def build_assistant_message(content: str, reasoning: str, tool_calls: list[dict]) -> dict:
    """Return the complete assistant message to store in a conversation."""
    msg: dict = {"role": "assistant", "content": content or ""}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [json.loads(json.dumps(tc)) for tc in tool_calls]
    return msg


def tool_call_arguments(tool_call: Mapping) -> dict:
    """Parse the ``function.arguments`` of a tool call into a dict (``{}`` when empty or unparsable)."""
    fn = tool_call.get("function") if isinstance(tool_call, Mapping) else None
    args = (fn or {}).get("arguments") if isinstance(fn, Mapping) else None
    if args is None and isinstance(tool_call, Mapping):
        args = tool_call.get("arguments")
    if isinstance(args, Mapping):
        return dict(args)
    if not isinstance(args, str) or not args.strip():
        return {}
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        parsed = extract_json_object(args)
    return parsed if isinstance(parsed, dict) else {}


def function_tool(name: str, description: str, parameters: dict) -> dict:
    """Build an OpenAI-style function tool definition."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def force_tool(name: str) -> dict:
    """``tool_choice`` value that forces the named function."""
    return {"type": "function", "function": {"name": name}}


# ----------------------------------------------------------------------------- accumulation
class _Accumulator:
    """Collects content, reasoning and tool-call fragments of one streamed answer."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self._tools: dict[Any, dict] = {}
        self._order: list[Any] = []

    def add_tool_fragment(self, tc: Mapping) -> tuple[str, dict]:
        idx = tc.get("index")
        if idx is None:
            # No index: merge by id when the id repeats, else start a new call.
            tc_id = tc.get("id")
            idx = next((k for k, v in self._tools.items() if tc_id and v["id"] == tc_id), None)
            if idx is None:
                idx = f"noidx-{len(self._order)}"
        entry = self._tools.get(idx)
        if entry is None:
            entry = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            self._tools[idx] = entry
            self._order.append(idx)
        if tc.get("id"):
            entry["id"] = str(tc["id"])
        if tc.get("type"):
            entry["type"] = str(tc["type"])
        fn = tc.get("function") or {}
        if isinstance(fn, Mapping):
            name = fn.get("name")
            if name and not entry["function"]["name"]:
                entry["function"]["name"] = str(name)
            args = fn.get("arguments")
            if isinstance(args, Mapping):
                args = json.dumps(args, ensure_ascii=False)
            frag = str(args) if args else ""
            entry["function"]["arguments"] += frag
        else:
            frag = ""
        return frag, {"index": idx, "id": entry["id"], "name": entry["function"]["name"]}

    def tool_calls(self) -> list[dict]:
        out = []
        for i, idx in enumerate(self._order):
            entry = json.loads(json.dumps(self._tools[idx]))
            if not entry["id"]:
                entry["id"] = f"call_{i}"
            out.append(entry)
        return out


def _message_text(content: Any) -> str:
    """Flatten a ``content`` that may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, Mapping) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return "" if content is None else str(content)


def _normalize_tool_calls(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for i, tc in enumerate(raw):
        if not isinstance(tc, Mapping):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments") if isinstance(fn, Mapping) else None
        if isinstance(args, Mapping):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": str(tc.get("id") or f"call_{i}"), "type": str(tc.get("type") or "function"),
                    "function": {"name": str((fn or {}).get("name") or ""), "arguments": "" if args is None else str(args)}})
    return out


def _metrics(t0: float, t_first: float | None, t_end: float, usage: Mapping, text_chunks: int) -> Metrics:
    total_ms = round((t_end - t0) * 1000.0, 1)
    ttft_ms = round((t_first - t0) * 1000.0, 1) if t_first is not None else None
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
    completion_tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
    prompt_tokens = int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else None
    completion_tokens = int(completion_tokens) if isinstance(completion_tokens, (int, float)) else None
    if prompt_tokens is not None or completion_tokens is not None:
        source = "usage"
    elif text_chunks > 0:
        completion_tokens = text_chunks
        source = "estimated_chunks"
    else:
        source = "none"
    gen_s = (t_end - t_first) if t_first is not None else (t_end - t0)
    tps = round(completion_tokens / gen_s, 2) if completion_tokens and gen_s > 0.001 else None
    return Metrics(ttft_ms=ttft_ms, total_ms=total_ms, prompt_tokens=prompt_tokens,
                   completion_tokens=completion_tokens, tps=tps, tokens_source=source)


def _excerpt(text: str | None, n: int = _ERROR_EXCERPT) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - body is best effort
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")


def _retry_after_s(headers: Any) -> float | None:
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except Exception:  # noqa: BLE001
        value = None
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER_S))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), MAX_RETRY_AFTER_S))
    except Exception:  # noqa: BLE001
        return None


def _emit(on_delta: Callable[[Delta], None] | None, delta: Delta) -> None:
    if on_delta is None:
        return
    try:
        on_delta(delta)
    except Exception as exc:  # noqa: BLE001 - a UI callback must never break the request
        log.warning("on_delta callback raised: %s", exc)


# ----------------------------------------------------------------------------- client
class NIMClient:
    """Thread-safe (stateless) client for one OpenAI-compatible endpoint and key."""

    def __init__(self, base_url: str, api_key: str, *, user_agent: str = "kyora",
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_s: Sequence[float] = DEFAULT_BACKOFF_S, opener: urllib.request.OpenerDirector | None = None):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("NIMClient: base_url is required")
        self.base_url = base
        self.api_key = api_key or ""
        self.user_agent = user_agent or "kyora"
        self.timeout_s = float(timeout_s)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = tuple(float(b) for b in backoff_s) or DEFAULT_BACKOFF_S
        # build_opener() installs the env-based ProxyHandler and the default HTTPS handler.
        self._opener = opener or urllib.request.build_opener()

    # ------------------------------------------------------------------ helpers
    def __repr__(self) -> str:
        return f"NIMClient({self.base_url!r}, key={self.key_hint()!r})"

    def key_hint(self) -> str:
        """A redacted form of the key safe for logs and the UI."""
        k = self.api_key
        if not k:
            return "(no key)"
        return "*" * len(k) if len(k) <= 12 else f"{k[:7]}…{k[-3:]}"

    def _host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).netloc or self.base_url

    def _headers(self, *, stream: bool, json_body: bool) -> dict[str, str]:
        h = {"User-Agent": self.user_agent, "Accept": "text/event-stream" if stream else "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def build_body(self, model: str, messages: list[dict], *, stream: bool = True, tools: list[dict] | None = None,
                   tool_choice: Any = None, temperature: float | None = None, top_p: float | None = None,
                   max_tokens: int | None = None, extra: Mapping | None = None) -> dict:
        """Assemble the JSON body exactly as it is sent (useful for tests and logs)."""
        body: dict = {"model": model, "messages": list(messages)}
        if temperature is not None:
            body["temperature"] = float(temperature)
        if top_p is not None:
            body["top_p"] = float(top_p)
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if tools:
            body["tools"] = list(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        else:
            body["stream"] = False
        if extra:
            for k, v in extra.items():
                body[k] = v
        return body

    @staticmethod
    def _check_cancel(cancel: threading.Event | None, resp: Any = None) -> None:
        if cancel is not None and cancel.is_set():
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            raise NIMCancelled()

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event | None) -> None:
        if seconds <= 0:
            return
        if cancel is not None:
            if cancel.wait(seconds):
                raise NIMCancelled()
        else:
            time.sleep(seconds)

    def _open(self, req: urllib.request.Request, timeout: float):
        return self._opener.open(req, timeout=timeout)

    # ------------------------------------------------------------------ chat
    def chat(self, model: str, messages: list[dict], *, stream: bool = True, tools: list[dict] | None = None,
             tool_choice: Any = None, temperature: float | None = None, top_p: float | None = None,
             max_tokens: int | None = None, extra: Mapping | None = None,
             on_delta: Callable[[Delta], None] | None = None, cancel: threading.Event | None = None,
             timeout_s: float | None = None) -> ChatResult:
        """Run one chat completion and return the complete :class:`ChatResult`.

        Retries 429 and 5xx with exponential backoff (honouring ``Retry-After``),
        raises :class:`NIMError` for other HTTP errors (``.status``/``.body`` set),
        :class:`NIMUnreachable` for network failures and :class:`NIMCancelled`
        (a ``NIMError`` whose message is ``'cancelled'``) when ``cancel`` is set.
        ``extra`` is merged top-level into the body (``chat_template_kwargs``,
        ``reasoning_effort`` ...). ``on_delta`` receives :class:`Delta` objects
        as they stream in (also once per field on the non-stream path).
        """
        body = self.build_body(model, messages, stream=stream, tools=tools, tool_choice=tool_choice,
                               temperature=temperature, top_p=top_p, max_tokens=max_tokens, extra=extra)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = self.base_url + "/chat/completions"
        timeout = float(timeout_s) if timeout_s else self.timeout_s
        attempt = 0
        while True:
            self._check_cancel(cancel)
            req = urllib.request.Request(url, data=payload, method="POST", headers=self._headers(stream=stream, json_body=True))
            t0 = time.monotonic()
            try:
                resp = self._open(req, timeout)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                err_body = _read_error_body(exc)
                if (status == 429 or status >= 500) and attempt + 1 < self.max_attempts:
                    retry_after = _retry_after_s(exc.headers)
                    delay = retry_after if retry_after is not None else self.backoff_s[min(attempt, len(self.backoff_s) - 1)]
                    log.warning("NIM %s: HTTP %s on attempt %d/%d, retrying in %.1fs (%s)", model, status,
                                attempt + 1, self.max_attempts, delay, _excerpt(err_body, 160))
                    attempt += 1
                    self._sleep(delay, cancel)
                    continue
                raise NIMError(f"HTTP {status} from {self._host()} for {model}: {_excerpt(err_body)}",
                               status=status, body=err_body) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", None) or exc
                raise NIMUnreachable(f"{self._host()} unreachable: {reason}") from None
            try:
                ctype = str(resp.headers.get("Content-Type", "") or "").lower()
                if stream and "text/event-stream" in ctype:
                    return self._consume_stream(resp, t0, on_delta, cancel)
                return self._consume_json(resp, t0, on_delta)
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass

    def _consume_stream(self, resp, t0: float, on_delta, cancel) -> ChatResult:
        acc = _Accumulator()
        t_first: float | None = None
        text_chunks = 0
        usage: dict = {}
        finish_reason = ""
        try:
            for ev in parse_sse(iter(resp)):
                self._check_cancel(cancel, resp)
                data = (ev.get("data") or "").strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("NIM stream: skipping non-JSON chunk: %s", _excerpt(data, 120))
                    continue
                if not isinstance(chunk, dict):
                    continue
                err = chunk.get("error")
                if err and not chunk.get("choices"):
                    msg = err.get("message") if isinstance(err, Mapping) else str(err)
                    code = err.get("code") if isinstance(err, Mapping) else None
                    raise NIMError(f"stream error from {self._host()}: {msg}",
                                   status=code if isinstance(code, int) else None, body=data)
                if isinstance(chunk.get("usage"), Mapping) and chunk["usage"]:
                    usage = dict(chunk["usage"])
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, Mapping):
                        continue
                    delta = choice.get("delta") or {}
                    got_text = False
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        acc.content.append(content)
                        got_text = True
                        _emit(on_delta, Delta(DELTA_CONTENT, content))
                    for key in ("reasoning_content", "reasoning"):
                        reasoning = delta.get(key)
                        if isinstance(reasoning, str) and reasoning:
                            acc.reasoning.append(reasoning)
                            got_text = True
                            _emit(on_delta, Delta(DELTA_REASONING, reasoning))
                    for tc in delta.get("tool_calls") or []:
                        if isinstance(tc, Mapping):
                            frag, partial = acc.add_tool_fragment(tc)
                            got_text = True
                            _emit(on_delta, Delta(DELTA_TOOL_CALL, frag, tool_call=partial))
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = str(fr)
                    if got_text:
                        text_chunks += 1
                        if t_first is None:
                            t_first = time.monotonic()
        except NIMError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise NIMUnreachable(f"connection to {self._host()} lost mid-stream: {exc}") from None
        t_end = time.monotonic()
        content = "".join(acc.content)
        reasoning = "".join(acc.reasoning)
        tool_calls = acc.tool_calls()
        if not finish_reason:
            finish_reason = "tool_calls" if tool_calls else "stop"
        return ChatResult(content=content, reasoning=reasoning, tool_calls=tool_calls, finish_reason=finish_reason,
                          usage=usage, metrics=_metrics(t0, t_first, t_end, usage, text_chunks),
                          message=build_assistant_message(content, reasoning, tool_calls))

    def _consume_json(self, resp, t0: float, on_delta) -> ChatResult:
        try:
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise NIMUnreachable(f"connection to {self._host()} lost while reading: {exc}") from None
        t_end = time.monotonic()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise NIMError(f"non-JSON response from {self._host()}: {_excerpt(text)}", body=text) from None
        if not isinstance(data, dict):
            raise NIMError(f"unexpected response shape from {self._host()}", body=text)
        err = data.get("error")
        if err and not data.get("choices"):
            msg = err.get("message") if isinstance(err, Mapping) else str(err)
            raise NIMError(f"error from {self._host()}: {msg}", body=text)
        choices = data.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
        msg = choice.get("message") or {}
        content = _message_text(msg.get("content"))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        reasoning = reasoning if isinstance(reasoning, str) else _message_text(reasoning)
        tool_calls = _normalize_tool_calls(msg.get("tool_calls"))
        finish_reason = str(choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop"))
        usage = dict(data.get("usage") or {}) if isinstance(data.get("usage"), Mapping) else {}
        if reasoning:
            _emit(on_delta, Delta(DELTA_REASONING, reasoning))
        if content:
            _emit(on_delta, Delta(DELTA_CONTENT, content))
        for tc in tool_calls:
            _emit(on_delta, Delta(DELTA_TOOL_CALL, tc["function"]["arguments"],
                                  tool_call={"index": None, "id": tc["id"], "name": tc["function"]["name"]}))
        return ChatResult(content=content, reasoning=reasoning, tool_calls=tool_calls, finish_reason=finish_reason,
                          usage=usage, metrics=_metrics(t0, None, t_end, usage, 0),
                          message=build_assistant_message(content, reasoning, tool_calls))

    # ------------------------------------------------------------------ discovery
    def _get_json(self, url: str, timeout: float) -> Any:
        req = urllib.request.Request(url, method="GET", headers=self._headers(stream=False, json_body=False))
        try:
            resp = self._open(req, timeout)
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            raise NIMError(f"HTTP {exc.code} from {self._host()} for GET {urllib.parse.urlsplit(url).path}: {_excerpt(body)}",
                           status=int(exc.code), body=body) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise NIMUnreachable(f"{self._host()} unreachable: {reason}") from None
        try:
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise NIMUnreachable(f"connection to {self._host()} lost: {exc}") from None
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise NIMError(f"non-JSON response from {self._host()}: {_excerpt(text)}", body=text) from None

    def list_models(self, timeout_s: float = 20.0) -> list[str]:
        """``GET /models`` -> list of model ids."""
        data = self._get_json(self.base_url + "/models", timeout_s)
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return [str(m.get("id")) for m in items if isinstance(m, Mapping) and m.get("id")]

    def probe(self, model: str, timeout_s: float = 30.0) -> dict:
        """Reachability + 1-token latency probe. Never raises.

        Returns ``{"reachable", "model_listed", "latency_ms", "error", "status"}``
        plus ``"chat_ok"`` and ``"reasoning_seen"`` for the self-check report.
        """
        out: dict = {"reachable": False, "model_listed": None, "latency_ms": None, "error": None, "status": None,
                     "chat_ok": False, "reasoning_seen": None, "model": model}
        try:
            models = self.list_models(timeout_s=timeout_s)
            out["reachable"] = True
            out["model_listed"] = model in models if models else None
        except NIMUnreachable as exc:
            out["error"] = str(exc)
            return out
        except NIMError as exc:
            out["reachable"] = True  # the host answered, even if with an error
            out["error"] = str(exc)
            out["status"] = exc.status
        except Exception as exc:  # noqa: BLE001
            log.exception("probe: unexpected error listing models")
            out["error"] = f"{type(exc).__name__}: {exc}"
        t0 = time.monotonic()
        try:
            res = self.chat(model, [{"role": "user", "content": "ping"}], stream=False, max_tokens=1, timeout_s=timeout_s)
            out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            out["reachable"] = True
            out["chat_ok"] = True
            out["reasoning_seen"] = bool(res.reasoning)
            out["error"] = None
            out["status"] = None
        except NIMUnreachable as exc:
            out["error"] = str(exc)
        except NIMError as exc:
            out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            out["reachable"] = True
            out["error"] = str(exc)
            out["status"] = exc.status
        except Exception as exc:  # noqa: BLE001
            log.exception("probe: unexpected error during chat")
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out


# ----------------------------------------------------------------------------- structured output helpers
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", re.DOTALL)
_MAX_SCAN = 2_000_000


def strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` blocks some chat templates leave in ``content``."""
    if not text:
        return ""
    out = _THINK_RE.sub("", text)
    # an unterminated <think> at the very start: drop everything up to the last '}' is too risky; just cut the tag
    return out.replace("<think>", "").replace("</think>", "").strip()


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` substring (string-aware), outermost first by start position."""
    n = min(len(text), _MAX_SCAN)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    break
            j += 1
        i += 1


def extract_json_object(text: str) -> dict | None:
    """Find the first JSON object in free text (fenced block, bare JSON or embedded ``{...}``)."""
    if not text:
        return None
    text = text.strip()
    for m in _FENCE_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for candidate in _balanced_objects(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_structured(result: ChatResult, tool_name: str) -> dict | None:
    """Return the structured payload of a tool-shaped answer.

    Prefers a tool call named ``tool_name``, then any tool call, then a JSON
    object embedded in the text content (unwrapping ``{"name", "arguments"}``
    envelopes some models emit as text). ``None`` when nothing parses.
    """
    for tc in result.tool_calls:
        if (tc.get("function") or {}).get("name") == tool_name:
            args = tool_call_arguments(tc)
            if args:
                return args
    for tc in result.tool_calls:
        args = tool_call_arguments(tc)
        if args:
            return args
    obj = extract_json_object(strip_think(result.content))
    if not isinstance(obj, dict):
        return None
    if "arguments" in obj and (obj.get("name") == tool_name or set(obj) <= {"name", "arguments", "type", "id", "function"}):
        inner = obj["arguments"]
        if isinstance(inner, str):
            inner = extract_json_object(inner)
        if isinstance(inner, dict):
            return inner
    fn = obj.get("function")
    if isinstance(fn, Mapping) and fn.get("name") == tool_name:
        inner = tool_call_arguments(obj)
        if inner:
            return inner
    return obj


def is_tool_rejection(exc: NIMError) -> bool:
    """True when a 4xx error says the gateway refused ``tools``/``tool_choice`` (fallback to plain JSON)."""
    if exc.status not in (400, 422):
        return False
    body = (exc.body or str(exc)).lower()
    return any(word in body for word in ("tool", "function", "tool_choice"))


# ----------------------------------------------------------------------------- prompt templates
_PROMPT_CACHE: dict[str, str] = {}
_PROMPT_LOCK = threading.Lock()


def load_prompt(name: str) -> str:
    """Read ``kyora/models/prompts/<name>`` (works from a source tree and from ``kyora.pyz``)."""
    with _PROMPT_LOCK:
        cached = _PROMPT_CACHE.get(name)
    if cached is not None:
        return cached
    node = resources.files("kyora").joinpath("models").joinpath("prompts").joinpath(name)
    text = node.read_text(encoding="utf-8")
    with _PROMPT_LOCK:
        _PROMPT_CACHE[name] = text
    return text


def render_prompt(name: str, mapping: Mapping[str, Any] | None = None, **kw: Any) -> str:
    """Load a template and fill its ``{placeholders}`` with ``str.format_map``."""
    values: dict[str, Any] = dict(mapping or {})
    values.update(kw)
    return load_prompt(name).format_map(values)


def json_for_prompt(obj: Any, limit: int = 24_000) -> str:
    """Pretty JSON for embedding in a prompt, truncated with an explicit marker (never silently)."""
    text = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} characters]"


# ----------------------------------------------------------------------------- bus integration
def chat_with_events(client: NIMClient, bus, *, role: str, model: str, run_id: str | None = None,
                     project_id: str | None = None, purpose: str | None = None, publish_deltas: bool = True,
                     **chat_kwargs: Any) -> ChatResult:
    """Run ``client.chat`` and publish ``model.request/delta/metrics/done`` or ``model.error`` on ``bus``.

    ``bus`` may be ``None`` (no events). Deltas are coalesced with
    :class:`kyora.pipeline.events.DeltaCoalescer`. Metrics are the measured
    :class:`Metrics` of the call; nothing is invented when the server sends
    no usage block (``tokens_source`` says what the numbers are).
    """
    from ..pipeline.events import MODEL_DONE, MODEL_ERROR, MODEL_METRICS, MODEL_REQUEST, DeltaCoalescer

    base = {"role": role, "model": model}
    if purpose:
        base["purpose"] = purpose

    def pub(evtype: str, data: dict) -> None:
        if bus is None:
            return
        try:
            bus.publish(evtype, {**base, **data}, run_id=run_id, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 - the bus must never break a model call
            log.warning("event publish failed (%s): %s", evtype, exc)

    pub(MODEL_REQUEST, {"stream": bool(chat_kwargs.get("stream", True))})
    coalescer = None
    if bus is not None and publish_deltas:
        coalescer = DeltaCoalescer(bus, run_id=run_id, project_id=project_id, role=role, model=model)
    user_on_delta = chat_kwargs.pop("on_delta", None)

    def on_delta(delta: Delta) -> None:
        if coalescer is not None and delta.kind in (DELTA_CONTENT, DELTA_REASONING) and delta.text:
            coalescer.add(delta.kind, delta.text)
        if user_on_delta is not None:
            user_on_delta(delta)

    try:
        res = client.chat(model, on_delta=on_delta, **chat_kwargs)
    except NIMError as exc:
        if coalescer is not None:
            coalescer.flush()
        pub(MODEL_ERROR, {"error": str(exc), "status": exc.status})
        raise
    except Exception as exc:  # noqa: BLE001 - report, then re-raise unchanged
        if coalescer is not None:
            coalescer.flush()
        pub(MODEL_ERROR, {"error": f"{type(exc).__name__}: {exc}", "status": None})
        raise
    if coalescer is not None:
        coalescer.flush()
    pub(MODEL_METRICS, {**res.metrics.to_dict(), "finish_reason": res.finish_reason})
    pub(MODEL_DONE, {"finish_reason": res.finish_reason,
                     "tool_calls": [(tc.get("function") or {}).get("name") for tc in res.tool_calls],
                     "content_chars": len(res.content), "reasoning_chars": len(res.reasoning)})
    return res


__all__ = [
    "NIMError", "NIMUnreachable", "NIMCancelled", "Delta", "ChatResult", "NIMClient",
    "build_assistant_message", "tool_call_arguments", "function_tool", "force_tool",
    "strip_think", "extract_json_object", "extract_structured", "is_tool_rejection",
    "load_prompt", "render_prompt", "json_for_prompt", "chat_with_events",
    "DELTA_CONTENT", "DELTA_REASONING", "DELTA_TOOL_CALL",
]
