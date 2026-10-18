"""Composition root: builds every component and registers routes.

Components are imported lazily and guarded so the server can start (and
``/api/status`` can explain what is missing) even while parts of the system are
unavailable. Nothing here fakes a component: a missing engine is reported as
missing and the pipeline reports BLOCKED.
"""
from __future__ import annotations

import logging
import secrets
import threading
import urllib.parse
from pathlib import Path

from . import __version__
from .config import Config
from .models.base import select_engine
from .pipeline.events import EventBus
from .server import Request, Response, Router, event_stream, lan_addresses, resource_response
from .util.fs import kyora_home, platform_info

log = logging.getLogger("kyora.app")

# Main-listener routes the preview listener serves too: the harness posts results to its own origin.
# (The slide variant is registered by kyora.projects.routes.register_preview_routes.)
PREVIEW_SHARED_ROUTES = {("POST", "/api/runs/{id}/browser-results")}


class App:
    def __init__(self, config: Config, *, home: Path | None = None, hub_mode: bool = False):
        self.config = config
        self.home = Path(home) if home else kyora_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "logs").mkdir(exist_ok=True)
        self.hub_mode = hub_mode
        self.platform = platform_info()
        self.bus = EventBus(self.home / "kyora.db")
        self.missing: dict[str, str] = {}
        self.server_url: str = ""
        self.preview_url: str = ""  # loopback base of the preview listener (separate origin for game code)
        self.started = threading.Event()
        # components (populated by _wire)
        self.store = None
        self.nim_nemotron = None
        self.nim_kimi = None
        self.nemotron = None
        self.kimi = None
        self.judge = None
        self.claude_engine = None
        self.anthropic_engine = None
        self.hub_client = None
        self.hub_worker = None
        self.tokkio = None
        self.run_manager = None
        self.selfcheck = None
        self.bench = None
        self.browser_results = None
        self._wire()

    # ------------------------------------------------------------------ wiring
    def _try(self, name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self.missing[name] = f"{type(exc).__name__}: {exc}"
            log.warning("component %s unavailable: %s", name, exc)
            return None

    def _wire(self) -> None:
        cfg = self.config

        def _store():
            from .projects.store import ProjectStore
            return ProjectStore(self.home / "projects", self.home / "kyora.db")
        self.store = self._try("store", _store)

        def _nim():
            from .models.nim_client import NIMClient
            nv = cfg.nvidia
            return (NIMClient(nv.nemotron.base_url, nv.nemotron.api_key, user_agent=f"kyora/{__version__}",
                              key_label="nemotron key", alt_keys=nv.alt_keys("nemotron")),
                    NIMClient(nv.kimi.base_url, nv.kimi.api_key, user_agent=f"kyora/{__version__}",
                              key_label="kimi key", alt_keys=nv.alt_keys("kimi")))
        pair = self._try("nim_client", _nim)
        if pair:
            self.nim_nemotron, self.nim_kimi = pair

        def _nemotron():
            from .models.nemotron import Nemotron
            return Nemotron(self.nim_nemotron, cfg.nvidia.nemotron.model, self.bus, cfg.nemotron,
                            fallback_models=cfg.nvidia.nemotron.fallback_models)
        if self.nim_nemotron:
            self.nemotron = self._try("nemotron", _nemotron)

        def _kimi():
            from .models.kimi import Kimi
            return Kimi(self.nim_kimi, cfg.nvidia.kimi.model, cfg.kimi, self.bus,
                        fallback_models=cfg.nvidia.kimi.fallback_models)
        if self.nim_kimi:
            self.kimi = self._try("kimi", _kimi)

        def _judge():
            from .pipeline.judge import JudgeService
            return JudgeService(self.kimi, self.bus, cfg, self.home)
        self.judge = self._try("judge", _judge)

        def _claude():
            from .models.claude_code import ClaudeCodeEngine
            return ClaudeCodeEngine(cfg.claude_code, self.home, self)
        self.claude_engine = self._try("claude_code", _claude)

        def _anthropic():
            from .models.anthropic_api import AnthropicAPIEngine
            # callable: an API key added in Settings takes effect without a restart
            return AnthropicAPIEngine(lambda: cfg.anthropic_api_key, cfg.claude_code, self.judge, self.home)
        self.anthropic_engine = self._try("anthropic_api", _anthropic)

        def _hub_client():
            from .hub import HubClientEngine
            return HubClientEngine(lambda: cfg.hub, self.bus)  # live: url/token changed in Settings apply immediately
        self.hub_client = self._try("hub_client", _hub_client)

        def _tokkio():
            from .models.tokkio import Tokkio
            return Tokkio(lambda: cfg.tokkio, self.bus)  # live: URLs changed in Settings apply immediately
        self.tokkio = self._try("tokkio", _tokkio)

        def _browser_results():
            from .pipeline.quality import BrowserResultsCollector
            return BrowserResultsCollector(self.home)
        self.browser_results = self._try("browser_results", _browser_results)

        def _runs():
            from .pipeline.runner import RunManager
            return RunManager(self, workers=cfg.server.workers)
        self.run_manager = self._try("run_manager", _runs)

        def _selfcheck():
            from .selfcheck import SelfCheck
            return SelfCheck(self)
        self.selfcheck = self._try("selfcheck", _selfcheck)

        def _bench():
            from .bench.runner import BenchRunner
            return BenchRunner(self)
        self.bench = self._try("bench", _bench)

        if self.hub_mode:
            def _hub_worker():
                from .hub import HubWorker
                return HubWorker(self)
            self.hub_worker = self._try("hub_worker", _hub_worker)
            if not cfg.hub.token:
                cfg.raw_keys.setdefault("hub", {})["token"] = secrets.token_urlsafe(24)
                try:
                    cfg.save()
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not persist hub token: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    def engine(self):
        return select_engine(self)

    @property
    def preview_port(self) -> int | None:
        try:
            return urllib.parse.urlsplit(self.preview_url).port if self.preview_url else None
        except ValueError:
            return None

    def start(self, server_url: str, preview_url: str | None = None) -> None:
        self.server_url = server_url
        if preview_url is not None:
            self.preview_url = preview_url
        if self.claude_engine and hasattr(self.claude_engine, "install_hook"):
            self._try("hook_install", self.claude_engine.install_hook)
        if self.run_manager:
            self.run_manager.start()
        if self.hub_worker:
            self.hub_worker.start()
        self.started.set()

    def apply_model_settings(self) -> None:
        """Endpoints, keys, models and fallbacks changed in Settings take effect without a restart."""
        nv = self.config.nvidia
        for client, role, label in ((self.nim_nemotron, nv.nemotron, "nemotron"), (self.nim_kimi, nv.kimi, "kimi")):
            if client is not None:
                client.base_url = role.base_url
                client.set_keys(role.api_key, f"{label} key", nv.alt_keys(label))
        if self.nemotron is not None:
            self.nemotron.configure(nv.nemotron.model, nv.nemotron.fallback_models)
        if self.kimi is not None:
            self.kimi.configure(nv.kimi.model, nv.kimi.fallback_models)

    def stop(self) -> None:
        for comp in (self.run_manager, self.hub_worker):
            if comp and hasattr(comp, "stop"):
                try:
                    comp.stop()
                except Exception:
                    pass
        if self.store is not None and hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:  # noqa: BLE001 - closing on shutdown is best effort
                pass
        self.bus.close()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        engine = None
        try:
            eng = self.engine()
            engine = {"backend": eng.name, **eng.availability().to_dict()} if eng else None
        except Exception as exc:  # noqa: BLE001
            engine = {"error": str(exc)}
        engines = {}
        for name, eng in (("hub", self.hub_client), ("claude_code", self.claude_engine), ("anthropic_api", self.anthropic_engine)):
            if eng is None:
                engines[name] = {"available": False, "reason": self.missing.get(name, "not configured")}
            else:
                try:
                    engines[name] = eng.availability().to_dict()
                except Exception as exc:  # noqa: BLE001
                    engines[name] = {"available": False, "reason": str(exc)}
        return {
            "version": __version__,
            "platform": self.platform,
            "home": str(self.home),
            "config_dir": str(self.config.config_dir),
            "hub_mode": self.hub_mode,
            "server_url": self.server_url,
            "preview_url": self.preview_url,
            "preview_port": self.preview_port,
            "lan": lan_addresses(),
            "components": {k: (v is not None) for k, v in {
                "store": self.store, "nemotron": self.nemotron, "kimi": self.kimi, "judge": self.judge,
                "claude_code": self.claude_engine, "anthropic_api": self.anthropic_engine, "hub_client": self.hub_client,
                "tokkio": self.tokkio, "run_manager": self.run_manager, "selfcheck": self.selfcheck, "bench": self.bench}.items()},
            "missing": self.missing,
            "selected_engine": engine,
            "engines": engines,
            "runs": self.run_manager.status() if self.run_manager else None,
            "attribution": ["NVIDIA Nemotron-3-Ultra", "Kimi K3 (Moonshot AI)", "Claude Code (Anthropic)", "NVIDIA ACE / Tokkio"],
        }

    # ------------------------------------------------------------------ routes
    def register_routes(self, router: Router) -> None:
        app = self

        def index(req: Request, params: dict) -> Response:
            # /?token=... sets the cookie for LAN access when hub.expose_ui is on
            resp = resource_response("index.html")
            tok = req.query.get("token")
            if tok and app.config.hub.token and tok == app.config.hub.token:
                # HttpOnly: cookies are shared by every port of a host, so game code on the preview
                # listener must not be able to read the token
                resp.headers.append(("Set-Cookie", f"kyora_token={tok}; Path=/; SameSite=Lax; HttpOnly"))
            return resp

        def static(req: Request, params: dict) -> Response:
            return resource_response(params["path"], cache="public, max-age=300")

        def status(req: Request, params: dict) -> Response:
            return Response.json(app.status())

        def get_settings(req: Request, params: dict) -> Response:
            return Response.json(app.config.to_public(reveal=req.query.get("reveal") == "1"))

        def put_settings(req: Request, params: dict) -> Response:
            patch = req.json()
            changed = app.config.apply_patch(patch)
            if changed:
                app.config.save()
                if any(c.startswith("keys.nvidia.") for c in changed):
                    app.apply_model_settings()  # endpoints, keys and models apply without a restart
                app.bus.publish("settings.changed", {"changed": changed})
            return Response.json({"changed": changed, "restart_required": any(c.startswith("settings.server") for c in changed)})

        def events(req: Request, params: dict) -> Response:
            since = int(req.query.get("since") or req.headers.get("last-event-id") or 0)
            types = [t for t in (req.query.get("types") or "").split(",") if t] or None
            return Response.sse(event_stream(app.bus, run_id=req.query.get("run") or None,
                                             project_id=req.query.get("project") or None, types=types, since=since))

        def events_json(req: Request, params: dict) -> Response:
            since = int(req.query.get("since") or 0)
            types = [t for t in (req.query.get("types") or "").split(",") if t] or None
            evs = app.bus.replay(since, run_id=req.query.get("run") or None, project_id=req.query.get("project") or None,
                                 types=types, limit=int(req.query.get("limit") or 500))
            return Response.json({"events": [e.to_dict() for e in evs], "last_id": app.bus.last_id()})

        def lan_urls(req: Request, params: dict) -> Response:
            port = app.server_url.rsplit(":", 1)[-1] if app.server_url else str(app.config.server.port)
            urls = [f"http://{ip}:{port}" for ip in lan_addresses()]
            return Response.json({"urls": urls, "loopback": app.server_url, "hub_mode": app.hub_mode,
                                  "expose_ui": app.config.hub.expose_ui, "preview_port": app.preview_port})

        def qr_svg(req: Request, params: dict) -> Response:
            text = req.query.get("text") or app.server_url
            try:
                from .util.qr import qr_svg as _qr
                return Response.bytes(_qr(text).encode("utf-8"), "image/svg+xml")
            except Exception as exc:  # noqa: BLE001
                return Response.error(501, f"qr unavailable: {exc}")

        router.add("GET", "/", index, auth="local")
        router.add("GET", "/static/{path:path}", static, auth="any")
        router.add("GET", "/api/status", status)
        router.add("GET", "/api/settings", get_settings)
        router.add("PUT", "/api/settings", put_settings)
        router.add("GET", "/api/events", events)
        router.add("GET", "/api/events.json", events_json)
        router.add("GET", "/api/lan-urls", lan_urls)
        router.add("GET", "/api/qr.svg", qr_svg)

        # module routes (each module exposes register_routes(router, app))
        for modname in ("projects.routes", "pipeline.routes", "hub", "selfcheck", "bench.routes", "models.tokkio"):
            try:
                mod = __import__(f"kyora.{modname}", fromlist=["register_routes"])
                fn = getattr(mod, "register_routes", None)
                if fn:
                    fn(router, app)
            except Exception as exc:  # noqa: BLE001
                self.missing[f"routes:{modname}"] = f"{type(exc).__name__}: {exc}"
                log.warning("routes for %s unavailable: %s", modname, exc)

    def register_preview_routes(self, router: Router, main_router: Router | None = None) -> None:
        """Routes of the preview listener: game files, the harness and its results - nothing else.

        Untrusted game code runs on this origin, so settings, keys, runs and the UI are absent.
        """
        def favicon(req: Request, params: dict) -> Response:
            return Response(204)

        router.add("GET", "/favicon.ico", favicon, auth="any")
        try:
            from .projects.routes import register_preview_routes
            register_preview_routes(router, self)
        except Exception as exc:  # noqa: BLE001
            self.missing["routes:preview"] = f"{type(exc).__name__}: {exc}"
            log.warning("preview routes unavailable: %s", exc)
        for route in (main_router.routes if main_router is not None else []):
            if (route.method, route.pattern) in PREVIEW_SHARED_ROUTES:
                router.routes.append(route)
