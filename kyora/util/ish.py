"""iSH (iOS): keep Kyora answering Safari while Safari is the app in front.

iOS suspends an app a few seconds after another app comes to the front. A suspended iSH cannot
answer: Safari's connection to ``http://127.0.0.1:8765`` is accepted by the kernel but never served,
and the page does not load. After a suspension iOS may also reclaim the listening socket, so Kyora
is not listening any more even after iSH is in front again (Apple TN2277, "Networking and
Multitasking"); :class:`kyora.server.KyoraServer`'s listener watch handles that part.

iSH's documented way to keep running in the background is an open location stream: reading
``/dev/location`` makes iSH ask iOS for location access and turn on background location updates
(iSH wiki, "Running in background"). :class:`BackgroundKeeper` opens it in a daemon thread and
discards everything it reads - no location is kept, logged or sent; only the number of updates is
counted. A read blocks until the first fix, and iSH reports no error when access was refused, so
"waiting" can mean either. ``KYORA_ISH_BACKGROUND=0`` turns the keeper off.
"""
from __future__ import annotations

import os
import threading

LOCATION_DEVICE = "/dev/location"
DISABLE_ENV = "KYORA_ISH_BACKGROUND"
_OFF_VALUES = {"0", "off", "false", "no"}
# seconds without a location update after which the banner says what a refused permission means
NO_UPDATE_HINT_S = 45.0


def background_disabled(environ=None) -> bool:
    """``KYORA_ISH_BACKGROUND`` is 0/off/false/no."""
    value = (environ if environ is not None else os.environ).get(DISABLE_ENV, "")
    return value.strip().lower() in _OFF_VALUES


class BackgroundKeeper:
    """Holds iSH's location stream open so iOS keeps iSH running while another app is in front.

    ``state``: ``off`` (not started), ``unavailable`` (no such device), ``waiting`` (opened, no
    update yet: iOS may still be asking, there is no fix yet, or access was refused), ``running``
    (updates arrive), ``failed`` (the stream could not be read or ended; ``detail`` says why).
    """

    def __init__(self, path: str = LOCATION_DEVICE):
        self.path = path
        self.state = "off"
        self.detail = ""
        self.updates = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        if not os.path.exists(self.path):
            self.state, self.detail = "unavailable", f"{self.path} does not exist"
            return self.state
        self.state = "waiting"
        self._thread = threading.Thread(target=self._run, name="kyora-ish-background", daemon=True)
        self._thread.start()
        return self.state

    def _run(self) -> None:
        try:
            with open(self.path, "rb", buffering=0) as stream:
                while not self._stop.is_set():
                    chunk = stream.read(256)
                    if not chunk:
                        break
                    self.updates += chunk.count(b"\n")  # the location itself is dropped here
                    del chunk
                    if self.state == "waiting":
                        self.state = "running"
        except OSError as exc:
            if not self._stop.is_set():
                self.state, self.detail = "failed", f"{self.path} could not be read ({type(exc).__name__}: {exc})"
            return
        if not self._stop.is_set():
            self.state, self.detail = "failed", f"{self.path} ended"

    def stop(self) -> None:
        """Stop counting; the blocked read ends with the process (the thread is a daemon)."""
        self._stop.set()


def serve_notes(url: str, keeper: BackgroundKeeper | None) -> list[str]:
    """Lines ``kyora serve`` prints on iSH: where to go in Safari and whether iSH keeps running."""
    lines = [f"iSH: in Safari, open {url} (type the whole address, with http://)."]
    if keeper is None:
        lines.append(f"iSH: background mode is off ({DISABLE_ENV}=0): iOS pauses iSH a few seconds after Safari "
                     "comes to the front and the page stops loading. On an iPad put iSH and Safari side by side "
                     "(Split View); on an iPhone start Kyora without that setting.")
    elif keeper.state == "unavailable":
        lines.append(f"iSH: {keeper.detail}, so iSH cannot keep running in the background: iOS pauses it a few "
                     "seconds after Safari comes to the front and the page stops loading. Update iSH from the "
                     "App Store, or on an iPad put iSH and Safari side by side (Split View).")
    else:
        lines.append("iSH: Kyora keeps iSH running while Safari is in front by holding iSH's location stream "
                     "open. iOS asks once for location access for iSH: choose \"Allow While Using App\". Kyora "
                     f"keeps no location (it discards the stream). Turn this off with {DISABLE_ENV}=0.")
    return lines


def no_update_note(keeper: BackgroundKeeper) -> str | None:
    """A hint when no location update has arrived after a while (refused access looks the same as no fix)."""
    if keeper.state == "waiting":
        return ("iSH: no location update has arrived yet. If you chose \"Don't Allow\", iOS pauses iSH while "
                "Safari is in front: allow it in Settings > Privacy & Security > Location Services > iSH "
                "(While Using the App), then restart Kyora. Indoors the first update can take a while.")
    if keeper.state == "failed":
        return f"iSH: {keeper.detail}: iOS may pause iSH while Safari is in front."
    return None


__all__ = ["BackgroundKeeper", "LOCATION_DEVICE", "DISABLE_ENV", "NO_UPDATE_HINT_S", "background_disabled",
           "serve_notes", "no_update_note"]
