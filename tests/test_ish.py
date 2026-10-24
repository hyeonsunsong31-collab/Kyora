"""iSH (iOS): Safari must reach Kyora while Safari, not iSH, is the app in front.

iOS pauses iSH a few seconds after another app comes to the front, and may then reclaim its
listening socket (Apple TN2277). Kyora holds iSH's location stream open so iSH keeps running
(``kyora.util.ish``), and on iSH the server probes its listeners and opens a lost one again on the
same port (``KyoraServer(watch_s=...)``). The location stream is simulated with a FIFO, a reclaimed
listener by closing the listening socket under the running server.
"""
from __future__ import annotations

import io
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kyora import __main__ as cli
from kyora.server import KyoraServer, Response, Router
from kyora.util import ish


def _router() -> Router:
    r = Router()
    r.add("GET", "/ping", lambda req, p: Response.json({"ok": True}), auth="any")
    return r


def _app() -> SimpleNamespace:
    return SimpleNamespace(preview_url="", config=SimpleNamespace(
        server=SimpleNamespace(preview_port=None, allowed_hosts=[]), hub=SimpleNamespace(token="", expose_ui=False)))


def _get(port: int, timeout: float = 2.0) -> int | None:
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/ping")
        return conn.getresponse().status
    except OSError:
        return None
    finally:
        conn.close()


def _until(pred, timeout: float = 10.0, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


@unittest.skipUnless(hasattr(os, "mkfifo"), "needs a POSIX FIFO to stand in for /dev/location")
class BackgroundKeeperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = os.path.join(self.tmp.name, "location")
        os.mkfifo(self.device)

    def test_updates_are_counted_and_the_location_is_not_kept(self):
        keeper = ish.BackgroundKeeper(self.device)
        self.assertEqual(keeper.start(), "waiting")  # iSH: iOS asks for access, the first fix is pending
        with open(self.device, "wb", buffering=0) as writer:
            writer.write(b"+37.334900,-122.009020\n")
            self.assertTrue(_until(lambda: keeper.state == "running"), keeper.state)
            writer.write(b"+37.334901,-122.009021\n")
            self.assertTrue(_until(lambda: keeper.updates == 2), keeper.updates)
            keeper_text = repr(vars(keeper))
            self.assertNotIn("37.3349", keeper_text, "no location is kept")
            self.assertNotIn("122.009", keeper_text)
        self.assertTrue(_until(lambda: keeper.state == "failed"), keeper.state)  # the stream ended
        self.assertIn("ended", keeper.detail)
        self.assertIn("ended", ish.no_update_note(keeper))
        keeper.stop()

    def test_waiting_gets_the_permission_hint(self):
        keeper = ish.BackgroundKeeper(self.device)
        keeper.start()
        note = ish.no_update_note(keeper)
        self.assertIn("Don't Allow", note)
        self.assertIn("Location Services > iSH", note)
        keeper.stop()
        with open(self.device, "wb"):  # let the blocked open() return so the thread ends
            pass

    def test_notes_say_what_to_open_and_why_ish_keeps_running(self):
        keeper = ish.BackgroundKeeper(self.device)
        keeper.start()
        lines = ish.serve_notes("http://127.0.0.1:8765", keeper)
        self.assertIn("open http://127.0.0.1:8765", lines[0])
        self.assertIn("Allow While Using App", lines[1])
        self.assertIn("KYORA_ISH_BACKGROUND=0", lines[1])
        keeper.stop()
        with open(self.device, "wb"):
            pass


class BackgroundSwitchTests(unittest.TestCase):
    def test_missing_device_is_reported_with_a_way_out(self):
        keeper = ish.BackgroundKeeper(os.path.join(tempfile.gettempdir(), "kyora-no-such-location-device"))
        self.assertEqual(keeper.start(), "unavailable")
        line = ish.serve_notes("http://127.0.0.1:8765", keeper)[1]
        self.assertIn("does not exist", line)
        self.assertIn("Split View", line)
        self.assertIsNone(ish.no_update_note(keeper))

    def test_disable_values(self):
        for value in ("0", "off", "false", "no", " OFF "):
            self.assertTrue(ish.background_disabled({ish.DISABLE_ENV: value}), value)
        for value in ("", "1", "on", "yes"):
            self.assertFalse(ish.background_disabled({ish.DISABLE_ENV: value}), value)
        self.assertFalse(ish.background_disabled({}))
        self.assertIn("background mode is off", ish.serve_notes("http://127.0.0.1:8765", None)[1])

    def test_serve_on_ish_prints_the_safari_address_and_the_background_state(self):
        missing = os.path.join(tempfile.gettempdir(), "kyora-no-such-location-device")
        out = io.StringIO()
        with mock.patch.object(ish, "LOCATION_DEVICE", missing), mock.patch.dict(os.environ, {ish.DISABLE_ENV: ""}), \
                redirect_stdout(out):
            keeper = cli._ish_background("http://127.0.0.1:8765")
        self.assertEqual(keeper.state, "unavailable")
        self.assertIn("iSH: in Safari, open http://127.0.0.1:8765", out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {ish.DISABLE_ENV: "0"}), redirect_stdout(out):
            self.assertIsNone(cli._ish_background("http://127.0.0.1:8765"))
        self.assertIn("background mode is off", out.getvalue())


class ListenerWatchTests(unittest.TestCase):
    def _server(self, watch_s=0.05) -> KyoraServer:
        srv = KyoraServer(_app(), "127.0.0.1", 0, _router(), preview_router=_router(), watch_s=watch_s)
        self.addCleanup(srv.shutdown)
        srv.start()
        return srv

    @unittest.skipIf(os.name == "nt", "the watch runs on iSH only; Windows' exclusive bind (SO_EXCLUSIVEADDRUSE) "
                                      "keeps a port unavailable while connections it accepted linger")
    def test_a_reclaimed_listener_listens_again_on_the_same_port(self):
        # iOS may take the listening socket of a paused app away; afterwards nothing listens on the port
        srv = self._server()
        port, preview = srv.port, srv.preview_port
        self.assertEqual(_get(port), 200)
        srv.httpd.socket.close()
        srv.preview_httpd.socket.close()
        self.assertTrue(_until(lambda: _get(port, timeout=0.5) == 200), "the main listener was not opened again")
        self.assertTrue(_until(lambda: _get(preview, timeout=0.5) == 200), "the preview listener was not opened again")
        self.assertEqual((srv.port, srv.preview_port), (port, preview))
        self.assertGreaterEqual(srv.relistens, 2)

    def test_a_port_that_cannot_be_opened_again_is_retried_and_the_server_still_stops(self):
        srv = self._server(watch_s=None)
        with mock.patch.object(srv.httpd, "server_bind", side_effect=OSError("address in use")), \
                self.assertLogs("kyora.server", "WARNING") as logs:
            self.assertFalse(srv._relisten(srv.httpd, "kyora-server"))
        self.assertIn("could not listen again", "\n".join(logs.output))
        self.assertEqual(srv.relistens, 0)
        srv.shutdown()  # the listener's loop had ended: shutdown must not wait for it

    def test_an_answering_listener_is_left_alone(self):
        srv = self._server()
        time.sleep(0.6)  # about ten probes of each listener
        self.assertEqual(srv.relistens, 0)
        self.assertEqual(_get(srv.port), 200)

    def test_no_watch_without_watch_s(self):
        srv = self._server(watch_s=None)
        self.assertIsNone(srv._watch)
        self.assertNotIn("kyora-listener-watch", [t.name for t in threading.enumerate()])

    def test_serve_forever_returns_after_shutdown_and_the_watch_ends(self):
        srv = KyoraServer(_app(), "127.0.0.1", 0, _router(), watch_s=0.05)
        runner = threading.Thread(target=srv.serve_forever, daemon=True)
        runner.start()
        self.assertTrue(_until(lambda: _get(srv.port, timeout=0.5) == 200))
        srv.shutdown()
        runner.join(5)
        self.assertFalse(runner.is_alive(), "serve_forever did not return")
        self.assertFalse(srv._watch.is_alive(), "the watch did not end")
        self.assertIsNone(_get(srv.port, timeout=0.5))

    def test_start_twice_serves_each_listener_once(self):
        srv = self._server(watch_s=None)
        before = len(srv._threads)
        srv.start()
        self.assertEqual(len(srv._threads), before)


class DetectionTests(unittest.TestCase):
    def test_the_location_device_marks_ish(self):
        from kyora.util import fs
        real_exists, real_isdir = os.path.exists, os.path.isdir
        hidden = ("/proc/ish", "/etc/alpine-release", "/data/data/com.termux")  # this host's own markers
        with mock.patch("platform.system", return_value="Linux"), mock.patch("platform.machine", return_value="x86_64"), \
                mock.patch.object(fs.os.path, "exists", lambda p: p == "/dev/location" or (
                    p not in hidden and real_exists(p))), \
                mock.patch.object(fs.os.path, "isdir", lambda p: p not in hidden and real_isdir(p)), \
                mock.patch.dict(os.environ, {"TERMUX_VERSION": ""}):
            info = fs.platform_info()
        if os.name == "nt":
            self.assertEqual(info["kind"], "windows")
        else:
            self.assertEqual((info["kind"], info["is_ish"]), ("ish", True))


class LauncherTextTests(unittest.TestCase):
    def test_launcher_and_installer_no_longer_ask_for_the_impossible(self):
        root = Path(__file__).resolve().parent.parent / "launchers"
        for name in ("kyora-ish.sh", "install-ish.sh"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("keep iSH in the foreground", text.lower(), name)
            self.assertNotIn("localhost:8765", text, name)
            self.assertIn("127.0.0.1", text, name)


if __name__ == "__main__":
    unittest.main()
