"""One Kyora per KYORA_HOME, and the bench's embedded-server fallback."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from kyora.util.instance_lock import InstanceLock

REPO = Path(__file__).resolve().parent.parent


def _tempdir(test: unittest.TestCase, prefix: str) -> str:
    """A temp dir removed after every other cleanup: Windows cannot delete a held lock or an open sqlite file."""
    d = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _busy_port(test: unittest.TestCase) -> int:
    s = socket.socket()
    test.addCleanup(s.close)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s.getsockname()[1]


class InstanceLockTests(unittest.TestCase):
    def test_second_lock_on_the_same_home_is_refused_until_released(self):
        with tempfile.TemporaryDirectory(prefix="kyora-lock-") as d:
            first, second = InstanceLock(d), InstanceLock(d)
            self.assertTrue(first.acquire())
            self.addCleanup(first.release)
            self.assertFalse(second.acquire())
            self.assertTrue(first.holder_pid().isdigit())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_different_homes_do_not_conflict(self):
        with tempfile.TemporaryDirectory(prefix="kyora-lock-a-") as a, \
                tempfile.TemporaryDirectory(prefix="kyora-lock-b-") as b:
            la, lb = InstanceLock(a), InstanceLock(b)
            self.assertTrue(la.acquire())
            self.assertTrue(lb.acquire())
            la.release()
            lb.release()

    def test_serve_refuses_a_home_held_by_another_kyora_before_starting_the_app(self):
        d = _tempdir(self, "kyora-lock-cli-")
        if True:
            config_dir = Path(d) / "config"
            shutil.copytree(REPO / "config", config_dir)
            home = Path(d) / "home"
            held = InstanceLock(home)
            self.assertTrue(held.acquire())
            self.addCleanup(held.release)
            from kyora import __main__ as cli
            from kyora.app import App
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(App, "start") as start, mock.patch("logging.basicConfig"), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["serve", "--config", str(config_dir), "--home", str(home),
                               "--host", "127.0.0.1", "--port", "0"])
            self.assertEqual(rc, 2)
            start.assert_not_called()  # app.start() would block the other instance's live runs
            self.assertIn("Another Kyora is already running with this home", err.getvalue())

    def test_docs_describe_the_home_lock_and_windows_exclusive_ports(self):
        for rel in ("README.md", "docs/hub-mode.md"):
            text = " ".join((REPO / rel).read_text(encoding="utf-8").split())
            self.assertIn("kyora.lock", text, rel)
            self.assertIn("SO_EXCLUSIVEADDRUSE", text, rel)
            self.assertNotIn("started on a port that is already in use stops with an error", text, rel)


class BenchEmbeddedServerTests(unittest.TestCase):
    def test_busy_main_port_and_explicit_preview_port_fall_back_to_ephemeral_ports(self):
        d = _tempdir(self, "kyora-bench-srv-")
        if True:
            config_dir = Path(d) / "config"
            shutil.copytree(REPO / "config", config_dir)
            settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
            settings["server"]["port"] = _busy_port(self)
            settings["server"]["preview_port"] = _busy_port(self)
            (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            from kyora.app import App
            from kyora.bench.runner import BenchRunner
            from kyora.config import Config
            app = App(Config.load(config_dir), home=Path(d) / "home")
            self.addCleanup(app.stop)
            runner = BenchRunner(app)
            runner._ensure_server()
            server = runner._server
            self.addCleanup(server.shutdown)
            self.assertNotEqual(server.port, settings["server"]["port"])
            self.assertTrue(app.started.is_set())
            self.assertTrue(app.preview_url)


if __name__ == "__main__":
    unittest.main()
