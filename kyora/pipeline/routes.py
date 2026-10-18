"""Pipeline HTTP routes: start/inspect/control runs, browser results, the judge hook endpoint, Tokkio actions.

``POST /api/judge`` is authenticated by the per-run hook token, not by the peer
address: ``JudgeService.handle_request`` verifies the token with a constant-time
``hmac`` compare and answers 403 otherwise. It is registered ``auth="any"`` so
the request reaches that check even when Kyora binds a specific LAN IP (``kyora
hub --host <ip>``): a loopback-only route would otherwise 403 the hook, which
then failed open and ran every tool call unjudged. Everything else uses the
server's ``local`` access level (loopback, or a valid hub token when the UI is
exposed).
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from ..server import Request, Response, Router, parse_host
from . import events as ev
from .stages import ENGINE_ATTRS

log = logging.getLogger("kyora.pipeline.routes")


def _caller_origins(app, req: Request) -> tuple[str, str]:
    """``(main, preview)`` origins under the name the caller used to reach Kyora.

    The ``Host`` header already passed the server's allow-list, so a Tokkio cluster that posts to
    ``http://192.168.1.20:8765`` gets ``http://192.168.1.20:8765`` and ``http://192.168.1.20:<preview_port>``
    back, not ``127.0.0.1``. Game files live only on the preview listener (a separate origin)."""
    raw = (req.headers.get("host") or "").strip()
    host = parse_host(raw)
    main = str(getattr(app, "server_url", "") or "").rstrip("/")
    preview = str(getattr(app, "preview_url", "") or "").rstrip("/")
    if host:
        name = f"[{host}]" if ":" in host else host
        try:
            port = urllib.parse.urlsplit("//" + raw).port or urllib.parse.urlsplit(main).port
        except ValueError:
            port = None
        main = f"http://{name}" + (f":{port}" if port else "")
        pport = getattr(app, "preview_port", None)
        preview = f"http://{name}:{pport}" if pport else preview
    return main, preview or main


def _run_view(app, run) -> dict:
    d = run.to_dict()
    d["runner_url"] = (run.extra or {}).get("runner_url")
    d["preview_ref"] = f"run-{run.id}"
    d["preview_url"] = f"/preview/{run.project_id}/run-{run.id}/index.html"
    rm = getattr(app, "run_manager", None)
    d["active"] = bool(rm and rm.is_active(run.id))
    if getattr(app, "judge", None) is not None:
        try:
            d["judge_live"] = app.judge.stats(run.id)
        except Exception:  # noqa: BLE001
            pass
    return d


def register_routes(router: Router, app) -> None:
    def need(component: str):
        obj = getattr(app, component, None)
        if obj is None:
            raise _Unavailable(f"{component} unavailable: {app.missing.get(component, 'not configured')}")
        return obj

    def wrap(fn):
        def handler(req: Request, params: dict) -> Response:
            try:
                return fn(req, params)
            except _Unavailable as exc:
                return Response.error(503, str(exc))
        return handler

    def create_run(req: Request, params: dict) -> Response:
        store = need("store")
        rm = need("run_manager")
        body = req.json() or {}
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        kind = str(body.get("kind") or "generate")
        prompt = body.get("prompt") or ""
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        if not str(prompt).strip() and not options.get("import_zip_attachment"):
            raise ValueError("prompt is required (or choose a ZIP to import as the base)")
        allowed_opts = {"engine_pref", "import_zip_attachment", "bench"}
        options = {k: v for k, v in options.items() if k in allowed_opts}
        prefs = ("auto",) + tuple(ENGINE_ATTRS)  # every registered engine, the Kyora agent engine included
        if options.get("engine_pref") not in (None,) + prefs:
            raise ValueError("engine_pref must be one of " + ", ".join(prefs))
        parent = body.get("parent_slide")
        run = store.create_run(params["id"], kind, str(prompt), parent if parent not in ("", None) else None,
                               body.get("attachment_ids") or [], options,
                               threshold=app.config.quality.min_pass_rate)
        app.bus.publish(ev.RUN_CREATED, {"kind": run.kind, "prompt": run.prompt[:200], "parent_slide": run.parent_slide,
                                         "attachments": len(run.attachment_ids)}, run_id=run.id, project_id=run.project_id)
        rm.submit(run.id)
        return Response.json({"run": _run_view(app, run)}, status=201)

    def list_runs(req: Request, params: dict) -> Response:
        store = need("store")
        runs = store.list_runs(req.query.get("project") or None, limit=int(req.query.get("limit") or 50))
        return Response.json({"runs": [r.summary() for r in runs]})

    def get_run(req: Request, params: dict) -> Response:
        store = need("store")
        return Response.json({"run": _run_view(app, store.get_run(params["id"]))})

    def control(action: str):
        def handler(req: Request, params: dict) -> Response:
            rm = need("run_manager")
            store = need("store")
            run = store.get_run(params["id"])
            ok = getattr(rm, action)(run.id)
            if not ok:
                return Response.error(409, f"cannot {action} run in state {store.get_run(run.id).state}")
            return Response.json({"ok": True, "run": _run_view(app, store.get_run(run.id))})
        return handler

    def browser_results(req: Request, params: dict) -> Response:
        store = need("store")
        collector = need("browser_results")
        run = store.get_run(params["id"])
        payload = req.json()
        if not collector.submit(run.id, payload):
            return Response.error(400, collector.last_error(run.id) or "invalid browser test payload")
        summary = payload.get("summary") if isinstance(payload, dict) else None
        app.bus.publish(ev.TEST_BROWSER_RESULT, {"summary": summary, "iteration": (payload or {}).get("iteration"),
                                                 "source": "harness"}, run_id=run.id, project_id=run.project_id)
        return Response.json({"ok": True})

    def judge(req: Request, params: dict) -> Response:
        # Authenticated by the per-run hook token inside handle_request (constant-time hmac), not by the
        # peer address, so the hook reaches the judge even when Kyora binds a specific LAN IP.
        svc = need("judge")
        try:
            payload = req.json()
        except ValueError:
            return Response.json({"error": "invalid JSON", "decision": "deny"}, status=400)
        status, body = svc.handle_request(payload)
        return Response.json(body, status=status)

    def actions(req: Request, params: dict) -> Response:
        body = req.json() or {}
        action = str(body.get("action") or "").replace("kyora:", "")
        store = need("store")
        rid = body.get("run_id")
        pid = body.get("project_id")
        run = store.get_run(rid) if rid else None
        if run is not None:
            pid = run.project_id
        if not pid:
            raise ValueError("project_id or run_id is required")
        project = store.get(pid)
        # Absolute URLs: the caller (the Tokkio overlay) runs on another host and origin. Previews and the test
        # harness are served by the preview listener only; the slide ZIP by the main listener.
        main, preview = _caller_origins(app, req)

        def on_preview(path: str) -> str:
            return path if "://" in path else preview + path

        if action == "run_tests":
            if run is None or not run.extra.get("runner_url"):
                n = project.current_slide
                if not n:
                    raise ValueError("no slide or run to test yet")
                return Response.json({"url": on_preview(f"/runner/{pid}/v{n}?auto=1&post=1")})
            app.bus.publish(ev.TEST_AWAIT_BROWSER, {"runner_url": run.extra["runner_url"], "iteration": run.iteration,
                                                    "requested_by": "action"}, run_id=run.id, project_id=pid)
            return Response.json({"url": on_preview(run.extra["runner_url"])})
        if action == "open_preview":
            if run is not None and run.result_slide is None:
                return Response.json({"url": on_preview(f"/preview/{pid}/run-{run.id}/index.html")})
            n = (run.result_slide if run is not None else None) or project.current_slide
            if not n:
                raise ValueError("no slide yet")
            return Response.json({"url": on_preview(f"/preview/{pid}/v{n}/index.html")})
        if action == "download":
            n = (run.result_slide if run is not None else None) or project.current_slide
            if not n:
                raise ValueError("no slide to download yet")
            return Response.json({"url": f"{main}/api/projects/{pid}/slides/{n}/download"})
        raise ValueError("unknown action (run_tests, open_preview, download)")

    router.add("POST", "/api/projects/{id}/runs", wrap(create_run))
    router.add("GET", "/api/runs", wrap(list_runs))
    router.add("GET", "/api/runs/{id}", wrap(get_run))
    router.add("POST", "/api/runs/{id}/cancel", wrap(control("cancel")))
    router.add("POST", "/api/runs/{id}/approve", wrap(control("approve")))
    router.add("POST", "/api/runs/{id}/resume", wrap(control("resume")))
    router.add("POST", "/api/runs/{id}/browser-results", wrap(browser_results))
    router.add("POST", "/api/judge", wrap(judge), auth="any")  # token-authenticated inside handle_request
    router.add("POST", "/api/actions", wrap(actions))


class _Unavailable(Exception):
    pass


__all__ = ["register_routes"]
