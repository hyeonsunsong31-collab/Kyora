"""KyoraProcessor: the Kyora overlay frame processor for NVIDIA Tokkio 5.0.0-ga.

Copyright (c) 2026 Kyora contributors. MIT licence (Kyora code only). This file is
Kyora's own addition; it is NOT part of the vendored NVIDIA tree and it does not
modify it.

What it does
------------
Tokkio is a presenter, not a UI generator. This processor turns the avatar into a
live presenter of a Kyora run:

* It subscribes to Kyora's event stream
  ``GET <events_url>/api/events?types=tokkio.narration,tokkio.view,tokkio.clear``
  (Server-Sent Events, ``Authorization: Bearer <token>``, resumable with
  ``Last-Event-ID``).
* ``tokkio.narration {text}``  -> the avatar speaks ``text`` (``TTSSpeakFrame``).
* ``tokkio.view {title, preview_url, screenshot_b64?, stages, buttons}`` -> a
  Tokkio *custom view* (header, preview link text, screenshot, stage table and
  action buttons) is shown in the Tokkio UI (``StartCustomViewFrame``).
* ``tokkio.clear`` -> the custom view is hidden (``StopCustomViewFrame``).
* Button presses in the Tokkio UI whose id starts with ``kyora:`` are posted back
  to Kyora as ``POST <events_url>/api/actions {action, run_id, project_id}``; the
  ``{"url": ...}`` Kyora answers (Open preview, Download ZIP, Run tests) is shown in
  the view as an absolute link. Preview links name the Kyora host the cluster
  reaches (``events_url``), never ``127.0.0.1``.

Nothing is invented: every spoken sentence and every block comes verbatim from a
Kyora event. When Kyora is unreachable the processor logs and retries with
backoff; the Tokkio pipeline keeps running without narration.

APIs relied on (verified against the released wheels, not from memory)
----------------------------------------------------------------------
pipecat-ai 0.0.68 (``pip download pipecat-ai==0.0.68``):
* ``pipecat/processors/frame_processor.py``: ``FrameProcessor.process_frame`` (handles
  ``StartFrame``/``CancelFrame`` in the base class and must be awaited first),
  ``push_frame(frame, direction)`` (queues non-system frames on the processor's
  single push task; refuses frames before ``StartFrame``), ``create_task(coro,
  name)`` / ``cancel_task(task)`` (processor-owned asyncio tasks tracked by the
  pipeline's ``TaskManager``), ``cleanup()``.
* ``pipecat/pipeline/task.py``: ``PipelineTask.queue_frame(frame)`` puts a frame on
  the task's push queue, which ``_process_push_queue`` hands to the
  ``PipelineTaskSource`` linked *in front of* the first processor.
* ``pipecat/frames/frames.py``: ``TTSSpeakFrame(text)`` (DataFrame), ``StartFrame``
  and ``CancelFrame`` (SystemFrame), ``EndFrame`` (ControlFrame).
nvidia-pipecat 0.2.0 (``pip download nvidia-pipecat==0.2.0``):
* ``nvidia_pipecat/frames/custom_view.py``: ``StartCustomViewFrame(blocks=[...])``
  (a ``StartActionFrame`` with kw-only ``action_id``), ``StopCustomViewFrame(action_id=)``,
  ``HeaderBlock``, ``TextBlock``, ``ImageBlock``/``Image``, ``TableBlock``,
  ``ButtonListBlock``/``Button``/``ButtonVariant`` (pydantic models with an ``id``),
  ``UIButtonPressFrame(component_id, action_id)``.
* ``nvidia_pipecat/transports/network/ace_fastapi_websocket.py`` (``_receive_messages``):
  every message from the Tokkio UI is deserialized by the configured serializer and
  pushed into the pipeline with ``push_frame`` from the input transport.
* vendored ``src/llm-rag/src/serializer.py`` (``TokkioUIWebSocketSerializer``): the
  UI message ``{"type": "button_pressed", "id": ...}`` becomes
  ``UIButtonPressFrame(component_id=id, action_id="")``; ``StartCustomViewFrame`` /
  ``StopCustomViewFrame`` are serialized to ``{"type": "custom_view", ...}``.
* vendored ``src/llm-rag/src/rag_multimodal_response_processor.py``: the reference
  for pushing ``StartCustomViewFrame`` downstream from a processor placed after TTS.
* vendored ``customization-notebooks/09_create_frame_processor.ipynb``: the
  ``super().process_frame`` first / ``push_frame`` pass-through pattern.

HTTP client
-----------
``httpx`` is used. It is not a *direct* dependency of pipecat-ai 0.0.68 (its direct
HTTP dependency is ``aiohttp~=3.11.12``), but it is a hard transitive dependency via
``openai~=1.70.0`` (required by both pipecat-ai and nvidia-pipecat), it is locked in
the vendored ``src/llm-rag/uv.lock`` (httpx 0.27.2), and
``nvidia_pipecat/services/nvidia_llm.py`` - which the vendored ``bot.py`` imports -
does ``import httpx`` itself. So wherever ``bot.py`` runs, httpx is present and no
pyproject change is needed.

Why narration goes through ``PipelineTask.queue_frame`` and not ``push_frame``
---------------------------------------------------------------------------
In the vendored pipeline (``bot.py``) the processors are ordered
``transport.input -> user_presence -> stt -> ... -> llm -> proactivity -> tts ->
tts_transcript_sync -> rag_multimodal_response_processor -> [KyoraProcessor] ->
a2f -> posture -> animgraph -> context_aggregator.assistant -> transport.output``.
This processor sits *after* the TTS service. ``push_frame(TTSSpeakFrame, DOWNSTREAM)``
would hand the frame to Audio2Face, the animation graph and the transport output,
none of which synthesize speech, so nothing would be spoken. Pushing it UPSTREAM is
not how pipecat delivers data frames either (upstream is for control/error
signalling and the TTS service's ``process_frame`` is designed for its downstream
input). ``PipelineTask.queue_frame`` injects the frame at the pipeline head, so it
travels the normal path: user-presence gating (no narration to an empty room),
the TTS service (speech), transcript synchronization (caption in the UI),
Audio2Face and the animation graph, and interruption handling all apply.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable

try:  # loguru ships with pipecat; plain logging keeps this module importable elsewhere
    from loguru import logger
except ImportError:  # pragma: no cover - only outside the ACE controller environment
    import logging as _logging

    logger = _logging.getLogger("kyora_processor")  # type: ignore[assignment]

from pipecat.frames.frames import CancelFrame, EndFrame, Frame, StartFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from nvidia_pipecat.frames.custom_view import (
    Button,
    ButtonListBlock,
    ButtonVariant,
    HeaderBlock,
    Image,
    ImageBlock,
    StartCustomViewFrame,
    StopCustomViewFrame,
    TableBlock,
    TextBlock,
    UIButtonPressFrame,
)

if TYPE_CHECKING:  # type hints only; bot.py imports PipelineTask itself
    from pipecat.pipeline.task import PipelineTask

EVENT_TYPES = ("tokkio.narration", "tokkio.view", "tokkio.clear")
VIEW_ACTION_ID = "kyora-view"
BUTTON_PREFIX = "kyora:"
MAX_BACKOFF_S = 60.0
CONNECT_TIMEOUT_S = 10.0
# Kyora sends ": keepalive" every 15 s; a read stall longer than this means a dead connection.
READ_TIMEOUT_S = 90.0
# Events that were persisted before this processor connected are not replayed to the avatar
# (they would narrate history). Only live events and events missed between reconnects are used.
INITIAL_REPLAY_GRACE_S = 5.0


class SSELineParser:
    """Incremental Server-Sent Events parser (text/event-stream, one line per ``feed``).

    Mirrors the semantics of Kyora's ``kyora/util/sse.py``: ``event``, ``data`` (joined
    with newlines) and ``id`` fields; comment lines (``:``) are keepalives and ignored;
    an empty line dispatches the message. ``last_id`` is sticky across messages as the
    SSE spec prescribes.
    """

    def __init__(self) -> None:
        self.event: str | None = None
        self.data_parts: list[str] = []
        self.last_id: str | None = None

    def feed(self, raw: str | bytes) -> list[dict]:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if line == "":
            out = []
            if self.data_parts or self.event:
                out.append({"event": self.event, "data": "\n".join(self.data_parts), "id": self.last_id})
            self.event = None
            self.data_parts = []
            return out
        if line.startswith(":"):
            return []
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self.event = value
        elif field == "data":
            self.data_parts.append(value)
        elif field == "id":
            self.last_id = value
        return []

    def flush(self) -> list[dict]:
        return self.feed("")


def kyora_base_url(events_url: str) -> str:
    """Normalise ``events_url`` to the Kyora base URL.

    Accepts ``http://host:8765``, ``http://host:8765/`` or a full
    ``http://host:8765/api/events?types=...`` and returns ``http://host:8765``.
    """
    u = (events_url or "").strip()
    if not u:
        return ""
    parts = urllib.parse.urlsplit(u)
    path = parts.path or ""
    for suffix in ("/api/events.json", "/api/events"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def resolve_kyora_url(url: str, base_url: str) -> str:
    """An absolute URL the Tokkio UI can open, from one Kyora reported.

    A Kyora-relative path (``/preview/...``) is joined to ``base_url`` (Kyora's main listener redirects
    ``/preview`` and ``/runner`` to its preview listener). Kyora names itself ``127.0.0.1`` when it listens on
    every interface, which means nothing on the cluster or the kiosk, so a loopback host is replaced by
    ``base_url``'s host; the scheme, the port (the preview listener has its own) and the path are kept.
    """
    u = (url or "").strip()
    if not u or not base_url:
        return u
    if u.startswith("/") and not u.startswith("//"):
        return base_url.rstrip("/") + u
    try:
        parts = urllib.parse.urlsplit(u)
        base = urllib.parse.urlsplit(base_url)
        host, port, base_host = parts.hostname, parts.port, base.hostname
    except ValueError:
        return u
    if not host or (host.lower() not in _LOOPBACK_HOSTS) or not base_host or base_host.lower() in _LOOPBACK_HOSTS:
        return u
    name = f"[{base_host}]" if ":" in base_host else base_host
    return urllib.parse.urlunsplit((parts.scheme, name + (f":{port}" if port else ""), parts.path, parts.query,
                                    parts.fragment))


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def normalise_stages(stages: Any) -> list[list[str]]:
    """Turn Kyora's ``stages`` payload into table rows ``[stage, status, progress]``.

    Kyora emits a list of ``{stage|name|id, status, pct?, message?}`` dicts; a dict
    ``{name: status}`` and ``[name, status]`` pairs are accepted too. Unknown shapes
    are stringified rather than dropped, so nothing is silently hidden.
    """
    rows: list[list[str]] = []
    if not stages:
        return rows
    if isinstance(stages, dict):
        items: Iterable[Any] = [{"stage": k, **(v if isinstance(v, dict) else {"status": v})} for k, v in stages.items()]
    else:
        items = stages if isinstance(stages, (list, tuple)) else [stages]
    for item in items:
        if isinstance(item, dict):
            name = _s(item.get("stage") or item.get("name") or item.get("id") or "?")
            status = _s(item.get("status") or item.get("state") or "")
            pct = item.get("pct")
            if pct is None:
                pct = item.get("progress")
            progress = ""
            if isinstance(pct, (int, float)):
                progress = f"{round(float(pct) * (100 if 0 <= float(pct) <= 1 else 1))}%"
            elif pct is not None:
                progress = _s(pct)
            if not progress and item.get("message"):
                progress = _s(item.get("message"))
            rows.append([name, status, progress])
        elif isinstance(item, (list, tuple)):
            cells = [_s(c) for c in item][:3]
            rows.append(cells + [""] * (3 - len(cells)))
        else:
            rows.append([_s(item), "", ""])
    return rows


def normalise_buttons(buttons: Any) -> list[tuple[str, str, bool]]:
    """Return ``(button_id, label, active)`` tuples with ids prefixed ``kyora:``.

    Accepts ``[{action, label?, active?}]``, ``[{id, text}]`` or plain strings.
    """
    out: list[tuple[str, str, bool]] = []
    if not buttons:
        return out
    if isinstance(buttons, dict):
        buttons = [{"action": k, "label": v} for k, v in buttons.items()]
    if not isinstance(buttons, (list, tuple)):
        buttons = [buttons]
    for b in buttons:
        if isinstance(b, dict):
            action = _s(b.get("action") or b.get("id") or "")
            label = _s(b.get("label") or b.get("text") or action)
            active = bool(b.get("active", True))
        else:
            action = _s(b)
            label = action
            active = True
        if not action:
            continue
        bid = action if action.startswith(BUTTON_PREFIX) else BUTTON_PREFIX + action
        out.append((bid, label, active))
    return out


def strip_data_url(b64: str) -> str:
    """``data:image/png;base64,AAAA`` -> ``AAAA``; plain base64 is returned unchanged."""
    s = (b64 or "").strip()
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1]
    return s


class KyoraProcessor(FrameProcessor):
    """Pass-through processor that narrates and displays a Kyora run in Tokkio.

    Insert it right after ``rag_multimodal_response_processor`` in ``bot.py`` and
    call :meth:`attach_task` with the ``PipelineTask`` so narration can be queued
    at the pipeline head (see the module docstring for why).
    """

    def __init__(self, events_url: str, token: str, *, narrate: bool = True, show_views: bool = True,
                 reconnect_s: float = 3.0, **kw) -> None:
        super().__init__(**kw)
        self._base_url = kyora_base_url(events_url)
        self._token = token or ""
        self._narrate_enabled = bool(narrate)  # not self._narrate: that name is the coroutine method below
        self._show_views = bool(show_views)
        self._reconnect_s = max(0.5, float(reconnect_s or 3.0))
        self._task: "PipelineTask | None" = None
        self._sse_task: asyncio.Task | None = None
        self._action_tasks: set[asyncio.Task] = set()
        self._last_event_id: str | None = None
        self._run_id: str | None = None
        self._project_id: str | None = None
        self._connected_at: float = 0.0
        self._resume_from: str | None = None
        self._warned_no_task = False
        self._last_view: dict = {}
        self._stats = {"connects": 0, "events": 0, "narrations": 0, "views": 0, "clears": 0, "actions": 0, "errors": 0}

    # ------------------------------------------------------------------ wiring
    def attach_task(self, task: "PipelineTask") -> None:
        """Remember the PipelineTask whose ``queue_frame`` delivers narration to TTS."""
        self._task = task

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def events_url(self) -> str:
        if not self._base_url:
            return ""
        return f"{self._base_url}/api/events?types={','.join(EVENT_TYPES)}"

    @property
    def actions_url(self) -> str:
        return f"{self._base_url}/api/actions" if self._base_url else ""

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "text/event-stream", "User-Agent": "kyora-tokkio-overlay/0.1"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------------ pipecat frame handling
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)  # base class handles StartFrame/CancelFrame bookkeeping

        if isinstance(frame, StartFrame):
            self._start_sse()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_background()
        elif isinstance(frame, UIButtonPressFrame):
            self._on_button(frame)

        await self.push_frame(frame, direction)  # never consume anything: this is a side channel

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._stop_background()

    def _start_sse(self) -> None:
        if self._sse_task is not None:
            return
        if not self._base_url:
            logger.warning("KyoraProcessor: no events_url configured (config KyoraProcessor.events_url or env "
                           "KYORA_EVENTS_URL); the avatar will not narrate Kyora runs")
            return
        logger.info(f"KyoraProcessor: subscribing to {self.events_url}")
        self._sse_task = self.create_task(self._sse_loop(), "kyora_sse")

    async def _stop_background(self) -> None:
        if self._sse_task is not None:
            task, self._sse_task = self._sse_task, None
            try:
                await self.cancel_task(task)
            except Exception as exc:  # noqa: BLE001 - shutting down must never raise into the pipeline
                logger.debug(f"KyoraProcessor: cancel sse task: {exc}")
        for t in list(self._action_tasks):
            try:
                await self.cancel_task(t)
            except Exception:  # noqa: BLE001
                pass
        self._action_tasks.clear()

    def _on_button(self, frame: UIButtonPressFrame) -> None:
        cid = getattr(frame, "component_id", "") or ""
        aid = getattr(frame, "action_id", "") or ""
        button_id = cid if cid.startswith(BUTTON_PREFIX) else (aid if aid.startswith(BUTTON_PREFIX) else "")
        if not button_id:
            return  # not ours (e.g. a RAG citation button)
        action = button_id[len(BUTTON_PREFIX):]
        payload = {"action": action, "run_id": self._run_id, "project_id": self._project_id}
        if not self._base_url:
            logger.warning(f"KyoraProcessor: button {button_id} pressed but no events_url configured")
            return
        logger.info(f"KyoraProcessor: button {button_id} -> POST {self.actions_url} {payload}")
        try:
            t = self.create_task(self._post_action(payload), "kyora_action")
        except Exception as exc:  # noqa: BLE001 - e.g. task manager not initialised
            logger.error(f"KyoraProcessor: cannot schedule action POST: {exc}")
            return
        self._action_tasks.add(t)
        t.add_done_callback(self._action_tasks.discard)

    # ------------------------------------------------------------------ Kyora -> Tokkio
    async def _sse_loop(self) -> None:
        """Reconnecting SSE client. Logs and retries; never raises into the pipeline."""
        backoff = self._reconnect_s
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._connect_and_stream()
                # A clean end of stream (Kyora restarted / closed the connection): reconnect promptly.
                logger.info("KyoraProcessor: event stream ended, reconnecting")
                backoff = self._reconnect_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any network/HTTP/parse failure
                self._stats["errors"] += 1
                msg = f"KyoraProcessor: Kyora unreachable or stream failed ({type(exc).__name__}: {exc}); retry in {backoff:.0f}s"
                if attempt <= 3:
                    logger.warning(msg)
                else:
                    logger.debug(msg)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _prefetch_last_id(self, client) -> None:
        """Learn Kyora's current last event id so the first connection replays nothing.

        ``GET /api/events.json?limit=1`` returns ``{"events": [...], "last_id": N}`` (kyora/app.py).
        Failure is harmless: the ts-based grace filter below still drops stale replays.
        """
        if self._last_event_id is not None:
            return
        url = f"{self._base_url}/api/events.json?types={','.join(EVENT_TYPES)}&limit=1"
        try:
            resp = await client.get(url, headers=self._headers({"Accept": "application/json"}))
            if resp.status_code == 200:
                last = resp.json().get("last_id")
                if isinstance(last, int) and last > 0:
                    self._last_event_id = str(last)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"KyoraProcessor: could not prefetch last event id: {exc}")

    async def _connect_and_stream(self) -> None:
        import httpx  # see module docstring: present wherever bot.py runs (nvidia_pipecat imports it)

        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await self._prefetch_last_id(client)
            self._resume_from = self._last_event_id
            headers = self._headers({"Last-Event-ID": self._last_event_id} if self._last_event_id else None)
            async with client.stream("GET", self.events_url, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread())[:200].decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {resp.status_code} from {self.events_url}: {body}"
                                       + (" (401/403: check hub.token and settings hub.expose_ui=true)"
                                          if resp.status_code in (401, 403) else ""))
                self._stats["connects"] += 1
                self._connected_at = time.time()
                logger.info(f"KyoraProcessor: connected to Kyora events (Last-Event-ID={self._last_event_id})")
                await self._consume_lines(resp.aiter_lines())

    async def _consume_lines(self, lines: AsyncIterator[str]) -> None:
        """Parse an SSE line stream and dispatch each Kyora event. Pure except for frame pushes."""
        parser = SSELineParser()
        async for line in lines:
            for msg in parser.feed(line):
                await self._handle_message(msg)
        for msg in parser.flush():
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict) -> None:
        data = msg.get("data") or ""
        if not data:
            return
        try:
            envelope = json.loads(data)
        except json.JSONDecodeError:
            logger.debug(f"KyoraProcessor: ignoring non-JSON SSE data: {data[:80]!r}")
            return
        if not isinstance(envelope, dict):
            return
        ev_id = msg.get("id") or envelope.get("id")
        if ev_id is not None:
            self._last_event_id = str(ev_id)
        ev_type = envelope.get("type") or msg.get("event") or ""
        ts = envelope.get("ts")
        if self._resume_from is None and self._connected_at and isinstance(ts, (int, float)) \
                and ts < self._connected_at - INITIAL_REPLAY_GRACE_S:
            # Connected without a resume point (prefetch failed): Kyora replays every persisted tokkio.*
            # event since id 0. Those are history, not live progress; skip them but keep the id for resume.
            return
        self._stats["events"] += 1
        await self.handle_event(ev_type, envelope.get("data") or {}, envelope.get("run_id"), envelope.get("project_id"))

    async def handle_event(self, ev_type: str, data: dict, run_id: str | None = None, project_id: str | None = None) -> None:
        """Dispatch one Kyora event (public so tests and other hosts can drive it directly)."""
        if run_id:
            self._run_id = run_id
        if project_id:
            self._project_id = project_id
        try:
            if ev_type == "tokkio.narration":
                await self._narrate(_s(data.get("text") if isinstance(data, dict) else data))
            elif ev_type == "tokkio.view":
                await self._show_view(data if isinstance(data, dict) else {})
            elif ev_type == "tokkio.clear":
                await self._clear_view()
            else:
                logger.debug(f"KyoraProcessor: ignoring event type {ev_type!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad event must not kill the stream or the pipeline
            self._stats["errors"] += 1
            logger.error(f"KyoraProcessor: failed to handle {ev_type}: {exc}")

    async def _narrate(self, text: str) -> None:
        text = (text or "").strip()
        if not text or not self._narrate_enabled:
            return
        if self._task is None:
            if not self._warned_no_task:
                self._warned_no_task = True
                logger.error("KyoraProcessor: attach_task() was not called; narration cannot reach the TTS "
                             "(push_frame from this position would bypass it). Dropping narration.")
            return
        self._stats["narrations"] += 1
        logger.info(f"KyoraProcessor: narrate: {text[:120]!r}")
        # Pipeline head, so the frame passes user-presence gating, TTS, transcript sync, A2F (see module docstring).
        await self._task.queue_frame(TTSSpeakFrame(text))

    def build_view_blocks(self, data: dict) -> list:
        """Build the custom-view blocks for a ``tokkio.view`` payload (no invented content)."""
        blocks: list = []
        title = _s(data.get("title")).strip()
        if title:
            blocks.append(HeaderBlock(id=f"kyora-title-{uuid.uuid4()}", header=title, level=2))
        for label, link in data.get("links") or []:  # URLs Kyora returned for a pressed button (see _post_action)
            blocks.append(TextBlock(id=f"kyora-link-{uuid.uuid4()}", text=f"{label}: {link}"))
        preview_url = resolve_kyora_url(_s(data.get("preview_url")), self._base_url)
        if preview_url:
            blocks.append(TextBlock(id=f"kyora-preview-{uuid.uuid4()}", text=f"Preview: {preview_url}"))
        shot = data.get("screenshot_b64")
        shot_url = data.get("screenshot_url")
        if shot:
            blocks.append(ImageBlock(id=f"kyora-shot-{uuid.uuid4()}", image=Image(data=strip_data_url(_s(shot))),
                                     caption=_s(data.get("screenshot_caption") or title or "screenshot")))
        elif shot_url:
            blocks.append(ImageBlock(id=f"kyora-shot-{uuid.uuid4()}", image=Image(url=_s(shot_url)),
                                     caption=_s(data.get("screenshot_caption") or title or "screenshot")))
        rows = normalise_stages(data.get("stages"))
        if rows:
            blocks.append(TableBlock(id=f"kyora-stages-{uuid.uuid4()}", headers=["Stage", "Status", "Progress"], rows=rows))
        buttons = normalise_buttons(data.get("buttons"))
        if buttons:
            blocks.append(ButtonListBlock(
                id=f"kyora-buttons-{uuid.uuid4()}",
                buttons=[Button(id=bid, active=active, toggled=False, variant=ButtonVariant.CONTAINED, text=label)
                         for bid, label, active in buttons],
            ))
        return blocks

    async def _show_view(self, data: dict) -> None:
        if not self._show_views:
            return
        self._last_view = dict(data)
        blocks = self.build_view_blocks(data)
        if not blocks:
            logger.debug("KyoraProcessor: tokkio.view without displayable content; clearing instead")
            await self._clear_view()
            return
        self._stats["views"] += 1
        # Downstream from here reaches transport.output() whose TokkioUIWebSocketSerializer renders custom views.
        await self.push_frame(StartCustomViewFrame(action_id=VIEW_ACTION_ID, blocks=blocks), FrameDirection.DOWNSTREAM)

    async def _clear_view(self) -> None:
        if not self._show_views:
            return
        self._stats["clears"] += 1
        self._last_view = {}
        await self.push_frame(StopCustomViewFrame(action_id=VIEW_ACTION_ID), FrameDirection.DOWNSTREAM)

    # ------------------------------------------------------------------ Tokkio -> Kyora
    async def _post_action(self, payload: dict) -> None:
        try:
            status, body = await self._http_post(self.actions_url, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._stats["errors"] += 1
            logger.error(f"KyoraProcessor: POST {self.actions_url} failed: {type(exc).__name__}: {exc}")
            return
        if 200 <= status < 300:
            self._stats["actions"] += 1
            logger.info(f"KyoraProcessor: action {payload.get('action')!r} accepted by Kyora (HTTP {status})")
            await self._show_action_link(_s(payload.get("action")), body)
        else:
            self._stats["errors"] += 1
            logger.error(f"KyoraProcessor: action {payload.get('action')!r} rejected by Kyora: HTTP {status} {body[:200]!r}")

    async def _show_action_link(self, action: str, body: str) -> None:
        """Show the URL Kyora answered a button with (``{"url": ...}`` for Open preview / Download ZIP / Run
        tests) in the current custom view, as an absolute link. The Tokkio UI cannot open a tab on the viewer's
        device, so the link is displayed, labelled like the button that was pressed."""
        try:
            url = _s((json.loads(body or "{}") or {}).get("url")).strip()
        except (ValueError, AttributeError):
            url = ""
        if not url or not self._show_views or not self._last_view:
            logger.info(f"KyoraProcessor: {action!r} answered {url!r}; no custom view to show it in")
            return
        link = resolve_kyora_url(url, self._base_url)
        label = next((lbl for bid, lbl, _ in normalise_buttons(self._last_view.get("buttons"))
                      if bid == BUTTON_PREFIX + action), action)
        view = {**self._last_view, "links": [(label, link)]}
        logger.info(f"KyoraProcessor: {label}: {link}")
        self._stats["views"] += 1
        await self.push_frame(StartCustomViewFrame(action_id=VIEW_ACTION_ID, blocks=self.build_view_blocks(view)),
                              FrameDirection.DOWNSTREAM)

    async def _http_post(self, url: str, payload: dict) -> tuple[int, str]:
        import httpx

        headers = self._headers({"Accept": "application/json", "Content-Type": "application/json"})
        async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT_S)) as client:
            resp = await client.post(url, content=json.dumps(payload).encode("utf-8"), headers=headers)
            return resp.status_code, resp.text


__all__ = [
    "KyoraProcessor",
    "SSELineParser",
    "kyora_base_url",
    "resolve_kyora_url",
    "normalise_stages",
    "normalise_buttons",
    "strip_data_url",
    "EVENT_TYPES",
    "VIEW_ACTION_ID",
    "BUTTON_PREFIX",
]
