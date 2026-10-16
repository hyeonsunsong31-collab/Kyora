"""Hub mode: two in-process Kyora servers (hub + client), token auth, lockout, SSE relay, result zip.

The hub's code engine is a FakeCodeEngine defined here (tests only): it writes
``built.txt`` into the workspace and emits engine events through the sink.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="kyora-hub-test-home-")
os.environ["KYORA_HOME"] = _TMP_HOME

from kyora.app import App  # noqa: E402
from kyora.config import Config, HubCfg  # noqa: E402
from kyora.hub import (CLIENT_KEEP, NO_ENGINE_REASON, RESULT_ENTRY, HubAuth, HubClientEngine, HubJobNotFinished,  # noqa: E402
                       HubJobStore, encode_multipart, fallback_unzip, link_payload, zip_tree)
from kyora.models.base import Availability, CodeEngine, EngineEvent, EngineJob, EngineResult  # noqa: E402
from kyora.pipeline.events import EventBus  # noqa: E402
from kyora.server import KyoraServer, Router  # noqa: E402
from kyora.util.fs import atomic_write_json  # noqa: E402

HUB_TOKEN = "hub-test-token-0123456789"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def tearDownModule():  # noqa: N802 - unittest hook
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


class FakeCodeEngine(CodeEngine):
    """Test double for a local code engine on the hub."""

    name = "fake"

    def __init__(self, block: bool = False):
        self.block = block
        self.jobs: list[EngineJob] = []
        self.started = threading.Event()
        self.judge = None  # when set, the engine judges a couple of calls on the hub's judge, like Claude Code

    def availability(self) -> Availability:
        return Availability(True, "fake", "", {"version": "fake 1.0", "logged_in": True})

    def run(self, job: EngineJob, sink, cancel: threading.Event) -> EngineResult:
        self.jobs.append(job)
        if self.judge is not None:
            self.judge.register_context(job.run_id, dict(job.judge_context or {}))
            self.judge.decide(job.run_id, {"tool_name": "Write", "tool_use_id": "jt1",
                                           "tool_input": {"file_path": str(Path(job.workspace) / "index.html")}})
            self.judge.decide(job.run_id, {"tool_name": "Bash", "tool_use_id": "jt2",
                                           "tool_input": {"command": "git push"}})
            self.judge.record_unavailable(job.run_id)  # a call the hook could not reach
            self.judge.forget(job.run_id)
        sink.on_event(EngineEvent("engine.session", {"session_id": "sess-1", "model": "fake-model", "tools": ["Write"],
                                                     "permission_mode": "acceptEdits", "cwd": str(job.workspace),
                                                     "backend": "fake"}))
        sink.on_event(EngineEvent("engine.tool_use", {"id": "tu1", "name": "Write", "summary": "Write built.txt"}))
        self.started.set()
        if self.block:
            cancel.wait(20)
            if cancel.is_set():
                return EngineResult(ok=False, backend="fake", requested_model=job.model, error="cancelled")
        (Path(job.workspace) / "built.txt").write_text("built on the hub\n", encoding="utf-8")
        sink.on_event(EngineEvent("engine.result", {"subtype": "success", "is_error": False, "num_turns": 2,
                                                    "duration_ms": 5, "cost_usd": None, "result": "done"}))
        return EngineResult(ok=True, backend="fake", requested_model=job.model, active_model="fake-model",
                            session_id="sess-1", num_turns=2, duration_ms=5, result_text="done")


class RecordingSink:
    def __init__(self, on_event=None):
        self.events: list[EngineEvent] = []
        self._hook = on_event

    def on_event(self, ev: EngineEvent) -> None:
        self.events.append(ev)
        if self._hook:
            self._hook(ev)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def _make_app(root: Path, keys: dict, hub_mode: bool) -> App:
    cfg_dir = root / "config"
    atomic_write_json(cfg_dir / "keys.json", keys)
    atomic_write_json(cfg_dir / "settings.json", {"server": {"host": "127.0.0.1", "port": 0}})
    app = App(Config.load(cfg_dir), home=root / "home", hub_mode=hub_mode)
    app.run_manager = None  # pipeline runs are not under test here
    return app


class _Node:
    """An App + KyoraServer pair started the way ``kyora serve`` does it."""

    def __init__(self, root: Path, keys: dict, hub_mode: bool, engine=None):
        self.app = _make_app(root, keys, hub_mode)
        if hub_mode:
            self.app.claude_engine = engine
            self.app.anthropic_engine = None
        self.router = Router()
        self.app.register_routes(self.router)
        self.server = KyoraServer(self.app, "127.0.0.1", 0, self.router)
        self.server.start()
        self.app.start(self.server.url)

    @property
    def url(self) -> str:
        return self.server.url

    def close(self) -> None:
        self.server.shutdown()
        self.app.stop()


def _request(method: str, url: str, token: str | None = None, data: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _NO_PROXY.open(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _job_doc(run_id: str = "r_test01", **extra) -> dict:
    doc = {"kind": "build", "run_id": run_id, "project_id": "p_test01", "prompt": "build the game",
           "system_prompt": "be careful", "max_turns": 5, "model": "fake-model", "fallback_models": [],
           "effort": "max", "resume_session_id": None, "judge_context": {"workspace": "/client/side/path"}}
    doc.update(extra)
    return doc


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HubRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.fake = FakeCodeEngine()
        cls.hub = _Node(root / "hub", {"hub": {"url": "", "token": HUB_TOKEN}}, hub_mode=True, engine=cls.fake)
        cls.client = _Node(root / "client", {"hub": {"url": cls.hub.url, "token": HUB_TOKEN}}, hub_mode=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.hub.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.hub.app.claude_engine = self.fake
        self.fake.block = False

    def _workspace(self) -> Path:
        ws = Path(tempfile.mkdtemp(dir=self.tmp.name, prefix="ws-"))
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (ws / "index.html").write_text("<!doctype html><title>game</title>", encoding="utf-8")
        (ws / "src").mkdir()
        (ws / "src" / "game.js").write_text("window.Game = {};\n", encoding="utf-8")
        (ws / "cache").mkdir()
        (ws / "cache" / "local-only.bin").write_bytes(b"\x00\x01")
        return ws

    def _engine_job(self, ws: Path, run_id: str, **env) -> EngineJob:
        return EngineJob(run_id=run_id, workspace=ws, prompt="build the game", system_prompt="system", max_turns=5,
                         model="fake-model", fallback_models=[], effort="max",
                         judge_context={"workspace": str(ws), "files": ["index.html"]}, env=env, kind="build",
                         project_id="p_client")

    def _client_engine(self) -> HubClientEngine:
        eng = self.client.app.hub_client
        self.assertIsInstance(eng, HubClientEngine)
        eng.poll_s = 0.5
        eng.invalidate_availability()
        return eng

    # ------------------------------------------------------------------ round trip
    def test_client_round_trip(self):
        ws = self._workspace()
        sink = RecordingSink()
        result = self._client_engine().run(self._engine_job(ws, "r_round1"), sink, threading.Event())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.backend, "hub:fake")
        self.assertEqual(result.active_model, "fake-model")
        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(result.raw["hub"]["state"], "done")
        # workspace: result arrived, local .git and the never-uploaded cache/ kept, no reserved dir left behind
        self.assertEqual((ws / "built.txt").read_text(encoding="utf-8"), "built on the hub\n")
        self.assertEqual((ws / ".git" / "HEAD").read_text(encoding="utf-8"), "ref: refs/heads/main\n")
        self.assertTrue((ws / "index.html").is_file())
        self.assertTrue((ws / "src" / "game.js").is_file())
        self.assertTrue((ws / "cache" / "local-only.bin").is_file())
        self.assertFalse((ws / ".kyora-hub").exists())
        # relayed events reached the client sink without the hub bookkeeping field
        types = sink.types()
        for t in ("engine.session", "engine.tool_use", "engine.result"):
            self.assertIn(t, types)
        session = next(e for e in sink.events if e.type == "engine.session")
        self.assertEqual(session.data["backend"], "hub:fake")
        self.assertTrue(all("hub_job_id" not in e.data for e in sink.events))
        self.assertEqual(types.count("engine.result"), 1)
        # the hub ran the job in its own workspace, with the judge context rewritten to the hub-side path
        hub_job = self.fake.jobs[-1]
        self.assertNotEqual(Path(hub_job.workspace), ws)
        self.assertEqual(hub_job.judge_context["workspace"], str(hub_job.workspace))
        self.assertTrue(str(hub_job.judge_context["run_dir"]).startswith(str(self.hub.app.home)))
        self.assertFalse((Path(hub_job.workspace) / ".git").exists())
        self.assertFalse((Path(hub_job.workspace) / "cache").exists())
        self.assertEqual(hub_job.env, {})
        # the hub's own bus carries the events tagged with the job id
        hub_events = self.hub.app.bus.replay(0, run_id="r_round1", types=["engine.*"])
        self.assertTrue(hub_events and all(e.data.get("hub_job_id") for e in hub_events))

    def test_judge_delta_reported_to_client(self):
        # Regression for honesty-run-judge-stats-miss-unjudged-calls: the Hub's per-job judge delta (what its
        # local judge counted, including an unjudged call) is carried back to the client on result.raw.
        self.fake.judge = self.hub.app.judge
        try:
            ws = self._workspace()
            result = self._client_engine().run(self._engine_job(ws, "r_judge1"), RecordingSink(), threading.Event())
        finally:
            self.fake.judge = None
        self.assertTrue(result.ok, result.error)
        delta = result.raw["hub"]["judge"]
        self.assertEqual(delta.get("allowed"), 2)      # the Write (rule allow) + the unjudged call (fail-open)
        self.assertEqual(delta.get("denied"), 1)       # git push (rule deny)
        self.assertEqual(delta.get("rule"), 2)
        self.assertEqual(delta.get("unavailable"), 1)

    def test_second_job_of_same_run_only_relays_its_own_events(self):
        ws = self._workspace()
        eng = self._client_engine()
        first = RecordingSink()
        self.assertTrue(eng.run(self._engine_job(ws, "r_twice"), first, threading.Event()).ok)
        second = RecordingSink()
        job = self._engine_job(ws, "r_twice")
        job.kind = "fix"
        job.resume_session_id = "sess-1"
        res = eng.run(job, second, threading.Event())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(second.types().count("engine.result"), 1)
        # resume: the fix job reuses the build job's hub workspace so the engine session's cwd matches
        self.assertEqual(self.fake.jobs[-1].workspace, self.fake.jobs[-2].workspace)
        self.assertEqual(self.fake.jobs[-1].resume_session_id, "sess-1")

    # ------------------------------------------------------------------ auth
    def test_status_requires_token(self):
        status_url = self.hub.url + "/api/code-engine/status"
        self.assertEqual(_request("GET", status_url)[0], 401)
        self.assertEqual(_request("GET", status_url, token="wrong")[0], 401)
        code, body = _request("GET", status_url, token=HUB_TOKEN)
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertTrue(doc["ok"] and doc["hub"])
        self.assertTrue(doc["engine"]["available"])
        self.assertEqual(doc["engine"]["backend"], "fake")
        self.assertEqual(doc["engine"]["details"]["logged_in"], True)
        self.assertIn("running", doc["queue"])
        self.assertIsInstance(doc["node"], bool)
        self.assertIsInstance(doc["git"], bool)

    def test_routes_absent_without_hub_mode(self):
        self.assertEqual(_request("GET", self.client.url + "/api/code-engine/status", token=HUB_TOKEN)[0], 404)

    # ------------------------------------------------------------------ jobs over HTTP
    def test_cancel_endpoint_and_result_409(self):
        self.fake.block = True
        self.fake.started.clear()
        body, ctype = encode_multipart([
            ("job", None, "application/json", json.dumps(_job_doc("r_cancel1")).encode("utf-8")),
            ("workspace", "workspace.zip", "application/zip", _zip({"index.html": b"<html></html>"}))])
        code, raw = _request("POST", self.hub.url + "/api/code-engine/jobs", HUB_TOKEN, body, {"Content-Type": ctype})
        self.assertEqual(code, 201, raw)
        job_id = json.loads(raw)["job_id"]
        self.assertTrue(self.fake.started.wait(10))
        base = f"{self.hub.url}/api/code-engine/jobs/{job_id}"
        self.assertEqual(json.loads(_request("GET", base, HUB_TOKEN)[1])["state"], "running")
        self.assertEqual(_request("GET", base + "/result", HUB_TOKEN)[0], 409)
        code, raw = _request("POST", base + "/cancel", HUB_TOKEN, b"")
        self.assertEqual(code, 200)
        deadline = time.time() + 10
        state = None
        while time.time() < deadline:
            state = json.loads(_request("GET", base, HUB_TOKEN)[1])["state"]
            if state == "cancelled":
                break
            time.sleep(0.1)
        self.assertEqual(state, "cancelled")
        code, data = _request("GET", base + "/result", HUB_TOKEN)
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn(RESULT_ENTRY, zf.namelist())
            self.assertEqual(json.loads(zf.read(RESULT_ENTRY))["state"], "cancelled")
        self.assertEqual(_request("POST", f"{self.hub.url}/api/code-engine/jobs/hj_ffffff/cancel", HUB_TOKEN, b"")[0], 404)
        self.assertEqual(_request("GET", f"{self.hub.url}/api/code-engine/jobs/..%2Fx", HUB_TOKEN)[0], 404)

    def test_bad_submissions_are_rejected(self):
        url = self.hub.url + "/api/code-engine/jobs"
        body, ctype = encode_multipart([("job", None, "application/json", b"{}"),
                                        ("workspace", "w.zip", "application/zip", _zip({"a": b"1"}))])
        self.assertEqual(_request("POST", url, HUB_TOKEN, body, {"Content-Type": ctype})[0], 400)
        body, ctype = encode_multipart([("job", None, "application/json", json.dumps(_job_doc()).encode()),
                                        ("workspace", "w.zip", "application/zip", _zip({"../escape.txt": b"x"}))])
        self.assertEqual(_request("POST", url, HUB_TOKEN, body, {"Content-Type": ctype})[0], 400)
        body, ctype = encode_multipart([("job", None, "application/json", json.dumps(_job_doc()).encode())])
        self.assertEqual(_request("POST", url, HUB_TOKEN, body, {"Content-Type": ctype})[0], 400)

    def test_client_cancel(self):
        self.fake.block = True
        cancel = threading.Event()
        sink = RecordingSink(on_event=lambda ev: cancel.set() if ev.type == "engine.tool_use" else None)
        ws = self._workspace()
        res = self._client_engine().run(self._engine_job(ws, "r_ccancel"), sink, cancel)
        self.assertFalse(res.ok)
        self.assertEqual(res.backend, "hub:fake")
        self.assertIn("cancel", (res.error or "").lower())
        self.assertFalse((ws / "built.txt").exists())
        self.assertTrue((ws / ".git" / "HEAD").exists())

    # ------------------------------------------------------------------ availability
    def test_availability_reasons(self):
        bus = EventBus(None)
        try:
            av = HubClientEngine(HubCfg(url="", token="x", expose_ui=False), bus).availability()
            self.assertFalse(av.available)
            self.assertEqual(av.reason, "hub.url not set")
            dead = HubClientEngine(HubCfg(url=f"http://127.0.0.1:{_free_port()}", token="x", expose_ui=False), bus)
            av = dead.availability()
            self.assertFalse(av.available)
            self.assertTrue(av.reason.startswith("hub unreachable:"), av.reason)
            bad = HubClientEngine(HubCfg(url=self.hub.url, token="not-the-token", expose_ui=False), bus).availability()
            self.assertFalse(bad.available)
            self.assertIn("401", bad.reason)
            ok = HubClientEngine(HubCfg(url=self.hub.url, token=HUB_TOKEN, expose_ui=False), bus).availability()
            self.assertTrue(ok.available, ok.reason)
            self.assertEqual(ok.details["remote_backend"], "fake")
            self.hub.app.claude_engine = None
            none = HubClientEngine(HubCfg(url=self.hub.url, token=HUB_TOKEN, expose_ui=False), bus).availability()
            self.assertFalse(none.available)
            self.assertTrue(none.reason.startswith("hub has no code engine"), none.reason)
        finally:
            self.hub.app.claude_engine = self.fake
            bus.close()

    def test_job_fails_honestly_without_hub_engine(self):
        self.hub.app.claude_engine = None
        ws = self._workspace()
        sink = RecordingSink()
        res = self._client_engine().run(self._engine_job(ws, "r_noeng"), sink, threading.Event())
        self.assertFalse(res.ok)
        self.assertIn("hub has no code engine", res.error)
        self.assertIn("engine.error", sink.types())
        self.assertFalse((ws / "built.txt").exists())

    def test_client_follows_live_settings(self):
        eng = self.client.app.hub_client
        self.assertEqual(eng.hub_settings(), (self.hub.url, HUB_TOKEN))
        self.client.app.config.raw_keys["hub"]["url"] = ""
        try:
            self.assertEqual(eng.availability().reason, "hub.url not set")
        finally:
            self.client.app.config.raw_keys["hub"]["url"] = self.hub.url
            eng.invalidate_availability()


class HubLockoutTests(unittest.TestCase):
    def test_429_after_20_bad_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            hub = _Node(Path(d), {"hub": {"url": "", "token": HUB_TOKEN}}, hub_mode=True, engine=FakeCodeEngine())
            try:
                url = hub.url + "/api/code-engine/status"
                codes = [_request("GET", url, token=f"bad-{i}")[0] for i in range(20)]
                self.assertEqual(codes, [401] * 20)
                code, body = _request("GET", url, token=HUB_TOKEN)
                self.assertEqual(code, 429)
                self.assertIn("retry_after", json.loads(body))
            finally:
                hub.close()

    def test_auth_window_and_expiry_with_fake_clock(self):
        now = [1000.0]

        class Req:
            def __init__(self, tok, ip="10.0.0.2"):
                self.tok, self.client_ip = tok, ip

            def bearer(self):
                return self.tok

        auth = HubAuth(lambda: "secret", clock=lambda: now[0])
        self.assertEqual(auth.check(Req("secret")), 200)
        for _ in range(19):
            self.assertEqual(auth.check(Req("nope")), 401)
        now[0] += 61  # the 19 failures fall out of the 60 s window
        for _ in range(19):
            self.assertEqual(auth.check(Req("nope")), 401)
        self.assertEqual(auth.check(Req("secret")), 200)  # success clears the history
        for _ in range(20):
            auth.check(Req("nope"))
        self.assertEqual(auth.check(Req("secret")), 429)
        self.assertEqual(auth.check(Req("secret", ip="10.0.0.3")), 200)  # per IP
        self.assertGreater(auth.retry_after("10.0.0.2"), 0)
        now[0] += 60
        self.assertEqual(auth.check(Req("secret")), 200)
        self.assertEqual(HubAuth("").check(Req("")), 401)  # no token configured: never authorized


class HubStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jobs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_result_zip_and_expire(self):
        store = HubJobStore(self.root)
        job = store.create(_job_doc(), _zip({"index.html": b"<html>", "node_modules/x/i.js": b"1",
                                             ".git/config": b"[core]", "src/a.js": b"a"}))
        self.assertTrue((job.dir / "job.json").is_file())
        self.assertTrue((job.workspace / "index.html").is_file())
        self.assertFalse((job.workspace / ".git").exists())  # .git entries are never extracted
        with self.assertRaises(HubJobNotFinished):
            store.result_zip(job.id)
        (job.workspace / "built.txt").write_text("x", encoding="utf-8")
        store.set_state(job.id, "done", result={"ok": True, "backend": "fake", "session_id": "s9"})
        self.assertEqual(json.loads((job.dir / "result.json").read_text(encoding="utf-8"))["backend"], "fake")
        with zipfile.ZipFile(io.BytesIO(store.result_zip(job.id))) as zf:
            names = zf.namelist()
        self.assertIn("built.txt", names)
        self.assertIn("src/a.js", names)
        self.assertIn(RESULT_ENTRY, names)
        self.assertFalse(any(n.startswith(("node_modules/", ".git/")) for n in names))
        self.assertEqual(store.find_by_session("s9").id, job.id)
        running = store.create(_job_doc("r_other"), _zip({"a.txt": b"a"}))
        store.set_state(running.id, "running")
        # reload: a job left running by a dead process is failed, finished ones survive
        again = HubJobStore(self.root)
        self.assertEqual(again.get(running.id).state, "failed")
        self.assertEqual(again.get(job.id).state, "done")
        self.assertEqual([j.id for j in again.list()], [job.id, running.id])
        self.assertIsNone(again.get("../etc"))
        self.assertEqual(again.expire(hours=24), [])
        removed = again.expire(hours=0)
        self.assertEqual(sorted(removed), sorted([job.id, running.id]))
        self.assertFalse(job.dir.exists())

    def test_unsafe_zip_leaves_nothing(self):
        store = HubJobStore(self.root)
        for bad in (_zip({"../evil.txt": b"x"}), _zip({"/abs.txt": b"x"}), b"not a zip"):
            with self.assertRaises(ValueError):
                store.create(_job_doc(), bad)
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(ValueError):
            store.create({"run_id": "r1"}, _zip({"a": b"1"}))  # no prompt

    def test_fallback_unzip(self):
        dest = Path(self.tmp.name) / "dest"
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "HEAD").write_text("h", encoding="utf-8")
        (dest / "old.txt").write_text("old", encoding="utf-8")
        written = fallback_unzip(_zip({"new/a.txt": b"a", ".git/hooks/pre-commit": b"evil"}), dest, replace=True)
        self.assertEqual(written, ["new/a.txt"])
        self.assertFalse((dest / "old.txt").exists())
        self.assertEqual((dest / ".git" / "HEAD").read_text(encoding="utf-8"), "h")
        self.assertFalse((dest / ".git" / "hooks").exists())
        for bad in ({"../x": b"1"}, {"/x": b"1"}, {"C:/x": b"1"}, {"a/../../x": b"1"}):
            with self.assertRaises(ValueError):
                fallback_unzip(_zip(bad), dest)
        link = io.BytesIO()
        with zipfile.ZipFile(link, "w") as zf:
            info = zipfile.ZipInfo("ln")
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError):
            fallback_unzip(link.getvalue(), dest)

    def test_zip_tree_exclusions(self):
        src = Path(self.tmp.name) / "src"
        for rel in ("index.html", ".git/HEAD", "node_modules/a/b.js", "cache/c.bin", "src/cache/lru.js",
                    "lib/node_modules/z.js"):
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rel, encoding="utf-8")
        with zipfile.ZipFile(io.BytesIO(zip_tree(src, exclude_any=(".git", "node_modules"),
                                                 exclude_top=("cache",)))) as zf:
            self.assertEqual(sorted(zf.namelist()), ["index.html", "src/cache/lru.js"])
        self.assertEqual(CLIENT_KEEP, (".git", "node_modules", "cache"))


class HubHelperTests(unittest.TestCase):
    def test_link_payload(self):
        link = link_payload("http://192.168.1.20:8765/", "abc_DEF-123")
        self.assertEqual(link, "http://127.0.0.1:8765/#hub=http%3A%2F%2F192.168.1.20%3A8765&token=abc_DEF-123")
        self.assertTrue(link_payload("http://10.0.0.2:9000", "t", ui_url="http://localhost:9999/")
                        .startswith("http://localhost:9999/#hub=http%3A%2F%2F10.0.0.2%3A9000&token=t"))

    def test_no_engine_reason_text(self):
        self.assertEqual(NO_ENGINE_REASON,
                         "hub has no code engine: install Claude Code and run claude login on the hub")


if __name__ == "__main__":
    unittest.main()
