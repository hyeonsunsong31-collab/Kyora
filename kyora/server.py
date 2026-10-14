"""HTTP server (standard library): router, multipart parser, SSE streaming, static assets.

Design notes
- ``ThreadingHTTPServer`` with HTTP/1.1 keep-alive for normal responses; SSE
  responses send ``Connection: close`` and stream until the client leaves.
- Access levels per route: ``any`` (static assets), ``local`` (loopback client
  OR a valid hub token in ``Authorization: Bearer``/``kyora_token`` cookie),
  ``loopback`` (loopback client only, e.g. the judge hook endpoint), ``hub``
  (valid token required).
- Static files come from the ``kyora/web`` package directory via
  ``importlib.resources`` so they also work inside ``kyora.pyz``.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import re
import socket
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .util.sse import encode_event, keepalive

log = logging.getLogger("kyora.server")
MAX_BODY = 256 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


# ----------------------------------------------------------------------------- multipart
@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str
    data: bytes

    def text(self) -> str:
        return self.data.decode("utf-8", "replace")


def _parse_header_params(value: str) -> tuple[str, dict]:
    main, *rest = value.split(";")
    params: dict = {}
    for item in rest:
        if "=" in item:
            k, v = item.strip().split("=", 1)
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            params[k.strip().lower()] = v
    return main.strip().lower(), params


def parse_multipart(body: bytes, content_type: str) -> list[Part]:
    """Parse ``multipart/form-data`` without the removed ``cgi`` module."""
    main, params = _parse_header_params(content_type or "")
    if main != "multipart/form-data" or "boundary" not in params:
        raise ValueError("not multipart/form-data")
    boundary = params["boundary"].encode("utf-8")
    delimiter = b"--" + boundary
    parts: list[Part] = []
    # Split on the delimiter; the first chunk is the preamble, the last is the epilogue after "--".
    chunks = body.split(delimiter)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        # strip leading CRLF and the trailing CRLF that precedes the next delimiter
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = chunk.partition(b"\n\n")
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        _, dparams = _parse_header_params(disp)
        name = dparams.get("name", "")
        filename = dparams.get("filename")
        ctype = headers.get("content-type", "application/octet-stream" if filename else "text/plain")
        parts.append(Part(name=name, filename=filename, content_type=ctype, data=data))
    return parts


# ----------------------------------------------------------------------------- request / response
class Request:
    def __init__(self, method: str, path: str, query: dict[str, str], headers: dict[str, str], body: bytes,
                 client_ip: str, raw_query: str = ""):
        self.method = method
        self.path = path
        self.query = query
        self.raw_query = raw_query
        self.headers = headers
        self.body = body
        self.client_ip = client_ip
        self._json: Any = None
        self._json_loaded = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        if not self._json_loaded:
            self._json = json.loads(self.body.decode("utf-8")) if self.body else {}
            self._json_loaded = True
        return self._json

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def multipart(self) -> list[Part]:
        return parse_multipart(self.body, self.content_type)

    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in self.headers.get("cookie", "").split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                out[k] = urllib.parse.unquote(v)
        return out

    def bearer(self) -> str:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def is_loopback(self) -> bool:
        return self.client_ip in LOOPBACK


class Response:
    def __init__(self, status: int = 200, headers: list[tuple[str, str]] | None = None,
                 body: bytes = b"", stream: Iterator[bytes] | None = None):
        self.status = status
        self.headers = headers or []
        self.body = body
        self.stream = stream

    @staticmethod
    def json(obj: Any, status: int = 200, headers: list[tuple[str, str]] | None = None) -> "Response":
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        return Response(status, [("Content-Type", "application/json; charset=utf-8")] + (headers or []), data)

    @staticmethod
    def error(status: int, message: str, **extra) -> "Response":
        return Response.json({"error": message, "status": status, **extra}, status=status)

    @staticmethod
    def text(text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return Response(status, [("Content-Type", content_type)], text.encode("utf-8"))

    @staticmethod
    def html(text: str, status: int = 200) -> "Response":
        return Response.text(text, status, "text/html; charset=utf-8")

    @staticmethod
    def bytes(data: bytes, content_type: str, status: int = 200, headers: list[tuple[str, str]] | None = None) -> "Response":
        return Response(status, [("Content-Type", content_type)] + (headers or []), data)

    @staticmethod
    def file(path: Path, download_name: str | None = None, content_type: str | None = None) -> "Response":
        path = Path(path)
        if not path.is_file():
            return Response.error(404, "file not found")
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        headers = [("Content-Type", ctype), ("Cache-Control", "no-cache")]
        if download_name:
            headers.append(("Content-Disposition", f'attachment; filename="{download_name}"'))
        return Response(200, headers, path.read_bytes())

    @staticmethod
    def redirect(location: str, status: int = 302) -> "Response":
        return Response(status, [("Location", location)], b"")

    @staticmethod
    def sse(iterator: Iterable[bytes]) -> "Response":
        return Response(200, [("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache"),
                              ("X-Accel-Buffering", "no")], stream=iter(iterator))


# ----------------------------------------------------------------------------- static resources
_STATIC_CACHE: dict[str, tuple[bytes, str]] = {}
_STATIC_LOCK = threading.Lock()


def _web_root():
    return resources.files("kyora").joinpath("web")


def read_resource(rel: str) -> bytes | None:
    """Read a file under ``kyora/web`` (works from a source tree or a zipapp)."""
    rel = rel.strip("/")
    if ".." in rel.split("/"):
        return None
    with _STATIC_LOCK:
        cached = _STATIC_CACHE.get(rel)
    if cached:
        return cached[0]
    node = _web_root()
    for part in rel.split("/"):
        node = node.joinpath(part)
    try:
        if not node.is_file():
            return None
        data = node.read_bytes()
    except (FileNotFoundError, OSError, NotADirectoryError):
        return None
    with _STATIC_LOCK:
        _STATIC_CACHE[rel] = (data, mimetypes.guess_type(rel)[0] or "application/octet-stream")
    return data


def clear_resource_cache() -> None:
    with _STATIC_LOCK:
        _STATIC_CACHE.clear()


def resource_response(rel: str, cache: str = "no-cache") -> Response:
    data = read_resource(rel)
    if data is None:
        return Response.error(404, "not found")
    ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    return Response(200, [("Content-Type", ctype), ("Cache-Control", cache)], data)


# ----------------------------------------------------------------------------- router
Handler = Callable[[Request, dict], Response]
_PARAM_RE = re.compile(r"{(\w+)(?::(path))?}")


@dataclass
class Route:
    method: str
    pattern: str
    regex: re.Pattern
    handler: Handler
    auth: str = "local"
    params: list[str] = field(default_factory=list)


class Router:
    def __init__(self):
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, *, auth: str = "local") -> None:
        names: list[str] = []

        def repl(m):
            names.append(m.group(1))
            return r"(?P<%s>.+)" % m.group(1) if m.group(2) == "path" else r"(?P<%s>[^/]+)" % m.group(1)

        regex = re.compile("^" + _PARAM_RE.sub(repl, pattern) + "$")
        self.routes.append(Route(method.upper(), pattern, regex, handler, auth, names))

    def match(self, method: str, path: str) -> tuple[Route | None, dict, bool]:
        """Return ``(route, params, path_exists)``; ``path_exists`` is True when another method matches."""
        path_exists = False
        for r in self.routes:
            m = r.regex.match(path)
            if m:
                if r.method == method.upper() or (r.method == "GET" and method.upper() == "HEAD"):
                    return r, {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}, True
                path_exists = True
        return None, {}, path_exists


# ----------------------------------------------------------------------------- handler
class KyoraHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Kyora/0.1"
    sys_version = ""
    app = None  # set by KyoraServer
    router: Router = None  # type: ignore

    def log_message(self, fmt, *args):  # quiet by default
        log.debug("%s - %s", self.address_string(), fmt % args)

    # auth ---------------------------------------------------------------------
    def _authorized(self, route: Route, req: Request) -> bool:
        level = route.auth
        if level == "any":
            return True
        token = getattr(getattr(self.app, "config", None), "hub", None)
        token = token.token if token else ""
        presented = req.bearer() or req.cookies().get("kyora_token", "") or req.query.get("token", "")
        token_ok = bool(token) and bool(presented) and hmac.compare_digest(token, presented)
        if level == "hub":
            return token_ok
        if level == "loopback":
            return req.is_loopback()
        # "local"
        if req.is_loopback():
            return True
        expose = bool(self.app and self.app.config.hub.expose_ui)
        return token_ok and (expose or route.pattern.startswith("/api/code-engine"))

    # dispatch -----------------------------------------------------------------
    def _dispatch(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path or "/"
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            self._send(Response.error(413, "body too large"))
            return
        body = self.rfile.read(length) if length else b""
        req = Request(self.command, path, query, headers, body, self.client_address[0], parsed.query)
        route, params, exists = self.router.match(self.command, path)
        if route is None:
            self._send(Response.error(405 if exists else 404, "method not allowed" if exists else "not found"))
            return
        if not self._authorized(route, req):
            self._send(Response.error(401 if route.auth == "hub" else 403, "unauthorized"))
            return
        try:
            resp = route.handler(req, params)
        except ValueError as exc:
            resp = Response.error(400, str(exc))
        except PermissionError as exc:
            resp = Response.error(403, str(exc))
        except FileNotFoundError as exc:
            resp = Response.error(404, str(exc) or "not found")
        except Exception as exc:  # noqa: BLE001
            log.error("handler error %s %s: %s\n%s", self.command, path, exc, traceback.format_exc())
            resp = Response.error(500, f"{type(exc).__name__}: {exc}")
        self._send(resp)

    def _send(self, resp: Response):
        if resp.stream is not None:
            self.send_response(resp.status)
            for k, v in resp.headers:
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in resp.stream:
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                pass
            finally:
                close = getattr(resp.stream, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
            self.close_connection = True
            return
        body = b"" if self.command == "HEAD" else resp.body
        self.send_response(resp.status)
        for k, v in resp.headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch


class KyoraServer:
    """Owns the ThreadingHTTPServer and its serving thread."""

    def __init__(self, app, host: str, port: int, router: Router | None = None):
        self.app = app
        self.host = host
        self.port = port
        self.router = router or Router()
        handler = type("BoundKyoraHandler", (KyoraHandler,), {"app": app, "router": self.router})
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "", "::") else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


# ----------------------------------------------------------------------------- SSE stream helper
def event_stream(bus, *, run_id: str | None, project_id: str | None, types: list[str] | None, since: int,
                 keepalive_s: float = 15.0, stop: threading.Event | None = None) -> Iterator[bytes]:
    """Replay persisted events after ``since`` then stream live events with keepalives."""
    sub = bus.subscribe(run_id=run_id, project_id=project_id, types=types)
    try:
        last = since
        for ev in bus.replay(since, run_id=run_id, project_id=project_id, types=types):
            last = ev.id
            yield encode_event(ev.to_dict(), event=ev.type, event_id=ev.id)
        yield b": connected\n\n"
        last_send = time.monotonic()
        while stop is None or not stop.is_set():
            try:
                ev = sub.queue.get(timeout=1.0)
            except Exception:
                ev = None
                if time.monotonic() - last_send >= keepalive_s:
                    yield keepalive()
                    last_send = time.monotonic()
                continue
            if ev is None:
                break
            if ev.id and ev.id <= last:
                continue  # already replayed
            if ev.id:
                last = ev.id
            yield encode_event(ev.to_dict(), event=ev.type, event_id=ev.id or None)
            last_send = time.monotonic()
    finally:
        sub.close()


def lan_addresses() -> list[str]:
    """Best-effort list of non-loopback IPv4 addresses of this host."""
    addrs: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in addrs:
                addrs.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127.") and ip not in addrs:
            addrs.insert(0, ip)
    except Exception:
        pass
    return addrs
