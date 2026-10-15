"""Test-only stand-ins for the code engine and the device browser (never shipped in kyora/).

``FakeCodeEngine`` writes the real fixture game (``tests/fixtures/game-ok``) into the
work tree, or a deliberately broken copy, and reports through the same
EngineSink protocol as the real engines. ``FakeBrowser`` answers
``test.await_browser`` by POSTing a browser-test payload to the real
``/api/runs/<rid>/browser-results`` route, so the route, the collector and the
gate are exercised for real.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from kyora.models.base import Availability, CodeEngine, EngineEvent, EngineJob, EngineResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAME_OK = FIXTURES / "game-ok"
TEST_IDS = [f"at-0{i}" for i in range(1, 7)]
PNG_1PX = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def copy_game(dst: Path, broken: bool = False) -> None:
    for src in GAME_OK.rglob("*"):
        if src.is_file():
            target = dst / src.relative_to(GAME_OK)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
    if broken:
        game = dst / "src" / "game.js"
        game.write_text(game.read_text(encoding="utf-8") + "\nfunction broken( {\n", encoding="utf-8")


class FakeCodeEngine(CodeEngine):
    """Scripted engine: ``script`` lists 'ok' / 'broken' per call (last entry repeats)."""

    name = "fake"

    def __init__(self, script=("ok",), sleep_s: float = 0.0, available: bool = True):
        self.script = list(script) or ["ok"]
        self.sleep_s = sleep_s
        self.available = available
        self.calls: list[EngineJob] = []
        self.started = threading.Event()

    def availability(self) -> Availability:
        return Availability(self.available, self.name, "" if self.available else "fake engine disabled")

    def run(self, job: EngineJob, sink, cancel: threading.Event) -> EngineResult:
        mode = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append(job)
        sink.on_event(EngineEvent("engine.session", {"session_id": "fake-session", "model": "fake-model",
                                                     "backend": self.name}))
        self.started.set()
        deadline = time.monotonic() + self.sleep_s
        while time.monotonic() < deadline:
            if cancel.wait(0.05):
                return EngineResult(ok=False, backend=self.name, requested_model=job.model, error="cancelled")
        copy_game(Path(job.workspace), broken=(mode == "broken"))
        sink.on_event(EngineEvent("engine.tool_use", {"id": "t1", "name": "Write", "summary": "src/game.js"}))
        sink.on_event(EngineEvent("engine.result", {"subtype": "success", "is_error": False, "num_turns": 3,
                                                    "duration_ms": 10, "cost_usd": None, "result": "done"}))
        return EngineResult(ok=True, backend=self.name, requested_model=job.model, active_model="fake-model",
                            session_id="fake-session", num_turns=3, duration_ms=10, result_text="done")


class FakeBrowser:
    """Posts a browser result for every ``test.await_browser`` (pass, or fail when ``mode(run_id, n)`` says so)."""

    def __init__(self, app, base_url: str, mode: Callable[[str, int], str] | None = None):
        self.app = app
        self.base_url = base_url.rstrip("/")
        self.mode = mode or (lambda rid, n: "pass")
        self.sub = app.bus.subscribe(types=["test.await_browser"])
        self.posted: list[tuple[str, int, int]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                e = self.sub.queue.get(timeout=0.2)
            except Exception:
                continue
            if e is None:
                return
            n = int(e.data.get("iteration") or 0)
            self.post(e.run_id, n, self.mode(e.run_id, n))

    def post(self, run_id: str, n: int, mode: str) -> int:
        status = "pass" if mode == "pass" else "fail"
        results = [{"id": i, "name": i, "status": status, "ms": 5, "error": None if status == "pass" else "boom"}
                   for i in TEST_IDS]
        passed = sum(r["status"] == "pass" for r in results)
        payload = {"type": "kyora:test-result", "run_id": run_id, "iteration": n, "ref": f"run-{run_id}",
                   "results": results,
                   "summary": {"passed": passed, "failed": len(results) - passed, "skipped": 0, "total": len(results)},
                   "syntax_checks": [{"file": "src/game.js", "ok": status == "pass", "error": None if status == "pass"
                                      else "SyntaxError", "method": "new Function"}],
                   "screenshots": [{"name": "start", "data_url": PNG_1PX}],
                   "env": {"ua": "fake", "viewport": [390, 760], "touch": True, "started_at": "", "duration_ms": 1}}
        req = urllib.request.Request(f"{self.base_url}/api/runs/{run_id}/browser-results",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.posted.append((run_id, n, code))
        return code

    def close(self) -> None:
        self._stop.set()
        self.sub.close()
