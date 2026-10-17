"""Tokkio overlay (``tokkio-kyora/``) checked offline against the vendored NVIDIA Tokkio 5.0.0-ga tree.

Run:  python3 -m unittest tests.test_tokkio_overlay -v

* ``config.kyora.yaml`` keeps every vendored section and key and sets the Kyora values;
* ``bot.kyora.py`` differs from the vendored ``bot.py`` only by added lines marked ``# KYORA``, and the
  diff printed in ``tokkio-kyora/README.md`` is current;
* ``upload_overlay.py``'s dry run prints the file map and opens no network connection;
* ``scripts/vendor_tokkio.py --verify`` passes;
* ``kyora_processor.py`` imports with stub ``pipecat`` / ``nvidia_pipecat`` / ``httpx`` / ``loguru``
  modules, and its SSE parser, normalisers and event dispatch behave.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import enum
import importlib.util
import io
import re
import socket
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
OVERLAY = REPO / "tokkio-kyora"
VENDOR = REPO / "vendor" / "tokkio-5.0.0-ga"
VENDOR_CONFIG = VENDOR / "src" / "llm-rag" / "configs" / "config.yaml"
VENDOR_BOT = VENDOR / "src" / "llm-rag" / "src" / "bot.py"
KYORA_CONFIG = OVERLAY / "configs" / "config.kyora.yaml"
KYORA_BOT = OVERLAY / "src" / "bot.kyora.py"
TOP_LEVEL = re.compile(r"^([A-Za-z_]\w*):", re.M)
KEY = re.compile(r"^\s*(?:-\s+)?([A-Za-z_]\w*):", re.M)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- config + bot
class OverlayConfigTests(unittest.TestCase):
    def setUp(self):
        self.text = KYORA_CONFIG.read_text(encoding="utf-8")
        self.vendor = VENDOR_CONFIG.read_text(encoding="utf-8")

    def test_kyora_key_lines(self):
        self.assertRegex(self.text, r'(?m)^\s+llm_processor:\s*"NvidiaLLMService"')
        self.assertRegex(self.text, r'(?m)^NvidiaLLMService:\n\s+base_url:\s*"https://integrate\.api\.nvidia\.com/v1"\n'
                                    r'\s+model:\s*"nvidia/nemotron-3-ultra-550b-a55b"')
        m = re.search(r"(?ms)^KyoraProcessor:\n((?:[ \t]+.*\n)+)", self.text)
        self.assertIsNotNone(m, "KyoraProcessor section missing")
        section = m.group(1)
        for key in ("events_url", "token", "narrate", "show_views", "reconnect_s"):
            self.assertRegex(section, rf"(?m)^\s+{key}:")
        # shipped without a host or secret; upload_overlay.py --events-url/--token fill them in
        self.assertRegex(section, r'(?m)^\s+events_url:\s*""')
        self.assertRegex(section, r'(?m)^\s+token:\s*""')
        self.assertNotIn("nvapi-", self.text)

    def test_every_vendored_section_and_key_kept(self):
        vendor_sections = TOP_LEVEL.findall(self.vendor)
        ours = TOP_LEVEL.findall(self.text)
        self.assertGreaterEqual(len(vendor_sections), 10)
        for s in vendor_sections:
            self.assertIn(s, ours, f"vendored section {s} dropped")
        self.assertEqual(set(ours) - set(vendor_sections), {"KyoraProcessor"})
        missing = sorted(set(KEY.findall(self.vendor)) - set(KEY.findall(self.text)))
        self.assertEqual(missing, [], f"vendored keys dropped: {missing}")
        self.assertEqual([s for s in ours if s in vendor_sections], vendor_sections, "vendored section order changed")


class OverlayBotTests(unittest.TestCase):
    def diff_lines(self) -> list[str]:
        a = VENDOR_BOT.read_text(encoding="utf-8").splitlines(keepends=True)
        b = KYORA_BOT.read_text(encoding="utf-8").splitlines(keepends=True)
        return list(difflib.unified_diff(a, b, "vendor/tokkio-5.0.0-ga/src/llm-rag/src/bot.py",
                                         "tokkio-kyora/src/bot.kyora.py"))

    def test_only_added_kyora_lines(self):
        a = VENDOR_BOT.read_text(encoding="utf-8").splitlines()
        b = KYORA_BOT.read_text(encoding="utf-8").splitlines()
        removed = [ln for ln in difflib.ndiff(a, b) if ln.startswith("- ")]
        added = [ln[2:] for ln in difflib.ndiff(a, b) if ln.startswith("+ ")]
        self.assertEqual(removed, [], "bot.kyora.py must not remove or change vendored lines")
        self.assertTrue(added, "bot.kyora.py adds nothing")
        for line in added:
            self.assertTrue(line.strip() == "" or "KYORA" in line, f"unmarked added line: {line!r}")
        joined = "\n".join(added)
        for needle in ("from .kyora_processor import KyoraProcessor", "kyora_processor,", "attach_task(task)",
                       'os.getenv("KYORA_EVENTS_URL"', 'os.getenv("KYORA_HUB_TOKEN"'):
            self.assertIn(needle, joined)

    def test_processor_position_in_pipeline(self):
        b = KYORA_BOT.read_text(encoding="utf-8")
        i_rag = b.index("                rag_multimodal_response_processor,")
        i_kyora = b.index("                kyora_processor,")
        i_a2f = b.index("                a2f,")
        self.assertLess(i_rag, i_kyora)
        self.assertLess(i_kyora, i_a2f)

    def test_bot_compiles(self):
        compile(KYORA_BOT.read_text(encoding="utf-8"), str(KYORA_BOT), "exec")

    def test_realtime_nemotron_runs_with_reasoning_off(self):
        # Nemotron-3-Ultra reasons by default, and the Tokkio LLM mixin speaks only delta.content: with reasoning on
        # the avatar stays silent for the whole trace. pipecat 0.0.68 passes InputParams.extra to the OpenAI SDK's
        # create(), so chat_template_kwargs must travel inside extra_body.
        import ast
        calls = [n for n in ast.walk(ast.parse(KYORA_BOT.read_text(encoding="utf-8")))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "TokkioNvidiaLLMService"]
        self.assertEqual(len(calls), 1)
        kw = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("params", kw, "the NvidiaLLMService avatar LLM is built without InputParams")
        params = kw["params"]
        self.assertEqual(ast.unparse(params.func), "TokkioNvidiaLLMService.InputParams")
        pkw = {k.arg: k.value for k in params.keywords}
        self.assertEqual(ast.literal_eval(pkw["temperature"]), 0.2)
        self.assertEqual(ast.literal_eval(pkw["extra"]),
                         {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})

    def test_readme_diff_is_current(self):
        readme = (OVERLAY / "README.md").read_text(encoding="utf-8")
        m = re.search(r"```diff\n(.*?)```", readme, re.S)
        self.assertIsNotNone(m, "tokkio-kyora/README.md has no ```diff block")
        self.assertEqual(m.group(1), "".join(self.diff_lines()),
                         "README diff is stale: regenerate it from diff -u (see the README)")


# ----------------------------------------------------------------------------- upload script
class _Listener:
    """A TCP listener that counts every connection attempt (proves the dry run stays offline)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


class UploadOverlayTests(unittest.TestCase):
    SCRIPT = OVERLAY / "deploy" / "upload_overlay.py"

    def test_dry_run_lists_file_map_offline(self):
        listener = _Listener()
        try:
            r = subprocess.run([sys.executable, str(self.SCRIPT), "--configurator", f"http://127.0.0.1:{listener.port}",
                                "--events-url", "http://10.1.2.3:8765", "--token", "SECRET-TOKEN-123"],
                               capture_output=True, timeout=60)
        finally:
            listener.close()
        out = r.stdout.decode("utf-8")
        self.assertEqual(r.returncode, 0, out + r.stderr.decode())
        self.assertIn("DRY RUN", out)
        self.assertIn("Dry run complete. Nothing was uploaded.", out)
        for local, target in (("configs/config.kyora.yaml", "configs/config.yaml"),
                              ("src/kyora_processor.py", "src/kyora_processor.py"),
                              ("src/bot.kyora.py", "src/bot.py")):
            self.assertRegex(out, rf"{re.escape(local)}\s+->\s+{re.escape(target)}")
        self.assertIn("ace-controller-ace-controller-deployment", out)
        self.assertIn("app-storage-volume", out)
        self.assertNotIn("SECRET-TOKEN-123", out + r.stderr.decode(), "the token must never be printed")
        self.assertEqual(listener.connections, 0, "the dry run contacted the configurator")

    def test_dry_run_in_process_touches_no_network_or_kubectl(self):
        mod = _load(self.SCRIPT, "kyora_upload_overlay_under_test")

        def forbidden(*a, **k):
            raise AssertionError("network or subprocess used during a dry run")

        buf = io.StringIO()
        with mock.patch.object(mod.urllib.request, "urlopen", forbidden), \
                mock.patch.object(mod.subprocess, "run", forbidden), \
                mock.patch.object(socket, "create_connection", forbidden), contextlib.redirect_stdout(buf):
            rc = mod.main(["--configurator", "http://192.0.2.1:30180", "--kubectl-fallback"])
        self.assertEqual(rc, 0)
        self.assertIn("File map:", buf.getvalue())

    def test_prepare_files_substitutes_events_url_and_token(self):
        mod = _load(self.SCRIPT, "kyora_upload_overlay_under_test2")
        files = mod.prepare_files(OVERLAY, "http://10.1.2.3:8765", "tok-xyz")
        self.assertEqual([t for _, t, _ in files], ["configs/config.yaml", "src/kyora_processor.py", "src/bot.py"])
        cfg = files[0][2].decode("utf-8")
        self.assertIn('events_url: "http://10.1.2.3:8765"', cfg)
        self.assertIn('token: "tok-xyz"', cfg)
        self.assertEqual(files[1][2], (OVERLAY / "src" / "kyora_processor.py").read_bytes())
        self.assertEqual(files[2][2], KYORA_BOT.read_bytes())
        untouched = mod.prepare_files(OVERLAY, None, None)
        self.assertEqual(untouched[0][2], KYORA_CONFIG.read_bytes())

    def test_find_upload_ops_scoring(self):
        mod = _load(self.SCRIPT, "kyora_upload_overlay_under_test3")
        spec = {"paths": {
            "/health": {"get": {}},
            "/api/v1/services/{service_name}/volumes/{volume_name}/files": {
                "put": {"summary": "Upload file", "requestBody": {"content": {"multipart/form-data": {
                    "schema": {"properties": {"file": {"type": "string", "format": "binary"}}}}}}},
                "get": {}},
            "/api/v1/sync": {"post": {"summary": "sync"}},
        }}
        cands = mod.find_upload_ops(spec)
        self.assertEqual(cands[0]["path"], "/api/v1/services/{service_name}/volumes/{volume_name}/files")
        self.assertEqual(cands[0]["field"], "file")
        path, _ = mod.fill_path(cands[0]["path"], "src/bot.py")
        self.assertEqual(path, "/api/v1/services/ace-controller-ace-controller-deployment/volumes/app-storage-volume/files")


class VendorVerifyTests(unittest.TestCase):
    def test_vendor_verify_passes(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "vendor_tokkio.py"), "--verify"],
                           capture_output=True, timeout=120)
        out = r.stdout.decode("utf-8")
        self.assertEqual(r.returncode, 0, out + r.stderr.decode())
        self.assertRegex(out, r"OK: \d+ files under vendor/tokkio-5\.0\.0-ga, \d+ listed in VENDOR\.md, 0 mismatched, "
                              r"0 missing, 0 extra")


# ----------------------------------------------------------------------------- processor with stub modules
class _Model:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return f"{type(self).__name__}({self.__dict__})"


def _stub_modules() -> dict[str, types.ModuleType]:
    mods = {n: types.ModuleType(n) for n in (
        "pipecat", "pipecat.frames", "pipecat.frames.frames", "pipecat.processors", "pipecat.processors.frame_processor",
        "nvidia_pipecat", "nvidia_pipecat.frames", "nvidia_pipecat.frames.custom_view", "httpx", "loguru")}

    class Frame:
        pass

    class StartFrame(Frame):
        pass

    class EndFrame(Frame):
        pass

    class CancelFrame(Frame):
        pass

    class TTSSpeakFrame(Frame):
        def __init__(self, text):
            self.text = text

    class FrameDirection(enum.Enum):
        DOWNSTREAM = 1
        UPSTREAM = 2

    class _Task:
        def __init__(self, name):
            self.name = name

        def add_done_callback(self, cb):
            pass

    class FrameProcessor:
        def __init__(self, **kw):
            self.pushed = []
            self.created = []

        async def process_frame(self, frame, direction):
            return None

        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append((frame, direction))

        def create_task(self, coro, name=None):
            self.created.append((name, coro.cr_frame.f_locals.get("payload") if hasattr(coro, "cr_frame") else None))
            coro.close()  # not run: no network in tests
            return _Task(name)

        async def cancel_task(self, task):
            return None

        async def cleanup(self):
            return None

    ff = mods["pipecat.frames.frames"]
    for c in (Frame, StartFrame, EndFrame, CancelFrame, TTSSpeakFrame):
        setattr(ff, c.__name__, c)
    fp = mods["pipecat.processors.frame_processor"]
    fp.FrameDirection = FrameDirection
    fp.FrameProcessor = FrameProcessor

    cv = mods["nvidia_pipecat.frames.custom_view"]
    for name in ("Button", "ButtonListBlock", "HeaderBlock", "Image", "ImageBlock", "StartCustomViewFrame",
                 "StopCustomViewFrame", "TableBlock", "TextBlock", "UIButtonPressFrame"):
        setattr(cv, name, type(name, (_Model,), {}))
    cv.ButtonVariant = enum.Enum("ButtonVariant", {"CONTAINED": "contained", "OUTLINED": "outlined"})
    mods["loguru"].logger = types.SimpleNamespace(**{lvl: (lambda *a, **k: None)
                                                    for lvl in ("debug", "info", "warning", "error", "exception")})
    mods["httpx"].AsyncClient = None  # the tests never open a connection
    mods["pipecat"].frames = mods["pipecat.frames"]
    mods["pipecat.frames"].frames = ff
    mods["pipecat"].processors = mods["pipecat.processors"]
    mods["pipecat.processors"].frame_processor = fp
    mods["nvidia_pipecat"].frames = mods["nvidia_pipecat.frames"]
    mods["nvidia_pipecat.frames"].custom_view = cv
    return mods


class _FakePipelineTask:
    def __init__(self):
        self.queued = []

    async def queue_frame(self, frame):
        self.queued.append(frame)


class KyoraProcessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stubs = _stub_modules()
        with mock.patch.dict(sys.modules, cls.stubs):
            cls.kp = _load(OVERLAY / "src" / "kyora_processor.py", "kyora_processor_under_test")

    def test_exports(self):
        for name in ("KyoraProcessor", "SSELineParser", "kyora_base_url", "normalise_stages", "normalise_buttons",
                     "strip_data_url"):
            self.assertIn(name, self.kp.__all__)
        self.assertEqual(self.kp.EVENT_TYPES, ("tokkio.narration", "tokkio.view", "tokkio.clear"))

    def test_sse_line_parser(self):
        p = self.kp.SSELineParser()
        self.assertEqual(p.feed(": keepalive\n"), [])
        self.assertEqual(p.feed("id: 41"), [])
        self.assertEqual(p.feed("event: tokkio.view"), [])
        self.assertEqual(p.feed("data: {\"a\": 1,"), [])
        self.assertEqual(p.feed(b"data:  \"b\": 2}\r\n"), [])
        msgs = p.feed("")
        self.assertEqual(msgs, [{"event": "tokkio.view", "data": "{\"a\": 1,\n \"b\": 2}", "id": "41"}])
        self.assertEqual(p.feed("data: x"), [])
        self.assertEqual(p.flush(), [{"event": None, "data": "x", "id": "41"}])  # id is sticky
        self.assertEqual(p.feed(""), [])  # empty dispatch is a no-op

    def test_kyora_base_url(self):
        f = self.kp.kyora_base_url
        self.assertEqual(f("http://h:8765"), "http://h:8765")
        self.assertEqual(f("http://h:8765/"), "http://h:8765")
        self.assertEqual(f(" http://h:8765/api/events?types=tokkio.view "), "http://h:8765")
        self.assertEqual(f("https://x.example/kyora/api/events.json"), "https://x.example/kyora")
        self.assertEqual(f(""), "")
        self.assertEqual(f(None), "")

    def test_normalise_stages(self):
        f = self.kp.normalise_stages
        self.assertEqual(f(None), [])
        self.assertEqual(f([{"stage": "build", "status": "running", "pct": 0.5},
                            {"name": "test", "state": "done", "pct": 75},
                            {"id": "gate", "status": "waiting", "message": "browser"},
                            ["review", "skipped"], "finalize"]),
                         [["build", "running", "50%"], ["test", "done", "75%"], ["gate", "waiting", "browser"],
                          ["review", "skipped", ""], ["finalize", "", ""]])
        self.assertEqual(f({"intake": "done", "plan": {"status": "running", "progress": 0.25}}),
                         [["intake", "done", ""], ["plan", "running", "25%"]])

    def test_normalise_buttons(self):
        f = self.kp.normalise_buttons
        self.assertEqual(f(None), [])
        self.assertEqual(f([{"action": "approve", "label": "Approve"}, "retry", {"id": "kyora:open", "text": "Open"},
                            {"action": "", "label": "ignored"}, {"action": "stop", "active": False}]),
                         [("kyora:approve", "Approve", True), ("kyora:retry", "retry", True),
                          ("kyora:open", "Open", True), ("kyora:stop", "stop", False)])
        self.assertEqual(f({"cancel": "Cancel run"}), [("kyora:cancel", "Cancel run", True)])

    def test_strip_data_url(self):
        f = self.kp.strip_data_url
        self.assertEqual(f("data:image/png;base64,AAAA"), "AAAA")
        self.assertEqual(f("  BBBB "), "BBBB")
        self.assertEqual(f(""), "")

    def test_processor_dispatch(self):
        kp = self.kp
        proc = kp.KyoraProcessor("http://10.0.0.2:8765/api/events", "tok")
        self.assertEqual(proc.base_url, "http://10.0.0.2:8765")
        self.assertEqual(proc.events_url, "http://10.0.0.2:8765/api/events?types=tokkio.narration,tokkio.view,tokkio.clear")
        self.assertEqual(proc.actions_url, "http://10.0.0.2:8765/api/actions")
        self.assertEqual(proc._headers()["Authorization"], "Bearer tok")
        task = _FakePipelineTask()

        async def scenario():
            await proc.handle_event("tokkio.narration", {"text": "Before attach"})  # dropped: no task yet
            proc.attach_task(task)
            lines = ["id: 7", "event: tokkio.narration",
                     'data: {"id": 7, "ts": 1.0, "type": "tokkio.narration", "run_id": "r_1", "project_id": "p_1", '
                     '"data": {"text": "Slide one is ready."}}', "",
                     ": keepalive", ""]

            async def gen():
                for ln in lines:
                    yield ln
            await proc._consume_lines(gen())
            await proc.handle_event("tokkio.view", {"title": "Sky Runner", "preview_url": "http://h/p",
                                                    "screenshot_b64": "data:image/png;base64,QUJD",
                                                    "stages": [{"stage": "build", "status": "done"}],
                                                    "buttons": [{"action": "approve", "label": "Approve"}]})
            await proc.handle_event("tokkio.clear", {})
            await proc.handle_event("tokkio.view", {})  # nothing displayable -> clear
            await proc.handle_event("something.else", {"x": 1})
        asyncio.run(scenario())

        self.assertEqual([f.text for f in task.queued], ["Slide one is ready."])
        self.assertEqual(proc._last_event_id, "7")
        self.assertEqual((proc._run_id, proc._project_id), ("r_1", "p_1"))
        cv = self.stubs["nvidia_pipecat.frames.custom_view"]
        kinds = [type(f).__name__ for f, _ in proc.pushed]
        self.assertEqual(kinds, ["StartCustomViewFrame", "StopCustomViewFrame", "StopCustomViewFrame"])
        view = proc.pushed[0][0]
        self.assertEqual(view.action_id, kp.VIEW_ACTION_ID)
        block_types = [type(b).__name__ for b in view.blocks]
        self.assertEqual(block_types, ["HeaderBlock", "TextBlock", "ImageBlock", "TableBlock", "ButtonListBlock"])
        self.assertEqual(view.blocks[0].header, "Sky Runner")
        self.assertEqual(view.blocks[2].image.data, "QUJD")
        self.assertEqual(view.blocks[3].rows, [["build", "done", ""]])
        self.assertEqual(view.blocks[4].buttons[0].id, "kyora:approve")
        self.assertIsInstance(view.blocks[4].buttons[0], cv.Button)
        self.assertEqual(proc.stats["narrations"], 1)

    def test_button_press_posts_action(self):
        kp = self.kp
        cv = self.stubs["nvidia_pipecat.frames.custom_view"]
        proc = kp.KyoraProcessor("http://10.0.0.2:8765", "")
        proc._run_id, proc._project_id = "r_9", "p_9"
        direction = self.stubs["pipecat.processors.frame_processor"].FrameDirection.UPSTREAM

        async def scenario():
            await proc.process_frame(cv.UIButtonPressFrame(component_id="kyora:approve", action_id=""), direction)
            await proc.process_frame(cv.UIButtonPressFrame(component_id="rag-citation-1", action_id=""), direction)
        asyncio.run(scenario())
        names = [n for n, _ in proc.created]
        self.assertEqual(names, ["kyora_action"], "only kyora:* buttons are posted to Kyora")
        self.assertEqual(proc.created[0][1], {"action": "approve", "run_id": "r_9", "project_id": "p_9"})
        self.assertEqual(len(proc.pushed), 2, "button frames pass through unchanged")
        self.assertNotIn("Authorization", proc._headers())

    def test_resolve_kyora_url(self):
        f = self.kp.resolve_kyora_url
        base = "http://10.0.0.2:8765"
        # Kyora names itself 127.0.0.1 when it listens on every interface; the preview listener keeps its port
        self.assertEqual(f("http://127.0.0.1:8766/preview/p/run-r/index.html", base),
                         "http://10.0.0.2:8766/preview/p/run-r/index.html")
        self.assertEqual(f("http://localhost:8765/api/projects/p/slides/2/download?x=1", base),
                         "http://10.0.0.2:8765/api/projects/p/slides/2/download?x=1")
        self.assertEqual(f("/preview/p/v3/index.html", base), "http://10.0.0.2:8765/preview/p/v3/index.html")
        self.assertEqual(f("http://192.168.1.20:8766/preview/p/v3/", base), "http://192.168.1.20:8766/preview/p/v3/")
        self.assertEqual(f("http://127.0.0.1:8766/p", "http://[fd00::2]:8765"), "http://[fd00::2]:8766/p")
        self.assertEqual(f("http://127.0.0.1:8766/p", "http://127.0.0.1:8765"), "http://127.0.0.1:8766/p")
        self.assertEqual(f("", base), "")

    def test_view_shows_reachable_preview_and_the_url_a_button_returns(self):
        kp = self.kp
        proc = kp.KyoraProcessor("http://10.0.0.2:8765", "tok")
        answers = {"open_preview": (200, '{"url": "http://127.0.0.1:8766/preview/p_1/v3/index.html"}'),
                   "download": (200, '{"url": "http://10.0.0.2:8765/api/projects/p_1/slides/3/download"}'),
                   "run_tests": (400, '{"error": "no slide or run to test yet"}')}

        async def fake_post(url, payload):
            self.assertEqual(url, "http://10.0.0.2:8765/api/actions")
            return answers[payload["action"]]
        proc._http_post = fake_post

        def texts(frame):
            return [b.text for b in frame.blocks if type(b).__name__ == "TextBlock"]

        async def scenario():
            await proc.handle_event("tokkio.view", {
                "title": "Slide v3 passed", "preview_url": "http://127.0.0.1:8766/preview/p_1/run-r_1/index.html",
                "buttons": [{"id": "kyora:open_preview", "text": "Open preview"},
                            {"id": "kyora:download", "text": "Download ZIP"}]}, "r_1", "p_1")
            await proc._post_action({"action": "open_preview", "run_id": "r_1", "project_id": "p_1"})
            await proc._post_action({"action": "download", "run_id": "r_1", "project_id": "p_1"})
            await proc._post_action({"action": "run_tests", "run_id": "r_1", "project_id": "p_1"})
        asyncio.run(scenario())
        frames = [f for f, _ in proc.pushed]
        self.assertEqual([type(f).__name__ for f in frames], ["StartCustomViewFrame"] * 3, "a rejected action shows nothing")
        self.assertEqual(texts(frames[0]), ["Preview: http://10.0.0.2:8766/preview/p_1/run-r_1/index.html"])
        self.assertEqual(texts(frames[1]), ["Open preview: http://10.0.0.2:8766/preview/p_1/v3/index.html",
                                            "Preview: http://10.0.0.2:8766/preview/p_1/run-r_1/index.html"])
        self.assertEqual(texts(frames[2])[0], "Download ZIP: http://10.0.0.2:8765/api/projects/p_1/slides/3/download")
        self.assertEqual(frames[2].blocks[-1].buttons[1].id, "kyora:download", "the buttons stay usable")
        self.assertEqual(proc.stats["actions"], 2)

    def test_narrate_and_views_can_be_switched_off(self):
        proc = self.kp.KyoraProcessor("http://h:8765", "", narrate=False, show_views=False)
        task = _FakePipelineTask()
        proc.attach_task(task)

        async def scenario():
            await proc.handle_event("tokkio.narration", {"text": "quiet"})
            await proc.handle_event("tokkio.view", {"title": "hidden"})
            await proc.handle_event("tokkio.clear", {})
        asyncio.run(scenario())
        self.assertEqual(task.queued, [])
        self.assertEqual(proc.pushed, [])
        self.assertEqual(proc.stats["errors"], 0)

    def test_no_events_url_is_inert(self):
        proc = self.kp.KyoraProcessor("", "")
        start = self.stubs["pipecat.frames.frames"].StartFrame()
        down = self.stubs["pipecat.processors.frame_processor"].FrameDirection.DOWNSTREAM
        asyncio.run(proc.process_frame(start, down))
        self.assertEqual(proc.created, [], "no SSE task without events_url")
        self.assertEqual(proc.pushed, [(start, down)])


if __name__ == "__main__":
    unittest.main()
