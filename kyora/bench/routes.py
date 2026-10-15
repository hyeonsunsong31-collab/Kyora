"""HTTP routes for Kyora Bench.

``GET /api/bench/tasks``    -> ``{tasks, estimate, running}``
``POST /api/bench/run``     json ``{task_ids, confirm: true}`` -> 202 ``{started: true, estimate}``;
                            400 ``{error}`` without confirmation or without a code engine; 409 while running
``POST /api/bench/cancel``  -> ``{cancelled}``
``GET /api/bench/results``  -> ``{results: <latest results.json> | null, tasks: [per-task rows], running}``
``GET /api/bench/badge``    -> badge computed from the latest measured run
``GET /api/bench/report``   -> the latest standalone ``report.html`` (404 when none)
"""
from __future__ import annotations

from ..server import Request, Response, Router
from .runner import BenchRefused


def register_routes(router: Router, app) -> None:
    """Register the bench routes on ``router`` (``app.bench`` is the :class:`BenchRunner`)."""

    def _bench():
        bench = getattr(app, "bench", None)
        if bench is None:
            reason = (getattr(app, "missing", None) or {}).get("bench", "bench component unavailable")
            return None, Response.error(503, f"bench unavailable: {reason}")
        return bench, None

    def tasks(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        return Response.json({"tasks": bench.list_tasks(), "estimate": bench.estimate(None),
                              "running": bench.is_running(), "status": bench.status()})

    def run(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        body = req.json() if req.body else {}
        if not isinstance(body, dict):
            return Response.error(400, "expected a JSON object {task_ids, confirm}")
        ids = body.get("task_ids")
        if ids is not None and not isinstance(ids, (list, str)):
            return Response.error(400, "task_ids must be a list of task ids or a comma-separated string")
        confirm = body.get("confirm") is True
        estimate = bench.estimate(ids)
        try:
            bench.start_async(ids, confirm=confirm)
        except BenchRefused as exc:
            return Response.json({"error": exc.message, "status": exc.status, "estimate": estimate}, status=exc.status)
        return Response.json({"started": True, "estimate": estimate}, status=202)

    def cancel(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        return Response.json({"cancelled": bench.cancel(), "running": bench.is_running()})

    def results(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        latest = bench.last_results()
        return Response.json({"results": latest, "tasks": (latest or {}).get("tasks") or [],
                              "running": bench.is_running(), "status": bench.status()})

    def badge(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        return Response.json(bench.badge())

    def report_html(req: Request, params: dict) -> Response:
        bench, err = _bench()
        if err:
            return err
        path = bench.last_report_path()
        if path is None:
            return Response.error(404, "no bench report yet")
        return Response.file(path, content_type="text/html; charset=utf-8")

    router.add("GET", "/api/bench/tasks", tasks)
    router.add("POST", "/api/bench/run", run)
    router.add("POST", "/api/bench/cancel", cancel)
    router.add("GET", "/api/bench/results", results)
    router.add("GET", "/api/bench/badge", badge)
    router.add("GET", "/api/bench/report", report_html)
