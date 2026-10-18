#!/usr/bin/env python3
"""Run the Tokkio overlay (tokkio-kyora/src/kyora_processor.py) inside a REAL pipecat pipeline.

Needs a Python with the exact Tokkio 5.0.0-ga libraries (``pip install nvidia-pipecat==0.2.0``,
which brings pipecat-ai 0.0.68). It starts a real Kyora server (temporary home), builds a
pipecat ``Pipeline([KyoraProcessor, Capture])`` with a ``PipelineTask`` exactly as bot.kyora.py
attaches it, and then checks, end to end over HTTP/SSE:

  1. Kyora ``tokkio.narration``  -> a ``TTSSpeakFrame`` with that text reaches the pipeline
  2. Kyora ``tokkio.view``       -> a ``StartCustomViewFrame`` with real nvidia_pipecat blocks
  3. Kyora ``tokkio.clear``      -> a ``StopCustomViewFrame``
  4. a ``UIButtonPressFrame`` ``kyora:open_preview`` -> ``POST /api/actions`` on Kyora, and the
     returned absolute preview URL is shown in a follow-up custom view

The GPU services of Tokkio (Riva ASR/TTS, Audio2Face, the renderer) are not involved; this
checks the overlay's contract with pipecat/nvidia-pipecat and with Kyora. Exit 0 when every
check passed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tokkio-kyora" / "src"))

from pipecat.frames.frames import EndFrame, Frame, TTSSpeakFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from nvidia_pipecat.frames.custom_view import (  # noqa: E402
    StartCustomViewFrame, StopCustomViewFrame, UIButtonPressFrame)

import kyora_processor  # noqa: E402
from kyora.app import App  # noqa: E402
from kyora.config import Config  # noqa: E402
from kyora.server import KyoraServer, Router  # noqa: E402

PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Capture(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)

    def of(self, kind):
        return [f for f in self.frames if isinstance(f, kind)]


async def wait_for(pred, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


async def scenario(app, server) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    proc = kyora_processor.KyoraProcessor(f"{server.url}/api/events", token="", reconnect_s=0.5)
    cap = Capture()
    task = PipelineTask(Pipeline([proc, cap]), params=PipelineParams(), idle_timeout_secs=None,
                        check_dangling_tasks=False)
    proc.attach_task(task)
    runner = PipelineRunner(handle_sigint=False)
    run = asyncio.create_task(runner.run(task))

    connected = await wait_for(lambda: proc.stats["connects"] >= 1)
    results.append(("overlay subscribes to Kyora /api/events over SSE", connected, json.dumps(proc.stats)))
    await asyncio.sleep(0.5)

    # A real project with a slide so the button action has something to open.
    project = app.store.create("Overlay check")
    fixture = REPO / "tests" / "fixtures" / "game-ok"
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in fixture.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(fixture).as_posix())
    slide_ok = False
    try:
        app.store.slide_manager(project).import_zip(buf.getvalue(), "game.zip")
        slide_ok = True
    except Exception as exc:  # noqa: BLE001
        results.append(("fixture slide imported", False, f"{type(exc).__name__}: {exc}"))
    if slide_ok:
        results.append(("fixture slide imported", True, f"project {project.id}"))

    app.bus.publish("tokkio.narration", {"text": "Kyora is checking the overlay."}, project_id=project.id)
    ok = await wait_for(lambda: any(getattr(f, "text", "") == "Kyora is checking the overlay."
                                    for f in cap.of(TTSSpeakFrame)))
    results.append(("tokkio.narration -> TTSSpeakFrame at the pipeline head", ok,
                    repr([f.text for f in cap.of(TTSSpeakFrame)])))

    view = {"title": "Block Catcher", "subtitle": "slide v1", "stage": "gate", "pct": 96,
            "preview_url": f"{server.preview_url}/preview/{project.id}/v1/index.html",
            "screenshot_b64": PNG_1PX, "stages": [{"stage": "build", "state": "done"}, {"stage": "test", "state": "done"}],
            "buttons": [{"id": "kyora:open_preview", "label": "Open preview"}, {"id": "kyora:download", "label": "Download ZIP"}]}
    app.bus.publish("tokkio.view", view, project_id=project.id)
    ok = await wait_for(lambda: len(cap.of(StartCustomViewFrame)) >= 1)
    blocks = cap.of(StartCustomViewFrame)[0].blocks if ok else []
    kinds = [type(b).__name__ for b in blocks]
    results.append(("tokkio.view -> StartCustomViewFrame with nvidia_pipecat blocks", ok and len(blocks) >= 3, repr(kinds)))

    before_views = len(cap.of(StartCustomViewFrame))
    await task.queue_frame(UIButtonPressFrame(action_id="kyora-view", component_id="kyora:open_preview"))
    ok = await wait_for(lambda: proc.stats["actions"] >= 1 and len(cap.of(StartCustomViewFrame)) > before_views, 20)
    shown = ""
    if ok:
        last = cap.of(StartCustomViewFrame)[-1]
        shown = " | ".join(str(getattr(b, "text", "") or getattr(b, "header", "")) for b in last.blocks)
    url_ok = ok and "/preview/" in shown and str(server.preview_port) in shown
    results.append(("button kyora:open_preview -> POST /api/actions -> absolute preview URL shown", url_ok,
                    shown[:300] or json.dumps(proc.stats)))

    app.bus.publish("tokkio.clear", {}, project_id=project.id)
    ok = await wait_for(lambda: len(cap.of(StopCustomViewFrame)) >= 1)
    results.append(("tokkio.clear -> StopCustomViewFrame", ok, ""))

    await task.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(run, 15)
    except asyncio.TimeoutError:
        await task.cancel()
    return results


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="kyora-tokkio-check-")
    app = App(Config.load(REPO / "config"), home=Path(tmp.name))
    router = Router()
    app.register_routes(router)
    server = KyoraServer(app, "127.0.0.1", 0, router, preview_port=0)
    server.start()
    app.start(server.url, server.preview_url)
    try:
        results = asyncio.run(scenario(app, server))
    finally:
        server.shutdown()
        app.stop()
        tmp.cleanup()
    ok_all = True
    for name, ok, detail in results:
        ok_all &= ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    import pipecat
    import importlib.metadata as md
    print(f"pipecat-ai {md.version('pipecat-ai')}, nvidia-pipecat {md.version('nvidia-pipecat')}")
    print("TOKKIO OVERLAY CHECK " + ("OK" if ok_all else "FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
