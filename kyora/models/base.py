"""Shared types for model clients and code engines.

A *code engine* implements the game (Claude Code CLI, the optional Anthropic
Messages-API engine, or a remote Kyora Hub). Engines never fabricate output: if
none is available the pipeline reports BLOCKED.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Metrics:
    """Measured, never estimated unless ``tokens_source`` says so."""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tps: float | None = None
    tokens_source: str = "none"  # "usage" | "estimated_chunks" | "none"

    def to_dict(self) -> dict:
        return {"ttft_ms": self.ttft_ms, "total_ms": self.total_ms, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "tps": self.tps, "tokens_source": self.tokens_source}


@dataclass
class EngineJob:
    run_id: str
    workspace: Path
    prompt: str
    system_prompt: str
    max_turns: int
    model: str
    fallback_models: list[str]
    effort: str
    resume_session_id: str | None = None
    judge_context: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    kind: str = "build"  # "build" | "fix"
    project_id: str | None = None


@dataclass
class EngineEvent:
    type: str  # engine.session|engine.model_resolved|engine.text|engine.thinking|engine.tool_use|engine.tool_result|engine.result|engine.error|engine.stderr|engine.compat_retry|stage.progress
    data: dict = field(default_factory=dict)


class EngineSink(Protocol):
    def on_event(self, ev: EngineEvent) -> None: ...


class BusSink:
    """EngineSink that forwards engine events to the EventBus for a run."""

    def __init__(self, bus, run_id: str, project_id: str | None = None, extra: dict | None = None):
        self.bus = bus
        self.run_id = run_id
        self.project_id = project_id
        self.extra = extra or {}

    def on_event(self, ev: EngineEvent) -> None:
        data = dict(ev.data)
        data.update(self.extra)
        self.bus.publish(ev.type, data, run_id=self.run_id, project_id=self.project_id)


@dataclass
class EngineResult:
    ok: bool
    backend: str
    requested_model: str
    active_model: str | None = None
    fallback_used: bool = False
    session_id: str | None = None
    num_turns: int | None = None
    duration_ms: int = 0
    cost_usd: float | None = None
    result_text: str = ""
    error: str | None = None
    permission_denials: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "backend": self.backend, "requested_model": self.requested_model,
                "active_model": self.active_model, "fallback_used": self.fallback_used, "session_id": self.session_id,
                "num_turns": self.num_turns, "duration_ms": self.duration_ms, "cost_usd": self.cost_usd,
                "result_text": self.result_text[:4000], "error": self.error,
                "permission_denials": self.permission_denials}


@dataclass
class Availability:
    available: bool
    backend: str
    reason: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"available": self.available, "backend": self.backend, "reason": self.reason, "details": self.details}


class CodeEngine(ABC):
    name: str = "abstract"

    @abstractmethod
    def availability(self) -> Availability: ...

    @abstractmethod
    def run(self, job: EngineJob, sink: EngineSink, cancel: threading.Event) -> EngineResult: ...


def select_engine(app) -> CodeEngine | None:
    """Pick the first available engine: hub (if configured) > claude_code > anthropic_api.

    ``app`` is the composition root (kyora.app.App). Returns None when nothing
    can run code; callers must surface BLOCKED, never fake output.
    """
    order = []
    if getattr(app, "hub_client", None) is not None and app.config.hub.url:
        order.append(app.hub_client)
    if getattr(app, "claude_engine", None) is not None:
        order.append(app.claude_engine)
    if getattr(app, "anthropic_engine", None) is not None:
        order.append(app.anthropic_engine)
    for eng in order:
        try:
            if eng.availability().available:
                return eng
        except Exception:
            continue
    return None
