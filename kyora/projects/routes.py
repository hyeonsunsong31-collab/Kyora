"""HTTP routes for projects, attachments, slides, previews, the test harness page and the deck.

All routes use ``auth='local'`` (loopback, or a valid hub token when the UI is
exposed). Game files and the harness are untrusted code: they are served only by
the preview listener (a separate origin, :func:`register_preview_routes`); on the
main listener ``/preview/...`` and ``/runner/...`` redirect there. Response shapes:

* ``GET /api/projects`` -> ``{"projects": [project]}``
* ``POST /api/projects`` ``{name}`` -> 201 ``{"project": project}``
* ``GET /api/projects/{id}`` -> ``{"project", "slides", "runs": [run summary]}``
* ``DELETE /api/projects/{id}`` -> ``{"ok": true, "deleted": id}`` or 409 while a run is active
* ``POST /api/projects/{id}/attachments`` (multipart ``file``..., ``role``) -> 201 ``{"attachments": [...]}``
* ``GET /api/projects/{id}/attachments/{aid}`` -> the file (``?download=1`` forces a download)
* ``GET /api/projects/{id}/slides`` -> ``{"slides", "current_slide", "repo"}``
* ``GET /api/projects/{id}/slides/{n}/download`` -> ``<slug>-v<n>.zip``
* ``GET /api/projects/{id}/preview-check?ref=`` -> ``{"ok", "ref", "error"}`` (is there an index.html to preview?)
* ``POST /api/projects/{id}/import`` (multipart ``file`` zip, optional ``parent``) -> 201 ``{"slide", "files", "project"}``
* ``GET /preview/{id}/{ref}/{path}`` with ref ``v<n>`` | ``work`` | ``run-<rid>`` (preview listener)
* ``GET /runner/{id}/{ref}`` -> the browser test harness page (preview listener)
* ``GET /projects/{id}/deck`` (``?slide=n``, ``?download=1``) -> deck HTML
* ``POST /api/projects/{id}/slides/{n}/browser-results`` -> ``{"ok": true, ...}``
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

from ..pipeline.events import SLIDE_CREATED, TEST_BROWSER_RESULT
from ..server import Request, Response, Router, parse_host, resource_response
from ..util import mime
from ..util.fs import atomic_write_json, safe_join
from ..util.ids import now_iso, slug
from .deck import CSP as DECK_CSP
from .deck import DeckBuilder
from .slides import SlideManager, VersionError
from .store import ProjectStore, valid_id

log = logging.getLogger("kyora.projects.routes")

MAX_BROWSER_RESULT_BYTES = 32 * 1024 * 1024
_SLIDE_REF_RE = re.compile(r"^v(\d{1,6})$")
_PREVIEW_MIME = mime.TABLE
_UTF8_TYPES = ("application/javascript", "application/json", "application/xml", "image/svg+xml", "model/gltf+json")
_INLINE_ATTACHMENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
                            "text/plain", "application/pdf"}


def preview_content_type(name: str) -> str:
    """Content type for a game file (Kyora's table, never the Windows registry), with charset for text."""
    ext = Path(name).suffix.lower()
    ctype = _PREVIEW_MIME.get(ext) or mime.guess_type(name) or "application/octet-stream"
    if ctype.startswith("text/") or ctype in _UTF8_TYPES:
        ctype += "; charset=utf-8"
    return ctype


def _content_disposition(kind: str, filename: str) -> str:
    ascii_name = "".join(ch if (32 <= ord(ch) < 127 and ch not in '"\\') else "_" for ch in filename) or "download"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"


def _slide_num(value: str) -> int:
    try:
        n = int(str(value).strip().lstrip("vV"))
    except (TypeError, ValueError):
        raise FileNotFoundError(f"slide not found: {value}") from None
    if n < 1:
        raise FileNotFoundError(f"slide not found: {value}")
    return n


class ProjectRoutes:
    """Route handlers bound to the application (see module docstring for shapes)."""

    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------------ helpers
    @property
    def store(self) -> ProjectStore:
        store = getattr(self.app, "store", None)
        if store is None:
            reason = (getattr(self.app, "missing", {}) or {}).get("store", "project store unavailable")
            raise _Unavailable(reason)
        return store

    def _project(self, pid: str):
        if not valid_id(pid):
            raise FileNotFoundError(f"project not found: {pid}")
        return self.store.get(pid)

    def _publish(self, type_: str, data: dict, project_id: str, run_id: str | None = None) -> None:
        bus = getattr(self.app, "bus", None)
        if bus is None:
            return
        try:
            bus.publish(type_, data, run_id=run_id, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 - an event failure must not fail the request
            log.warning("publishing %s failed: %s", type_, exc)

    def active_runs(self, pid: str) -> list[str]:
        """Ids of runs of ``pid`` that the run manager reports as running or pending."""
        rm = getattr(self.app, "run_manager", None)
        if rm is None or not hasattr(rm, "status"):
            return []
        try:
            status = rm.status() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("run_manager.status() failed: %s", exc)
            return []
        ids: list[str] = []
        if isinstance(status, dict):
            for key in ("running", "pending", "queued", "active"):
                for item in status.get(key) or []:
                    rid = item if isinstance(item, str) else (item.get("id") or item.get("run_id")) if isinstance(item, dict) else None
                    if isinstance(rid, str) and rid not in ids:
                        ids.append(rid)
        out = []
        for rid in ids:
            try:
                if self.store.get_run(rid).project_id == pid:
                    out.append(rid)
            except (FileNotFoundError, RuntimeError):
                continue
        return out

    # ------------------------------------------------------------------ projects
    def list_projects(self, req: Request, params: dict) -> Response:
        return Response.json({"projects": [p.to_dict() for p in self.store.list()]})

    def create_project(self, req: Request, params: dict) -> Response:
        body = req.json() if req.body else {}
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object {name}")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        project = self.store.create(name)
        return Response.json({"project": project.to_dict()}, status=201)

    def get_project(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        runs = [r.summary() for r in self.store.list_runs(p.id, limit=50)]
        slides = sorted(p.slides, key=lambda s: int(s.get("number") or 0))
        return Response.json({"project": p.to_dict(), "slides": slides, "runs": runs})

    def delete_project(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        active = self.active_runs(p.id)
        if active:
            return Response.error(409, "a run is active for this project; cancel it first", runs=active)
        self.store.delete(p.id)
        return Response.json({"ok": True, "deleted": p.id})

    # ------------------------------------------------------------------ attachments
    def upload_attachments(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        parts = req.multipart()
        role = "reference"
        for part in parts:
            if part.name == "role" and part.filename is None:
                role = part.text().strip() or "reference"
        files = [part for part in parts if part.name in ("file", "files") and part.filename is not None]
        if not files:
            raise ValueError("no file parts named 'file' in the upload")
        added = [self.store.add_attachment(p.id, part.filename or "file", part.content_type, part.data, role).to_dict()
                 for part in files]
        return Response.json({"attachments": added}, status=201)

    def get_attachment(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        att = p.attachment(params["aid"])
        if att is None:
            raise FileNotFoundError("attachment not found")
        path = self.store.attachment_path(p.id, att.id)
        inline = att.mime in _INLINE_ATTACHMENT_TYPES and req.query.get("download") != "1"
        ctype = att.mime if inline else "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        headers = [("Content-Type", ctype), ("Cache-Control", "private, no-cache"),
                   ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "sandbox"),
                   ("Content-Disposition", _content_disposition("inline" if inline else "attachment", att.name))]
        return Response(200, headers, path.read_bytes())

    # ------------------------------------------------------------------ slides
    def list_slides(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        slides = sorted(p.slides, key=lambda s: int(s.get("number") or 0))
        return Response.json({"slides": slides, "current_slide": p.current_slide, "repo": p.repo})

    def download_slide(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        n = _slide_num(params["n"])
        if p.slide(n) is None:
            raise FileNotFoundError(f"slide v{n} not found")
        path = SlideManager.for_project(self.store, p).export_zip(n)
        name = f"{slug(p.name)}-v{n}.zip"
        return Response(200, [("Content-Type", "application/zip"), ("Cache-Control", "no-store"),
                              ("Content-Disposition", _content_disposition("attachment", name))], path.read_bytes())

    def import_zip(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        parts = req.multipart()
        upload = next((part for part in parts if part.name == "file" and part.filename is not None), None)
        if upload is None:
            raise ValueError("no ZIP file part named 'file' in the upload")
        parent = None
        for part in parts:
            if part.name == "parent" and part.filename is None and part.text().strip():
                parent = _slide_num(part.text())
                if p.slide(parent) is None:
                    raise ValueError(f"parent slide v{parent} does not exist")
        active = self.active_runs(p.id)
        if active:
            return Response.error(409, "a run is active for this project; import when it has finished", runs=active)
        sm = SlideManager.for_project(self.store, p)
        slide, files = sm.import_zip(upload.data, upload.filename or "game.zip", parent=parent)
        self._publish(SLIDE_CREATED, {"number": slide["number"], "tag": slide["tag"], "sha": slide["sha"],
                                      "title": slide["title"], "imported": True}, p.id)
        return Response.json({"slide": slide, "files": len(files), "file_list": files[:400],
                              "project": self.store.get(p.id).to_dict()}, status=201)

    def slide_browser_results(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        n = _slide_num(params["n"])
        if p.slide(n) is None:
            raise FileNotFoundError(f"slide v{n} not found")
        if len(req.body) > MAX_BROWSER_RESULT_BYTES:
            raise ValueError("browser result too large")
        payload = req.json()
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        rel = f"cache/browser-results/v{n}.json"
        out = self.store.paths(p.id).cache / "browser-results" / f"v{n}.json"
        atomic_write_json(out, {**payload, "received_at": now_iso(), "slide": n})
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else None
        self._publish(TEST_BROWSER_RESULT, {"summary": summary, "ref": f"v{n}", "slide": n}, p.id)
        return Response.json({"ok": True, "slide": n, "path": rel, "summary": summary})

    # ------------------------------------------------------------------ preview / runner / deck
    def _preview_root(self, pid: str, ref: str) -> Path:
        p = self._project(pid)
        m = _SLIDE_REF_RE.match(ref)
        if m:
            n = int(m.group(1))
            if p.slide(n) is None:
                raise FileNotFoundError(f"slide v{n} not found")
            return SlideManager.for_project(self.store, p).materialize(n)
        if ref == "work":
            return self.store.paths(p.id).repo
        if ref.startswith("run-") and valid_id(ref[4:]):
            run = self.store.get_run(ref[4:])
            if run.project_id != p.id:
                raise FileNotFoundError("run not found")
            return self.store.paths(p.id).repo
        raise FileNotFoundError(f"unknown preview ref {ref!r}")

    def preview(self, req: Request, params: dict) -> Response:
        rel = params.get("path", "") or ""
        try:
            root = self._preview_root(params["id"], params["ref"])
        except VersionError as exc:
            log.warning("preview materialize failed: %s", exc)
            return Response.error(404, "slide could not be materialized", detail=str(exc))
        parts = [s for s in rel.split("/") if s not in ("", ".")]
        if any(s.lower() == ".git" or "\\" in s or ":" in s or "\x00" in s for s in parts):
            return Response.error(404, "not found")
        try:
            target = safe_join(root, *parts) if parts else root.resolve()
        except PermissionError:
            return Response.error(404, "not found")
        if target.is_dir():
            if parts and not req.path.endswith("/"):
                loc = req.path + "/" + (("?" + req.raw_query) if req.raw_query else "")
                return Response.redirect(loc, 301)
            target = target / "index.html"
        if not target.is_file():
            return Response.error(404, "not found")
        return Response(200, [("Content-Type", preview_content_type(target.name)), ("Cache-Control", "no-store"),
                              ("X-Content-Type-Options", "nosniff")], target.read_bytes())

    def preview_bare(self, req: Request, params: dict) -> Response:
        loc = req.path + "/" + (("?" + req.raw_query) if req.raw_query else "")
        return Response.redirect(loc, 301)

    def to_preview_origin(self, req: Request, params: dict) -> Response:
        """Main listener: game bytes never render on the API's origin; send the browser to the preview listener."""
        port = getattr(self.app, "preview_port", None)
        if not port:
            return Response.error(503, "the preview listener is not running")
        host = parse_host(req.headers.get("host") or "") or "127.0.0.1"  # the Host passed the allow-list
        host = f"[{host}]" if ":" in host else host
        return Response.redirect(f"http://{host}:{port}{req.path}" + (("?" + req.raw_query) if req.raw_query else ""))

    def preview_check(self, req: Request, params: dict) -> Response:
        """Whether ``ref`` has an ``index.html`` to preview (the UI cannot read the cross-origin frame)."""
        self._project(params["id"])
        ref = req.query.get("ref") or "work"
        try:
            root = self._preview_root(params["id"], ref)
            ok = (root / "index.html").is_file()
            error = None if ok else "no index.html"
        except (FileNotFoundError, VersionError) as exc:
            ok, error = False, str(exc) or "not found"
        return Response.json({"ok": ok, "ref": ref, "error": error})

    def runner(self, req: Request, params: dict) -> Response:
        self._project(params["id"])
        resp = resource_response("runner/harness.html")
        resp.headers = [(k, v) for k, v in resp.headers if k.lower() != "cache-control"] + [("Cache-Control", "no-store")]
        return resp

    def deck(self, req: Request, params: dict) -> Response:
        p = self._project(params["id"])
        slide_q = req.query.get("slide")
        n = _slide_num(slide_q) if slide_q else None
        html_text = DeckBuilder(p, self.store).build(n)
        headers = [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                   ("Content-Security-Policy", DECK_CSP), ("X-Content-Type-Options", "nosniff")]
        if req.query.get("download") == "1":
            name = f"{slug(p.name)}-deck" + (f"-v{n}" if n else "") + ".html"
            headers.append(("Content-Disposition", _content_disposition("attachment", name)))
        return Response(200, headers, html_text.encode("utf-8"))


class _Unavailable(Exception):
    """The project store is not available (component failed to start)."""


def _wrap(fn):
    def handler(req: Request, params: dict) -> Response:
        try:
            return fn(req, params)
        except _Unavailable as exc:
            return Response.error(503, f"project store unavailable: {exc}")
    handler.__name__ = getattr(fn, "__name__", "handler")
    handler.__doc__ = getattr(fn, "__doc__", None)
    return handler


def register_routes(router: Router, app) -> None:
    """Register the project, slide and deck routes (and redirects to the preview listener) on ``router``."""
    h = ProjectRoutes(app)
    add = lambda method, pattern, fn: router.add(method, pattern, _wrap(fn), auth="local")  # noqa: E731
    add("GET", "/api/projects", h.list_projects)
    add("POST", "/api/projects", h.create_project)
    add("GET", "/api/projects/{id}", h.get_project)
    add("DELETE", "/api/projects/{id}", h.delete_project)
    add("POST", "/api/projects/{id}/attachments", h.upload_attachments)
    add("GET", "/api/projects/{id}/attachments/{aid}", h.get_attachment)
    add("GET", "/api/projects/{id}/slides", h.list_slides)
    add("GET", "/api/projects/{id}/slides/{n}/download", h.download_slide)
    add("POST", "/api/projects/{id}/slides/{n}/browser-results", h.slide_browser_results)
    add("POST", "/api/projects/{id}/import", h.import_zip)
    add("GET", "/api/projects/{id}/preview-check", h.preview_check)
    add("GET", "/preview/{path:path}", h.to_preview_origin)
    add("GET", "/runner/{path:path}", h.to_preview_origin)
    add("GET", "/projects/{id}/deck", h.deck)


def register_preview_routes(router: Router, app) -> None:
    """Register the preview listener's game, harness and slide-result routes on ``router``."""
    h = ProjectRoutes(app)
    add = lambda method, pattern, fn: router.add(method, pattern, _wrap(fn), auth="local")  # noqa: E731
    add("GET", "/preview/{id}/{ref}/", h.preview)
    add("GET", "/preview/{id}/{ref}/{path:path}", h.preview)
    add("GET", "/preview/{id}/{ref}", h.preview_bare)
    add("GET", "/runner/{id}/{ref}", h.runner)
    router.add("GET", "/static/runner/{path:path}",
               lambda req, params: resource_response("runner/" + params["path"], cache="public, max-age=300"), auth="any")
    add("POST", "/api/projects/{id}/slides/{n}/browser-results", h.slide_browser_results)
