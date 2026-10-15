"""NVIDIA Tokkio 5.0.0-ga connector: health of a deployed Tokkio and the presenter event channel.

Tokkio is an avatar *presenter* that the user deploys on an NVIDIA-GPU
Kubernetes cluster (see ``docs/tokkio-overlay.md``). Kyora never pretends an
avatar exists: :meth:`Tokkio.health` reports what it could actually reach
(the Tokkio UI and the ACE Configurator's ``/openapi.json``) and says
"not configured" when ``tokkio.ui_url`` is empty.

The presenter channel is the EventBus. The Kyora overlay processor running
inside Tokkio (``tokkio-kyora/src/kyora_processor.py``) subscribes to
``/api/events?types=tokkio.*`` and turns

* ``tokkio.narration {text}`` into avatar speech,
* ``tokkio.view {title, preview_url, screenshot_b64?, stages, buttons}`` into a
  Tokkio custom view, and
* ``tokkio.clear`` into hiding that view.

Button presses whose id starts with ``kyora:`` are posted back to Kyora's
``POST /api/actions``. Standard library only.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from ..pipeline.events import TOKKIO_CLEAR, TOKKIO_NARRATION, TOKKIO_VIEW
from ..util.ids import now_iso

log = logging.getLogger("kyora.tokkio")

DOCS = "docs/tokkio-overlay.md"
HTTP_TIMEOUT_S = 4.0
BUTTON_PREFIX = "kyora:"
TITLE_MAX = 80
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024  # base64 text length limit for one tokkio.view event
RECENT_VIEWS_MAX = 50
NARRATION_MAX = 4000
STAGES_MAX = 30
BUTTONS_MAX = 8
UI_READ_LIMIT = 64 * 1024
OPENAPI_READ_LIMIT = 8 * 1024 * 1024
_BUTTON_ID_RE = re.compile(r"^kyora:[A-Za-z0-9._:/-]{1,64}$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/_-]*={0,2}$")
_WS_RE = re.compile(r"\s+")


def _clean_text(value: Any, limit: int) -> str:
    """Single-line text: whitespace collapsed, cut to ``limit`` characters (with an ellipsis)."""
    if value is None:
        return ""
    text = _WS_RE.sub(" ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def strip_data_url(value: str) -> str:
    """``data:image/png;base64,AAAA`` -> ``AAAA``; plain base64 is returned unchanged (whitespace removed)."""
    s = (value or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return "".join(s.split())


def _norm_preview_url(value: Any) -> str:
    """Accept absolute http(s) URLs and site-relative paths (``/preview/...``); drop anything else."""
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("/") and not url.startswith("//"):
        return url[:2000]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return url[:2000]
    log.warning("tokkio.view: preview_url %r dropped (only http(s) URLs or /paths are shown)", url[:120])
    return ""


def _norm_stages(stages: Any) -> list[dict]:
    """``[{name, status}]`` from a list of dicts / pairs or a ``{name: status}`` mapping."""
    out: list[dict] = []
    if not stages:
        return out
    if isinstance(stages, Mapping):
        items: list[Any] = [{"name": k, **(v if isinstance(v, Mapping) else {"status": v})} for k, v in stages.items()]
    elif isinstance(stages, (list, tuple)):
        items = list(stages)
    else:
        items = [stages]
    for item in items:
        if len(out) >= STAGES_MAX:
            break
        if isinstance(item, Mapping):
            name = _clean_text(item.get("name") or item.get("stage") or item.get("id"), 40)
            status = _clean_text(item.get("status") or item.get("state"), 24)
            entry: dict = {"name": name, "status": status}
            pct = item.get("pct")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                entry["pct"] = round(float(pct), 1)
        elif isinstance(item, (list, tuple)) and item:
            entry = {"name": _clean_text(item[0], 40), "status": _clean_text(item[1] if len(item) > 1 else "", 24)}
        else:
            entry = {"name": _clean_text(item, 40), "status": ""}
        if entry["name"]:
            out.append(entry)
    return out


def _norm_buttons(buttons: Any) -> list[dict]:
    """``[{id, text}]`` with ids prefixed ``kyora:``; ids with unsupported characters are dropped."""
    out: list[dict] = []
    if not buttons:
        return out
    if isinstance(buttons, Mapping):
        buttons = [{"id": k, "text": v} for k, v in buttons.items()]
    elif not isinstance(buttons, (list, tuple)):
        buttons = [buttons]
    seen: set[str] = set()
    for b in buttons:
        if len(out) >= BUTTONS_MAX:
            break
        if isinstance(b, Mapping):
            raw_id = str(b.get("id") or b.get("action") or "").strip()
            text = b.get("text") or b.get("label")
        else:
            raw_id = str(b or "").strip()
            text = None
        if not raw_id:
            continue
        bid = raw_id if raw_id.startswith(BUTTON_PREFIX) else BUTTON_PREFIX + raw_id
        if not _BUTTON_ID_RE.match(bid):
            log.warning("tokkio.view: button id %r dropped (allowed: kyora:[A-Za-z0-9._:/-])", raw_id[:80])
            continue
        if bid in seen:
            continue
        seen.add(bid)
        label = _clean_text(text if text else bid[len(BUTTON_PREFIX):], 40)
        out.append({"id": bid, "text": label})
    return out


def normalise_view(view: Mapping) -> dict:
    """Validate and normalise a ``tokkio.view`` payload.

    Returns ``{title, preview_url, stages, buttons}`` plus ``screenshot_b64``
    when a valid screenshot of at most 2 MB (base64 text) was given. A dropped
    screenshot is reported in ``screenshot_dropped`` rather than hidden.
    """
    if not isinstance(view, Mapping):
        raise ValueError("view must be an object")
    out: dict = {
        "title": _clean_text(view.get("title"), TITLE_MAX),
        "preview_url": _norm_preview_url(view.get("preview_url")),
        "stages": _norm_stages(view.get("stages")),
        "buttons": _norm_buttons(view.get("buttons")),
    }
    shot = view.get("screenshot_b64")
    if shot:
        b64 = strip_data_url(str(shot))
        if len(b64) > SCREENSHOT_MAX_BYTES:
            out["screenshot_dropped"] = f"screenshot of {len(b64)} bytes exceeds the 2 MB limit"
            log.info("tokkio.view: %s", out["screenshot_dropped"])
        elif not b64 or not _B64_RE.match(b64):
            out["screenshot_dropped"] = "screenshot is not valid base64"
        else:
            out["screenshot_b64"] = b64
    return out


class Tokkio:
    """Connector to a user-deployed NVIDIA Tokkio 5.0.0-ga (health + presenter events).

    ``tokkio_cfg`` is a :class:`kyora.config.TokkioCfg` (``ui_url``,
    ``configurator_url``) or a zero-argument callable returning one; call
    :meth:`update_config` after the settings change. Thread-safe.
    """

    def __init__(self, tokkio_cfg: Any, bus, *, timeout_s: float = HTTP_TIMEOUT_S,
                 opener: urllib.request.OpenerDirector | None = None):
        self._cfg_source: Callable[[], Any] | None = tokkio_cfg if callable(tokkio_cfg) else None
        self._cfg = None if self._cfg_source else tokkio_cfg
        self.bus = bus
        self.timeout_s = float(timeout_s)
        self._opener = opener or urllib.request.build_opener()
        self._lock = threading.Lock()
        self._views: deque[dict] = deque(maxlen=RECENT_VIEWS_MAX)

    # ------------------------------------------------------------------ config
    def update_config(self, tokkio_cfg: Any) -> None:
        """Replace the URLs (e.g. after ``PUT /api/settings``)."""
        with self._lock:
            self._cfg_source = tokkio_cfg if callable(tokkio_cfg) else None
            self._cfg = None if self._cfg_source else tokkio_cfg

    def _urls(self) -> tuple[str, str]:
        with self._lock:
            cfg = self._cfg_source() if self._cfg_source else self._cfg
        if cfg is not None and not hasattr(cfg, "ui_url") and hasattr(cfg, "tokkio"):
            cfg = cfg.tokkio  # a whole kyora.config.Config was passed
        ui = str(getattr(cfg, "ui_url", "") or "").strip().rstrip("/")
        conf = str(getattr(cfg, "configurator_url", "") or "").strip().rstrip("/")
        return ui, conf

    @property
    def ui_url(self) -> str:
        return self._urls()[0]

    @property
    def configurator_url(self) -> str:
        return self._urls()[1]

    def configured(self) -> bool:
        """True when ``tokkio.ui_url`` is set (Tokkio deployed and linked)."""
        return bool(self.ui_url)

    # ------------------------------------------------------------------ health
    def _get(self, url: str, read_limit: int) -> dict:
        """GET ``url``; returns ``{reachable, status, latency_ms, error, body}`` and never raises."""
        out: dict = {"reachable": False, "status": None, "latency_ms": None, "error": None, "body": b""}
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "kyora-tokkio-health",
                                                                  "Accept": "application/json, text/html;q=0.9, */*;q=0.5"})
        t0 = time.monotonic()
        try:
            resp = self._opener.open(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as exc:  # the host answered with an error status
            out.update(reachable=True, status=int(exc.code), latency_ms=round((time.monotonic() - t0) * 1000.0, 1),
                       error=f"HTTP {exc.code} {exc.reason}")
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - closing an error response is best effort
                pass
            return out
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            out.update(latency_ms=round((time.monotonic() - t0) * 1000.0, 1), error=f"unreachable: {reason}")
            return out
        try:
            out["status"] = int(getattr(resp, "status", None) or resp.getcode() or 0) or None
            out["reachable"] = True
            try:
                out["body"] = resp.read(read_limit)
            except (OSError, ValueError) as exc:
                out["error"] = f"connection lost while reading: {exc}"
            out["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _ui_health(self, ui_url: str) -> dict:
        info: dict = {"configured": bool(ui_url), "url": ui_url, "reachable": False, "ok": False, "status": None,
                      "latency_ms": None, "error": None}
        if not ui_url:
            info["error"] = "tokkio.ui_url is empty"
            return info
        res = self._get(ui_url, UI_READ_LIMIT)
        info.update(reachable=res["reachable"], status=res["status"], latency_ms=res["latency_ms"], error=res["error"])
        info["ok"] = bool(res["reachable"] and res["status"] and res["status"] < 400 and not res["error"])
        return info

    def _configurator_health(self, conf_url: str) -> dict:
        info: dict = {"configured": bool(conf_url), "url": conf_url, "reachable": False, "openapi_found": False,
                      "status": None, "latency_ms": None, "error": None, "paths": None}
        if not conf_url:
            info["error"] = "tokkio.configurator_url is empty"
            return info
        res = self._get(conf_url + "/openapi.json", OPENAPI_READ_LIMIT)
        info.update(reachable=res["reachable"], status=res["status"], latency_ms=res["latency_ms"], error=res["error"])
        if res["reachable"] and res["status"] and res["status"] < 400 and not res["error"]:
            try:
                doc = json.loads(bytes(res["body"]).decode("utf-8", "replace"))
            except ValueError:
                info["error"] = "/openapi.json is not JSON"
                return info
            if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc or isinstance(doc.get("paths"), dict)):
                info["openapi_found"] = True
                paths = doc.get("paths")
                info["paths"] = len(paths) if isinstance(paths, dict) else None
            else:
                info["error"] = "/openapi.json is not an OpenAPI document"
        return info

    def health(self) -> dict:
        """Probe the Tokkio UI (``GET ui_url``) and the ACE Configurator (``GET configurator_url/openapi.json``).

        Each request has a 4 s timeout and both run concurrently. Nothing is
        assumed: an empty URL is "not configured", a refused connection is
        "unreachable", an HTTP error is "reachable" with its status.
        """
        ui_url, conf_url = self._urls()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tokkio-health") as pool:
            f_ui = pool.submit(self._ui_health, ui_url)
            f_conf = pool.submit(self._configurator_health, conf_url)
            ui = f_ui.result()
            conf = f_conf.result()
        return {"configured": bool(ui_url), "ui": ui, "configurator": conf, "docs": DOCS, "checked_at": now_iso()}

    # ------------------------------------------------------------------ presenter channel
    def _publish(self, type_: str, data: dict, run_id: str | None, project_id: str | None):
        if self.bus is None:
            log.warning("tokkio: no event bus; %s not published", type_)
            return None
        return self.bus.publish(type_, data, run_id=run_id or None, project_id=project_id or None)

    def narrate(self, run_id: str | None, project_id: str | None, text: str | None):
        """Publish ``tokkio.narration {text}`` for the avatar to speak; empty text is ignored (returns None)."""
        clean = str(text or "").strip()
        if not clean:
            return None
        if len(clean) > NARRATION_MAX:
            clean = clean[: NARRATION_MAX - 1].rstrip() + "…"
        return self._publish(TOKKIO_NARRATION, {"text": clean}, run_id, project_id)

    def show_view(self, run_id: str | None, project_id: str | None, view: Mapping) -> dict:
        """Normalise ``view`` (see :func:`normalise_view`), publish ``tokkio.view`` and return the payload sent."""
        data = normalise_view(view)
        self._publish(TOKKIO_VIEW, data, run_id, project_id)
        record = {"ts": now_iso(), "run_id": run_id, "project_id": project_id,
                  **{k: v for k, v in data.items() if k != "screenshot_b64"},
                  "has_screenshot": "screenshot_b64" in data}
        with self._lock:
            self._views.append(record)
        return data

    def clear_view(self, run_id: str | None, project_id: str | None):
        """Publish ``tokkio.clear`` (hide the custom view)."""
        return self._publish(TOKKIO_CLEAR, {}, run_id, project_id)

    def recent_views(self, limit: int = RECENT_VIEWS_MAX) -> list[dict]:
        """The last views shown (newest first, at most 50; screenshots are summarised as ``has_screenshot``)."""
        with self._lock:
            items = list(self._views)
        items.reverse()
        return [dict(v) for v in items[: max(0, int(limit))]]


def register_routes(router, app) -> None:
    """``GET /api/tokkio/health`` (``?views=1`` adds ``recent_views``)."""
    from ..server import Response

    def health(req, params):
        tk = getattr(app, "tokkio", None)
        if tk is None:
            missing = getattr(app, "missing", {}) or {}
            return Response.json({"configured": False, "available": False, "docs": DOCS,
                                  "error": missing.get("tokkio", "Tokkio connector unavailable")})
        cfg = getattr(app, "config", None)
        if cfg is not None and hasattr(tk, "update_config"):
            try:
                tk.update_config(cfg.tokkio)  # pick up URLs changed in Settings
            except Exception as exc:  # noqa: BLE001 - keep serving with the previous URLs
                log.warning("tokkio: could not refresh config: %s", exc)
        out = tk.health()
        if req.query.get("views") in ("1", "true", "yes"):
            out["recent_views"] = tk.recent_views()
        return Response.json(out)

    router.add("GET", "/api/tokkio/health", health)
