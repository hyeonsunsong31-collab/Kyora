"""Short identifiers and timestamps."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone


def new_id(prefix: str = "") -> str:
    """Return a short random id such as ``r_3fa9c1``."""
    token = secrets.token_hex(3)
    return f"{prefix}_{token}" if prefix else token


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(text: str, max_len: int = 40) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    s = "".join(out).strip("-")
    return (s[:max_len].rstrip("-")) or "project"
