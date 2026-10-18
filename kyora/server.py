"""HTTP server (standard library): router, multipart parser, SSE streaming, static assets.

Design notes
- ``ThreadingHTTPServer`` with HTTP/1.1 keep-alive for normal responses; SSE
  responses send ``Connection: close`` and stream until the client leaves.
- Access levels per route: ``any`` (static assets), ``local`` (loopback client
  OR a valid hub token in ``Authorization: Bearer``/``kyora_token`` cookie),
  ``loopback`` (loopback client only, e.g. the judge hook endpoint), ``hub``
  (valid token required).
- Before routing, every request passes the origin guard: the ``Host`` header
  must name this machine (loopback, its LAN names when bound beyond loopback,
  or ``server.allowed_hosts``) so DNS rebinding cannot reach the API, and a
  state-changing request that carries an ``Origin`` must come from this
  listener's own origin. No ``Access-Control-Allow-*`` header is ever sent.
- Two listeners: the main one (UI + API) and the preview listener, a separate
  origin that serves only untrusted game code, the test harness and the
  browser-results POSTs, so game JavaScript never runs on the API's origin.
- Static files come from the ``kyora/web`` package directory via
  ``importlib.resources`` so they also work inside ``kyora.pyz``.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import socketserver
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

from .util.mime import guess_type as guess_mime
from .util.sse import encode_event, keepalive

log = logging.getLogger("kyora.server")
IS_WINDOWS = os.name == "nt"
MAX_BODY = 256 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}  # Host names that always mean this machine
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_HOST_RE = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9._-]+))(?::\d{1,5})?$")


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
            # A body must be declared JSON: browsers send text/plain cross-origin without a CORS preflight.
            if self.body and not is_json_type(self.content_type):
                raise ValueError("Content-Type must be application/json")
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


def is_json_type(content_type: str) -> bool:
    """True for ``application/json`` and ``application/<x>+json`` (parameters such as charset ignored)."""
    main, _ = _parse_header_params(content_type or "")
    return main == "application/json" or (main.startswith("application/") and main.endswith("+json"))


def parse_host(value: str) -> str | None:
    """Host name of a ``Host``/``host:port`` value (lower case, no port, ``[::1]`` -> ``::1``); None if malformed."""
    m = _HOST_RE.match((value or "").strip())
    if not m:
        return None
    return (m.group("v6") or m.group("name") or "").lower().rstrip(".") or None


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
        ctype = content_type or guess_mime(str(path)) or "application/octet-stream"
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


def safe_resource_rel(rel: str) -> str | None:
    """``a/b.js`` for a safe path under ``kyora/web``; None for traversal or anything not a plain relative path.

    Both ``/`` and ``\\`` separate segments (``WindowsPath.joinpath`` treats a backslash as a
    separator); ``..``, ``.``, empty segments, drive letters/streams (``:``) and NUL are rejected.
    """
    rel = (rel or "").strip("/")
    parts = re.split(r"[\\/]", rel)
    if not rel or any(p in ("", ".", "..") or ":" in p or "\x00" in p for p in parts):
        return None
    return "/".join(parts)


def read_resource(rel: str) -> bytes | None:
    """Read a file under ``kyora/web`` (works from a source tree or a zipapp)."""
    rel = safe_resource_rel(rel)
    if rel is None:
        return None
    with _STATIC_LOCK:
        cached = _STATIC_CACHE.get(rel)
    if cached:
        return cached[0]
    root = node = _web_root()
    for part in rel.split("/"):
        node = node.joinpath(part)
    try:
        if isinstance(node, Path):  # filesystem install: the resolved file must stay inside kyora/web
            node.resolve().relative_to(Path(root).resolve())
        if not node.is_file():
            return None
        data = node.read_bytes()
    except (FileNotFoundError, OSError, NotADirectoryError, ValueError):
        return None
    with _STATIC_LOCK:
        _STATIC_CACHE[rel] = (data, guess_mime(rel) or "application/octet-stream")
    return data


def clear_resource_cache() -> None:
    with _STATIC_LOCK:
        _STATIC_CACHE.clear()


def resource_response(rel: str, cache: str = "no-cache") -> Response:
    rel = safe_resource_rel(rel) or ""
    data = read_resource(rel) if rel else None
    if data is None:
        return Response.error(404, "not found")
    ctype = guess_mime(rel) or "application/octet-stream"
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
    frame_options: str | None = None  # X-Frame-Options for HTML (the main listener: SAMEORIGIN)

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

    # origin guard -------------------------------------------------------------
    def _host_allowed(self, name: str) -> bool:
        """Loopback names, ``server.allowed_hosts``, the address the request arrived on, and the LAN
        names of this host when the listener is bound beyond loopback."""
        if name in LOOPBACK_NAMES:
            return True
        server_cfg = getattr(getattr(self.app, "config", None), "server", None)
        if name in {parse_host(h) for h in (getattr(server_cfg, "allowed_hosts", None) or [])}:
            return True
        try:
            local = str(self.connection.getsockname()[0])
        except (OSError, AttributeError):
            local = ""
        if name == local.removeprefix("::ffff:"):
            return True
        return str(self.server.server_address[0]) not in LOOPBACK and name in local_host_names()

    def _origin_allowed(self, origin: str, raw_host: str) -> bool:
        """This listener's origin ``http://<allowed host>:<port>``, or (behind a proxy) the Host the browser used."""
        try:
            o = urllib.parse.urlsplit(origin)
            port = o.port
        except ValueError:
            return False
        if o.scheme not in ("http", "https") or not o.hostname or o.username or o.path or o.query:
            return False  # includes "null" (sandboxed documents, file: pages)
        if not self._host_allowed(o.hostname.rstrip(".")):
            return False
        if o.scheme == "http" and (port or 80) == self.server.server_address[1]:
            return True
        return o.netloc.lower() == raw_host.strip().lower()

    def _guard(self, req: Request) -> Response | None:
        """403 for a foreign Host (DNS rebinding) or a cross-origin state-changing request; None when fine."""
        raw_host = req.headers.get("host")
        if raw_host is not None:
            name = parse_host(raw_host)
            if name is None or not self._host_allowed(name):
                return Response.error(403, f"host not allowed: {(name or raw_host.strip())[:120]}")
        origin = req.headers.get("origin")
        if req.method.upper() not in SAFE_METHODS and origin is not None \
                and not self._origin_allowed(origin.strip(), raw_host or ""):
            return Response.error(403, "cross-origin request refused")
        return None

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
        blocked = self._guard(req)
        if blocked is not None:
            self._send(blocked)
            return
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
        headers = list(resp.headers)
        if self.frame_options and any(k.lower() == "content-type" and v.lower().startswith("text/html") for k, v in headers) \
                and not any(k.lower() == "x-frame-options" for k, _ in headers):
            headers.append(("X-Frame-Options", self.frame_options))
        self.send_response(resp.status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch


def _bound_handler(app, router: Router, frame_options: str | None) -> type:
    return type("BoundKyoraHandler", (KyoraHandler,), {"app": app, "router": router, "frame_options": frame_options})


class _HTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that owns its port exclusively.

    ``SO_REUSEADDR`` lets a restarted Kyora rebind a port still in TIME_WAIT on POSIX, where a
    second bind to a listening port still fails. On Windows the same option lets a second process
    bind a port that another socket is listening on, so a second Kyora would start silently next
    to the first; there the option stays off and ``SO_EXCLUSIVEADDRUSE`` makes the bind fail.
    """
    daemon_threads = True
    allow_reuse_address = True
    allow_reuse_port = False  # never SO_REUSEPORT: it lets a second process share the port

    def server_bind(self):
        if IS_WINDOWS:
            self.allow_reuse_address = False
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # socketserver's bind without HTTPServer.server_bind's socket.getfqdn(host): that reverse lookup ran
        # between bind() and listen() and took tens of seconds on macOS runners (connections timed out
        # meanwhile). server_name only feeds CGI variables, which Kyora does not use.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class KyoraServer:
    """Owns the main listener, the preview listener and their serving threads.

    The preview listener is a second origin (same bind address) for untrusted game code. Its
    routes come from ``preview_router`` or, when the app has ``register_preview_routes``, from
    the app. ``preview_port`` None reads ``app.config.server.preview_port``; None there means
    main port + 1 (an ephemeral port when that one is busy or the main port is 0). The app's
    ``preview_url`` is set to the listener's loopback base so the main listener can redirect.
    """

    def __init__(self, app, host: str, port: int, router: Router | None = None, *,
                 preview_port: int | None = None, preview_router: Router | None = None):
        self.app = app
        self.host = host
        self.port = port
        self.router = router or Router()
        self.httpd = _HTTPServer((host, port), _bound_handler(app, self.router, "SAMEORIGIN"))
        self.port = self.httpd.server_address[1]
        self.preview_httpd: ThreadingHTTPServer | None = None
        self.preview_port: int | None = None
        self._threads: list[threading.Thread] = []
        self._serving: set[int] = set()
        if preview_router is None and callable(getattr(type(app), "register_preview_routes", None)):
            preview_router = Router()
            app.register_preview_routes(preview_router, self.router)
        if preview_router is not None:
            try:
                self.preview_httpd = self._bind_preview(app, host, port, preview_port, preview_router)
            except BaseException:
                self.httpd.server_close()
                raise
            self.preview_port = self.preview_httpd.server_address[1]
            if hasattr(app, "preview_url"):
                app.preview_url = self.preview_url

    @staticmethod
    def _bind_preview(app, host: str, port: int, preview_port: int | None, router: Router) -> ThreadingHTTPServer:
        handler = _bound_handler(app, router, None)  # the harness page is framed by the main UI
        if preview_port is None:
            preview_port = getattr(getattr(getattr(app, "config", None), "server", None), "preview_port", None)
        auto = preview_port is None
        if auto:
            preview_port = port + 1 if 0 < port < 65535 else 0
        try:
            return _HTTPServer((host, preview_port), handler)
        except OSError as exc:
            if not auto or preview_port == 0:
                raise
            log.warning("preview port %s is not available (%s); using an ephemeral port", preview_port, exc)
            return _HTTPServer((host, 0), handler)

    @staticmethod
    def _base(host: str, port: int) -> str:
        host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"

    @property
    def url(self) -> str:
        return self._base(self.host, self.port)

    @property
    def preview_url(self) -> str:
        """Loopback base of the preview listener ("" without one)."""
        return self._base(self.host, self.preview_port) if self.preview_httpd is not None else ""

    def _start_thread(self, httpd: ThreadingHTTPServer, name: str) -> None:
        self._serving.add(id(httpd))
        t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def start(self) -> None:
        if self.preview_httpd is not None:
            self._start_thread(self.preview_httpd, "kyora-preview")
        self._start_thread(self.httpd, "kyora-server")

    def serve_forever(self) -> None:
        if self.preview_httpd is not None:
            self._start_thread(self.preview_httpd, "kyora-preview")
        self._serving.add(id(self.httpd))
        self.httpd.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        for httpd in (self.httpd, self.preview_httpd):
            if httpd is None:
                continue
            try:
                if id(httpd) in self._serving:  # shutdown() of a never-started loop would wait forever
                    httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        self._serving.clear()


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


_NAMES_LOCK = threading.Lock()
_NAMES_CACHE: dict = {"at": 0.0, "names": frozenset()}
_LAN_CACHE: dict = {"at": 0.0, "addrs": None}
# Resolving this host's own name can block for seconds: macOS answers "<name>.local" over mDNS
# (the GitHub macOS runners took >5 s, which made every GET /api/status time out). Name lookups
# therefore run with a time budget and results are cached; the UDP route probe needs no DNS.
DNS_BUDGET_S = 1.0


def _bounded(fn, budget_s: float = DNS_BUDGET_S):
    """``fn()`` in a daemon thread; its result, or None when it raised or did not finish in ``budget_s``."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception:  # noqa: BLE001 - a failed lookup is just "no answer"
            box["value"] = None

    t = threading.Thread(target=run, name="kyora-dns", daemon=True)
    t.start()
    t.join(budget_s)
    return box.get("value")


def local_host_names(ttl_s: float = 60.0) -> frozenset:
    """Names of this host for the Host allow-list: LAN addresses, hostname, ``<hostname>.local``, FQDN.

    Cached for ``ttl_s``; ``getfqdn`` runs within :data:`DNS_BUDGET_S`. Only consulted when a
    listener is bound beyond loopback and the Host is not otherwise allowed.
    """
    now = time.monotonic()
    with _NAMES_LOCK:
        if _NAMES_CACHE["names"] and now - _NAMES_CACHE["at"] < ttl_s:
            return _NAMES_CACHE["names"]
    names = set(lan_addresses())
    try:
        hostname = socket.gethostname()
        names.update({hostname, hostname + ".local"})
        fqdn = _bounded(socket.getfqdn)
        if fqdn:
            names.add(fqdn)
    except OSError:
        pass
    out = frozenset(n.lower().rstrip(".") for n in names if n)
    with _NAMES_LOCK:
        _NAMES_CACHE.update(at=now, names=out)
    return out


def lan_addresses(ttl_s: float = 30.0) -> list[str]:
    """Best-effort list of non-loopback IPv4 addresses of this host (cached for ``ttl_s``)."""
    now = time.monotonic()
    with _NAMES_LOCK:
        if _LAN_CACHE["addrs"] is not None and now - _LAN_CACHE["at"] < ttl_s:
            return list(_LAN_CACHE["addrs"])
    addrs: list[str] = []
    try:  # the address of the default route: no DNS, no packet sent
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if not ip.startswith("127.") and ip != "0.0.0.0":
            addrs.append(ip)
    except Exception:
        pass
    infos = _bounded(lambda: socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)) or []
    for info in infos:
        ip = info[4][0]
        if not ip.startswith("127.") and ip not in addrs:
            addrs.append(ip)
    with _NAMES_LOCK:
        _LAN_CACHE.update(at=now, addrs=list(addrs))
    return addrs
