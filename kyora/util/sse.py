"""Server-Sent Events encoding (server side) and parsing (client side)."""
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator


def encode_event(data: Any, *, event: str | None = None, event_id: int | str | None = None) -> bytes:
    """Encode one SSE message. ``data`` is JSON-encoded unless it is already a str."""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    for part in payload.split("\n"):
        lines.append(f"data: {part}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def keepalive() -> bytes:
    return b": keepalive\n\n"


def parse_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """Parse an SSE byte stream into dicts ``{"event", "data", "id"}``.

    Accepts an iterable of lines (with or without trailing newlines).
    """
    event: str | None = None
    data_parts: list[str] = []
    last_id: str | None = None
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, (bytes, bytearray)) else raw.rstrip("\r\n")
        if line == "":
            if data_parts or event:
                yield {"event": event, "data": "\n".join(data_parts), "id": last_id}
            event = None
            data_parts = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_parts.append(value)
        elif field == "id":
            last_id = value
    if data_parts or event:
        yield {"event": event, "data": "\n".join(data_parts), "id": last_id}
