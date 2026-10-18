"""In-process mock of the NVIDIA API Catalog's OpenAI-compatible endpoint (tests only).

``MockNIM(handler)`` starts a ``ThreadingHTTPServer`` on ``127.0.0.1:<random>``
that emulates ``POST /v1/chat/completions`` (stream and non-stream) and
``GET /v1/models``. ``handler(request_json)`` returns a response spec dict:

```
{"content": str,                       # assistant text
 "reasoning": str,                     # optional; streamed via delta.reasoning_content AND delta.reasoning
 "reasoning_both": bool,               # optional: every reasoning delta carries both keys (vLLM >= 0.11 style)
 "tool_calls": [{"id", "name", "arguments" (str or dict)}],   # optional
 "usage": {...} | None,                # optional; None = send no usage block at all
 "finish_reason": str,                 # optional (default stop / tool_calls)
 "status": int,                        # optional: answer with this HTTP status and an error body
 "error_body": dict | str,             # optional body for status != 200
 "retry_after": int | str,             # optional Retry-After header for status != 200
 "chunks": int,                        # optional number of content chunks
 "delay_s": float,                     # optional sleep between streamed chunks
 "first_delay_s": float,               # optional sleep before the response headers
 "headers": {name: value},             # optional extra response headers
 "json_even_if_stream": bool,          # optional: ignore stream=true and answer with JSON
 "stream_error": dict,                 # optional: an error object inside the 200 answer (see below)
 "after_chunks": int}                  # optional: emit stream_error after this many delta chunks (default 0)
```

``stream_error`` reproduces what the NVIDIA API Catalog sends on the free tier, an HTTP 200 whose
stream carries ``{"error": {"message": "Service temporarily overloaded", "type": "Service Unavailable",
"code": 503}}``. The dict is sent as one ``data:`` event (wrapped as ``{"error": ...}`` when it has no
``error`` key) after the first ``after_chunks`` delta chunks (reasoning, content and tool-call fragments
in streaming order; 0 = the first data event) and the stream ends there. A non-stream request gets the
same object as its 200 JSON body.

This file lives under ``tests/`` on purpose: no mock output ever ships in ``kyora/``.
"""
from __future__ import annotations

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

DEFAULT_MODELS = ["nvidia/nemotron-3-ultra-550b-a55b", "moonshotai/kimi-k3"]
Handler = Callable[[dict], dict]


def _split(text: str, n: int) -> list[str]:
    if not text:
        return []
    n = max(1, min(int(n), len(text)))
    size = -(-len(text) // n)  # ceil
    return [text[i:i + size] for i in range(0, len(text), size)]


def _default_chunks(text: str) -> int:
    return max(1, min(40, len(text) // 4)) if text else 0


def _tool_calls(spec: dict) -> list[dict]:
    out = []
    for i, tc in enumerate(spec.get("tool_calls") or []):
        args = tc.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": str(tc.get("id") or f"call_{i + 1}"), "type": "function",
                    "function": {"name": str(tc.get("name") or "tool"), "arguments": args}})
    return out


def _usage(spec: dict, req: dict) -> dict | None:
    if "usage" in spec:
        return spec["usage"]  # may be None -> omitted
    prompt_text = json.dumps(req.get("messages") or [], ensure_ascii=False)
    completion_text = (spec.get("reasoning") or "") + (spec.get("content") or "") + "".join(
        tc["function"]["arguments"] for tc in _tool_calls(spec))
    completion = max(1, len(completion_text.split()))
    prompt = max(1, len(prompt_text) // 4)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _finish_reason(spec: dict) -> str:
    return str(spec.get("finish_reason") or ("tool_calls" if spec.get("tool_calls") else "stop"))


def build_completion(req: dict, spec: dict) -> dict:
    """Non-stream ``chat.completion`` JSON for a response spec."""
    message: dict = {"role": "assistant", "content": spec.get("content") or ""}
    if spec.get("reasoning"):
        message["reasoning_content"] = spec["reasoning"]
    tcs = _tool_calls(spec)
    if tcs:
        message["tool_calls"] = tcs
    body: dict = {"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
                  "model": req.get("model", "mock"),
                  "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(spec)}]}
    usage = _usage(spec, req)
    if usage is not None:
        body["usage"] = usage
    return body


def iter_stream_chunks(req: dict, spec: dict) -> Iterable[dict | str]:
    """Yield OpenAI-style ``chat.completion.chunk`` dicts (and finally the string ``[DONE]``)."""
    model = req.get("model", "mock")
    created = int(time.time())

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    reasoning = spec.get("reasoning") or ""
    # Alternate the two field names the real gateways use so both parser paths are exercised.
    for i, part in enumerate(_split(reasoning, spec.get("reasoning_chunks") or _default_chunks(reasoning))):
        if spec.get("reasoning_both"):
            yield chunk({"reasoning": part, "reasoning_content": part})
        else:
            yield chunk({"reasoning_content": part} if i % 2 == 0 else {"reasoning": part})
    content = spec.get("content") or ""
    for part in _split(content, spec.get("chunks") or _default_chunks(content)):
        yield chunk({"content": part})
    for idx, tc in enumerate(_tool_calls(spec)):
        args = tc["function"]["arguments"]
        frags = _split(args, max(2, spec.get("tool_chunks") or min(6, max(2, len(args) // 8)))) or [""]
        yield chunk({"tool_calls": [{"index": idx, "id": tc["id"], "type": "function",
                                     "function": {"name": tc["function"]["name"], "arguments": frags[0]}}]})
        for frag in frags[1:]:
            yield chunk({"tool_calls": [{"index": idx, "function": {"arguments": frag}}]})
    yield chunk({}, _finish_reason(spec))
    usage = _usage(spec, req)
    if usage is not None:
        yield {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": created, "model": model,
               "choices": [], "usage": usage}
    yield "[DONE]"


def _stream_error_event(spec: dict) -> dict:
    err = spec.get("stream_error")
    return dict(err) if isinstance(err, dict) and "error" in err else {"error": err}


def _is_delta_chunk(item: dict | str) -> bool:
    """A chunk that delivers content, reasoning or a tool-call fragment (not the role / finish / usage chunk)."""
    if not isinstance(item, dict):
        return False
    for choice in item.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("reasoning") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False


def with_stream_error(items: Iterable[dict | str], spec: dict) -> Iterator[dict | str]:
    """Pass ``items`` through until ``spec["after_chunks"]`` delta chunks went out, then the error event, then stop."""
    after = max(0, int(spec.get("after_chunks") or 0))
    event = _stream_error_event(spec)
    if after == 0:
        yield event
        return
    sent = 0
    for item in items:
        if _is_delta_chunk(item):
            yield item
            sent += 1
            if sent >= after:
                break
        elif isinstance(item, dict) and any("role" in (c.get("delta") or {}) for c in item.get("choices") or []):
            yield item  # the opening role chunk
        else:
            break  # finish / usage / [DONE]: fewer deltas than after_chunks, the error comes now
    yield event


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    mock: "MockNIM" = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # silence the default stderr logging
        pass

    # helpers ----------------------------------------------------------------
    def _send_json(self, status: int, body: Any, headers: dict | None = None) -> None:
        data = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth_ok(self) -> bool:
        required = self.mock.require_key
        if not required:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {required}"

    def _path(self) -> str:
        path = urlsplit(self.path).path
        return path[3:] if path.startswith("/v1/") else path

    # GET ----------------------------------------------------------------------
    def do_GET(self) -> None:
        path = self._path()
        self.mock._record("GET", self.path, dict(self.headers), None)
        if path != "/models":
            self._send_json(404, {"error": {"message": f"no route for GET {self.path}", "code": 404}})
            return
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "invalid api key", "code": 401}})
            return
        if self.mock.models_status and self.mock.models_status != 200:
            self._send_json(self.mock.models_status, {"error": {"message": "models unavailable", "code": self.mock.models_status}})
            return
        self._send_json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in self.mock.models]})

    # POST ---------------------------------------------------------------------
    def do_POST(self) -> None:
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid JSON body", "code": 400}})
            return
        self.mock._record("POST", self.path, dict(self.headers), req)
        if path != "/chat/completions":
            self._send_json(404, {"error": {"message": f"no route for POST {self.path}", "code": 404}})
            return
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "invalid api key", "code": 401}})
            return
        try:
            spec = dict(self.mock.handler(req) or {})
        except Exception as exc:  # noqa: BLE001 - surface handler bugs as a 500 the client can report
            self._send_json(500, {"error": {"message": f"mock handler raised {type(exc).__name__}: {exc}", "code": 500}})
            return
        status = int(spec.get("status") or 200)
        if status != 200:
            headers = dict(spec.get("headers") or {})
            if "retry_after" in spec:
                headers["Retry-After"] = str(spec["retry_after"])
            body = spec.get("error_body") or {"error": {"message": f"mock error {status}", "code": status}}
            self._send_json(status, body, headers)
            return
        if spec.get("first_delay_s"):
            time.sleep(float(spec["first_delay_s"]))
        if req.get("stream") and not spec.get("json_even_if_stream"):
            self._stream(req, spec)
        elif spec.get("stream_error") is not None:
            self._send_json(200, _stream_error_event(spec), spec.get("headers"))
        else:
            self._send_json(200, build_completion(req, spec), spec.get("headers"))

    def _stream(self, req: dict, spec: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        for k, v in (spec.get("headers") or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        delay = float(spec.get("delay_s") or 0.0)
        items = iter_stream_chunks(req, spec)
        if spec.get("stream_error") is not None:
            items = with_stream_error(items, spec)
        try:
            for item in items:
                payload = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.mock._note_disconnect()


class _MockServer(ThreadingHTTPServer):
    """No reverse DNS on bind (``HTTPServer.server_bind`` calls ``getfqdn``, slow on macOS runners)."""
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


class MockNIM:
    """Mock NVIDIA API Catalog server. Use as a context manager or call ``start()``/``stop()``."""

    def __init__(self, handler: Handler | None = None, *, models: list[str] | None = None, host: str = "127.0.0.1",
                 port: int = 0, require_key: str | None = None):
        self.handler: Handler = handler or (lambda req: {"content": "ok"})
        self.models = list(models or DEFAULT_MODELS)
        self.models_status = 200
        self.require_key = require_key
        self.requests: list[dict] = []        # POST /chat/completions bodies, in order
        self.calls: list[dict] = []           # every request: {"method", "path", "headers", "json"}
        self.disconnects = 0
        self._lock = threading.Lock()
        handler_cls = type("BoundMockHandler", (_MockHandler,), {"mock": self})
        self._server = _MockServer((host, port), handler_cls)
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread: threading.Thread | None = None

    # recording ----------------------------------------------------------------
    def _record(self, method: str, path: str, headers: dict, body: Any) -> None:
        with self._lock:
            self.calls.append({"method": method, "path": path, "headers": {k.lower(): v for k, v in headers.items()}, "json": body})
            if method == "POST" and isinstance(body, dict):
                self.requests.append(body)

    def _note_disconnect(self) -> None:
        with self._lock:
            self.disconnects += 1

    @property
    def url(self) -> str:
        """Base URL ending in ``/v1`` (what ``NIMClient`` expects)."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def last_request(self) -> dict | None:
        with self._lock:
            return self.requests[-1] if self.requests else None

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.calls.clear()
            self.disconnects = 0

    # lifecycle ----------------------------------------------------------------
    def start(self) -> "MockNIM":
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                            name="mock-nim", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> "MockNIM":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ----------------------------------------------------------------------------- scripting helpers
def scripted_handler(rules: list[tuple]) -> Handler:
    """Build a handler from ``[(predicate(req) -> bool, response_dict[, once]) ...]``.

    The first matching rule answers. A rule whose response carries ``"once": True``
    (or a third tuple element ``True``) is removed after it fires, which is how
    a single 429/500 before success is scripted. No match -> HTTP 500 with a
    clear message so the test fails loudly instead of silently succeeding.
    """
    live: list[list] = [list(r) for r in rules]
    lock = threading.Lock()

    def handler(req: dict) -> dict:
        with lock:
            for i, rule in enumerate(live):
                pred, resp = rule[0], rule[1]
                if pred(req):
                    out = dict(resp)
                    once = bool(out.pop("once", False)) or (len(rule) > 2 and bool(rule[2]))
                    if once:
                        live.pop(i)
                    return out
        return {"status": 500, "error_body": {"error": {"message": "mock_nim: no scripted rule matched", "code": 500}}}

    return handler


def tool_response(name: str, arguments: Any, *, reasoning: str = "", content: str = "", call_id: str = "call_1",
                  **extra: Any) -> dict:
    """Response spec that answers with one tool call."""
    spec = {"content": content, "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}]}
    if reasoning:
        spec["reasoning"] = reasoning
    spec.update(extra)
    return spec


def any_req(req: dict) -> bool:
    return True


def has_role(role: str) -> Callable[[dict], bool]:
    """Predicate: the request carries at least one message with ``role``."""
    return lambda req: any(m.get("role") == role for m in req.get("messages") or [])


def last_user_text(req: dict) -> str:
    """Flattened text of the last user message (string or parts)."""
    for m in reversed(req.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def has_image(req: dict) -> bool:
    """Predicate: some user message carries an ``image_url`` part."""
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


__all__ = ["MockNIM", "scripted_handler", "tool_response", "any_req", "has_role", "has_image", "last_user_text",
           "build_completion", "iter_stream_chunks", "with_stream_error", "DEFAULT_MODELS"]
