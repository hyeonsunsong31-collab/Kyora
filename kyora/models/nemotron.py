"""NVIDIA Nemotron-3-Ultra wrapper: Kyora's text and design director (role ``nemotron``).

Nemotron writes the GDD prose, narrative and dialogue (:meth:`Nemotron.design_doc`),
the UI specification with design tokens and copy (:meth:`Nemotron.ui_spec`) and
the one-sentence avatar narration (:meth:`Nemotron.narration`). Structured
answers are requested through OpenAI function tools (``emit_design``,
``emit_ui_spec``); when the model answers in text instead, the JSON object is
extracted from the text. Nothing is invented when the model fails: the
wrapper raises (or, for narration, returns ``''`` and publishes
``model.error``).

Thinking mode is switched with ``chat_template_kwargs.enable_thinking``:
``True`` samples at temperature 1.0 / top_p 0.95, ``False`` at temperature 0.2.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Mapping, Sequence

from .nim_client import (
    ChatResult, ModelChain, NIMClient, NIMError, call_with_model_fallback, chat_with_events, extract_structured,
    force_tool, function_tool, is_tool_rejection, json_for_prompt, render_prompt, strip_think,
)

log = logging.getLogger("kyora.models.nemotron")

ROLE = "nemotron"
NARRATION_MAX_CHARS = 120

DESIGN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "gdd_markdown": {"type": "string", "description": "Game design document in Markdown (600-1200 words)."},
        "narrative": {"type": "string", "description": "Setting and premise, 80-200 words."},
        "dialogue": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"speaker": {"type": "string"}, "line": {"type": "string"}},
                      "required": ["speaker", "line"]},
            "description": "4-12 short in-game lines.",
        },
        "presenter_intro": {"type": "string", "description": "One plain sentence under 120 characters."},
    },
    "required": ["gdd_markdown", "narrative", "dialogue", "presenter_intro"],
}

UI_SPEC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tokens": {
            "type": "object",
            "properties": {
                "color": {"type": "object", "additionalProperties": {"type": "string"}},
                "type": {"type": "object"},
                "spacing": {"type": "object", "additionalProperties": {"type": "string"}},
                "radius": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["color", "type", "spacing", "radius"],
        },
        "screens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "components": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {"type": {"type": "string"}, "label": {"type": "string"},
                                                 "notes": {"type": "string"}},
                                  "required": ["type", "label"]},
                    },
                },
                "required": ["id", "name", "purpose", "components"],
            },
        },
        "copy": {"type": "object", "additionalProperties": {"type": "string"},
                 "description": "All user-facing strings keyed like screen.element."},
    },
    "required": ["tokens", "screens", "copy"],
}

DESIGN_TOOL = function_tool("emit_design", "Emit the complete game design document, narrative, dialogue and presenter intro.",
                            DESIGN_SCHEMA)
UI_SPEC_TOOL = function_tool("emit_ui_spec", "Emit the complete UI specification: design tokens, screens with components, copy.",
                             UI_SPEC_SCHEMA)

_MD_CHARS_RE = re.compile(r"[*_`#>\[\]|~]+")
_WS_RE = re.compile(r"\s+")


def _str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_str(v) for v in value)
    return str(value)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def clean_narration(text: str, max_chars: int = NARRATION_MAX_CHARS) -> str:
    """Plain, single-line, markdown-free, at most ``max_chars`` characters (cut at a word boundary)."""
    text = strip_think(_str(text))
    text = _MD_CHARS_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip().strip('"').strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-")


def normalize_design(obj: Mapping) -> dict:
    """Validate and normalize an ``emit_design`` payload. Raises ``ValueError`` when the GDD is missing."""
    gdd = _str(obj.get("gdd_markdown") or obj.get("gdd") or obj.get("design_document")).strip()
    if not gdd:
        raise ValueError("Nemotron design payload has no 'gdd_markdown'")
    dialogue: list[dict] = []
    for item in obj.get("dialogue") or []:
        if isinstance(item, Mapping):
            speaker = _str(item.get("speaker") or item.get("who") or "Narrator").strip()
            line = _str(item.get("line") or item.get("text")).strip()
        else:
            speaker, line = "Narrator", _str(item).strip()
        if line:
            dialogue.append({"speaker": speaker or "Narrator", "line": line})
    return {
        "gdd_markdown": gdd,
        "narrative": _str(obj.get("narrative")).strip(),
        "dialogue": dialogue,
        "presenter_intro": clean_narration(_str(obj.get("presenter_intro"))),
    }


def normalize_ui_spec(obj: Mapping) -> dict:
    """Validate and normalize an ``emit_ui_spec`` payload. Raises ``ValueError`` on a missing core."""
    tokens_in = obj.get("tokens")
    if not isinstance(tokens_in, Mapping):
        raise ValueError("Nemotron ui_spec payload has no 'tokens' object")
    tokens = {}
    for group in ("color", "type", "spacing", "radius"):
        value = tokens_in.get(group)
        if group == "color" and value is None:
            value = tokens_in.get("colors")
        tokens[group] = dict(value) if isinstance(value, Mapping) else {}
    if not tokens["color"]:
        raise ValueError("Nemotron ui_spec payload has no 'tokens.color' entries")
    screens_in = obj.get("screens")
    if not isinstance(screens_in, list) or not screens_in:
        raise ValueError("Nemotron ui_spec payload has no 'screens'")
    screens: list[dict] = []
    for i, scr in enumerate(screens_in):
        if not isinstance(scr, Mapping):
            continue
        comps: list[dict] = []
        for comp in scr.get("components") or []:
            if isinstance(comp, Mapping):
                comps.append({"type": _str(comp.get("type") or "component").strip(),
                              "label": _str(comp.get("label")).strip(),
                              "notes": _str(comp.get("notes") or comp.get("states")).strip()})
            elif comp:
                comps.append({"type": "component", "label": _str(comp).strip(), "notes": ""})
        sid = _str(scr.get("id") or scr.get("name") or f"screen-{i + 1}").strip().lower().replace(" ", "-")
        screens.append({"id": sid, "name": _str(scr.get("name") or sid).strip(),
                        "purpose": _str(scr.get("purpose")).strip(), "components": comps})
    if not screens:
        raise ValueError("Nemotron ui_spec payload has no usable screens")
    copy_in = obj.get("copy")
    copy = {str(k): _str(v) for k, v in copy_in.items()} if isinstance(copy_in, Mapping) else {}
    return {"tokens": tokens, "screens": screens, "copy": copy}


def attachments_text(attachments: list[dict] | None, *, excerpt_chars: int = 1500, max_items: int = 12) -> str:
    """Describe attachments for a text-only model (names, kinds, text excerpts, ZIP listings)."""
    if not attachments:
        return "(no attachments)"
    lines: list[str] = []
    for att in list(attachments)[:max_items]:
        if not isinstance(att, Mapping):
            continue
        name = _str(att.get("name") or att.get("id") or "attachment")
        mime = _str(att.get("mime") or att.get("content_type") or "")
        role = _str(att.get("role") or "")
        head = f"- {name}" + (f" ({mime})" if mime else "") + (f" [{role}]" if role else "")
        if att.get("image_data_url") or mime.startswith("image/"):
            head += ": image (not visible to this text model; the planner's intake describes it)"
        lines.append(head)
        excerpt = _str(att.get("text_excerpt") or att.get("excerpt"))
        if excerpt:
            lines.append("  excerpt:\n" + "\n".join("    " + ln for ln in excerpt[:excerpt_chars].splitlines()))
        listing = att.get("listing")
        if listing:
            items = listing if isinstance(listing, (list, tuple)) else _str(listing).splitlines()
            shown = [str(x) for x in list(items)[:60]]
            more = len(items) - len(shown)
            lines.append("  zip listing: " + ", ".join(shown) + (f" … (+{more} more)" if more > 0 else ""))
    if len(attachments) > max_items:
        lines.append(f"- … {len(attachments) - max_items} more attachments not shown")
    return "\n".join(lines) or "(no attachments)"


class Nemotron:
    """Text/design director. Constructed by ``kyora.app`` as ``Nemotron(client, model, bus, nemotron_cfg)``."""

    ROLE = ROLE
    MAX_TOKENS: dict[str, int] = {"design": 16384, "ui_spec": 12288, "narration": 96}
    MAX_RETRY_TOKENS = 32768

    def __init__(self, client: NIMClient, model: str, bus, nemotron_cfg, fallback_models: Sequence[str] = ()):
        if client is None:
            raise ValueError("Nemotron needs a NIMClient")
        if not model:
            raise ValueError("Nemotron needs a model id")
        self.client = client
        self.chain = ModelChain(model, fallback_models)
        self.bus = bus
        self.cfg = nemotron_cfg
        self._lock = threading.Lock()
        self._last_metrics: dict[str, dict] = {}

    # ------------------------------------------------------------------ model
    @property
    def model(self) -> str:
        """The model the next call starts with: the requested one, or the fallback that replaced it."""
        return self.chain.active

    def configure(self, model: str, fallback_models: Sequence[str] = ()) -> None:
        """Settings changed: a new requested model / fallback list (forgets skipped models)."""
        self.chain = ModelChain(model, fallback_models)

    # ------------------------------------------------------------------ bookkeeping
    def _remember(self, purpose: str, res: ChatResult) -> None:
        with self._lock:
            self._last_metrics[purpose] = res.metrics.to_dict()

    def last_metrics(self, purpose: str) -> dict | None:
        """Measured metrics of the most recent call for ``purpose`` (``design``, ``ui_spec``, ``narration`` ...)."""
        with self._lock:
            m = self._last_metrics.get(purpose)
            return dict(m) if m else None

    @property
    def thinking_for_design(self) -> bool:
        return bool(getattr(self.cfg, "thinking_for_design", True))

    # ------------------------------------------------------------------ core call
    def generate(self, *, role: str = ROLE, system: str, messages: list[dict], thinking: bool,
                 tools: list[dict] | None = None, tool_choice: Any = None, run_id: str | None = None,
                 project_id: str | None = None, cancel: threading.Event | None = None,
                 max_tokens: int | None = None, purpose: str | None = None, publish_deltas: bool = True,
                 timeout_s: float | None = None) -> ChatResult:
        """One Nemotron chat call with ``model.*`` events on the bus.

        ``thinking=True`` -> ``temperature 1.0, top_p 0.95``; ``False`` -> ``temperature 0.2``;
        the body always carries ``chat_template_kwargs.enable_thinking``.
        """
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        sampling: dict[str, float] = {"temperature": 1.0, "top_p": 0.95} if thinking else {"temperature": 0.2}
        extra = {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}
        def call(model: str) -> ChatResult:
            fields = {} if model == self.chain.requested else {"requested_model": self.chain.requested, "fallback": True}
            return chat_with_events(self.client, self.bus, role=role, model=model, run_id=run_id,
                                    project_id=project_id, purpose=purpose, publish_deltas=publish_deltas,
                                    event_fields=fields, messages=msgs, tools=tools, tool_choice=tool_choice,
                                    max_tokens=max_tokens, extra=extra, cancel=cancel, timeout_s=timeout_s, **sampling)
        res = call_with_model_fallback(self.chain, self.bus, call, role=role, run_id=run_id, project_id=project_id,
                                       purpose=purpose)
        self._remember(purpose or role, res)
        return res

    def _structured(self, tool: dict, *, system: str, user: str, thinking: bool, purpose: str, max_tokens: int,
                    run_id: str | None, project_id: str | None, cancel: threading.Event | None,
                    normalize: Callable[[Mapping], dict] | None = None) -> tuple[dict, ChatResult]:
        """Ask for a tool call; fall back to JSON-in-text, and to a tool-less request when tools are rejected.

        An answer cut off at the token limit or failing ``normalize`` is asked for once more, without
        thinking and with twice the budget: on 2026-10-18 a live UI spec spent all 12288 tokens in
        thinking mode and arrived truncated (no ``tokens`` object).
        """
        name = tool["function"]["name"]
        error = ""
        for attempt in (1, 2):
            think = thinking if attempt == 1 else False
            budget = max_tokens if attempt == 1 else min(max_tokens * 2, self.MAX_RETRY_TOKENS)
            prompt = user if attempt == 1 else (
                f"{user}\n\nYour previous answer was not usable ({error}). Call {name} once with the complete JSON "
                "object; keep every value short so the whole answer fits.")
            messages = [{"role": "user", "content": prompt}]
            try:
                res = self.generate(system=system, messages=messages, thinking=think, tools=[tool],
                                    tool_choice=force_tool(name), run_id=run_id, project_id=project_id, cancel=cancel,
                                    max_tokens=budget, purpose=purpose)
            except NIMError as exc:
                if not is_tool_rejection(exc):
                    raise
                log.warning("nemotron %s: gateway rejected tools (%s); retrying without tools", purpose, exc)
                res = self.generate(system=system, messages=messages, thinking=think, run_id=run_id,
                                    project_id=project_id, cancel=cancel, max_tokens=budget, purpose=purpose)
            obj = extract_structured(res, name)
            truncated = res.finish_reason == "length"
            if obj is None:
                error = (f"no parsable {name} payload (finish_reason={res.finish_reason!r}, "
                         f"content_chars={len(res.content)})")
            else:
                try:
                    return (normalize(obj) if normalize else obj), res
                except ValueError as exc:
                    error = f"{exc}" + (" (cut off at the token limit)" if truncated else "")
            if attempt == 1:
                log.warning("nemotron %s: %s; asking once more without thinking and a %d-token budget", purpose, error,
                            min(max_tokens * 2, self.MAX_RETRY_TOKENS))
        raise ValueError(f"Nemotron returned no usable {name} payload after one retry: {error}")

    # ------------------------------------------------------------------ design document
    def design_doc(self, intake: dict, plan: dict, attachments: list[dict] | None = None, run_id: str | None = None,
                   project_id: str | None = None, cancel: threading.Event | None = None) -> dict:
        """GDD + narrative + dialogue + presenter intro -> ``design.json`` content.

        Returns ``{gdd_markdown, narrative, dialogue:[{speaker,line}], presenter_intro}``.
        Raises ``NIMError`` on API failure and ``ValueError`` when the answer has no usable design.
        """
        system = render_prompt("nemotron_design.md",
                               intake_json=json_for_prompt(intake or {}),
                               plan_json=json_for_prompt(plan or {}),
                               design_brief=_str((plan or {}).get("design_brief_for_nemotron")).strip() or "(none given)",
                               attachments_text=attachments_text(attachments))
        user = "Write the design for this plan now and call emit_design with the complete result."
        obj, _ = self._structured(DESIGN_TOOL, system=system, user=user, thinking=self.thinking_for_design, purpose="design",
                                  max_tokens=self.MAX_TOKENS["design"], run_id=run_id, project_id=project_id, cancel=cancel,
                                  normalize=normalize_design)
        return obj

    # ------------------------------------------------------------------ UI spec
    def ui_spec(self, intake: dict, plan: dict, run_id: str | None = None, project_id: str | None = None,
                cancel: threading.Event | None = None) -> dict:
        """UI specification -> ``ui_spec.json`` content ``{tokens:{color,type,spacing,radius}, screens:[...], copy:{}}``."""
        system = render_prompt("nemotron_ui_spec.md",
                               intake_json=json_for_prompt(intake or {}),
                               plan_json=json_for_prompt(plan or {}),
                               design_brief=_str((plan or {}).get("design_brief_for_nemotron")).strip() or "(none given)")
        user = "Produce the UI specification for this plan now and call emit_ui_spec with the complete result."
        obj, _ = self._structured(UI_SPEC_TOOL, system=system, user=user, thinking=self.thinking_for_design, purpose="ui_spec",
                                  max_tokens=self.MAX_TOKENS["ui_spec"], run_id=run_id, project_id=project_id, cancel=cancel,
                                  normalize=normalize_ui_spec)
        return obj

    # ------------------------------------------------------------------ narration
    def narration(self, text: str, run_id: str | None = None, project_id: str | None = None) -> str:
        """One spoken sentence (<= 120 chars, no markdown) for the avatar presenter. Never raises.

        Returns ``''`` when the model call fails; the failure is published as ``model.error``.
        """
        text = _str(text).strip()
        if not text:
            return ""
        system = render_prompt("nemotron_narration.md", max_chars=NARRATION_MAX_CHARS)
        try:
            res = self.generate(system=system, messages=[{"role": "user", "content": text[:2000]}], thinking=False,
                                run_id=run_id, project_id=project_id, max_tokens=self.MAX_TOKENS["narration"],
                                purpose="narration", publish_deltas=False, timeout_s=60.0)
        except NIMError as exc:
            log.warning("nemotron narration failed: %s", exc)  # model.error already published by chat_with_events
            return ""
        except Exception as exc:  # noqa: BLE001 - narration is best effort but never silent
            log.exception("nemotron narration: unexpected error")
            if self.bus is not None:
                try:
                    from ..pipeline.events import MODEL_ERROR
                    self.bus.publish(MODEL_ERROR, {"role": self.ROLE, "model": self.model, "purpose": "narration",
                                                   "error": f"{type(exc).__name__}: {exc}", "status": None},
                                     run_id=run_id, project_id=project_id)
                except Exception as pub_exc:  # noqa: BLE001
                    log.warning("could not publish model.error: %s", pub_exc)
            return ""
        return clean_narration(res.content)


__all__ = ["Nemotron", "ROLE", "DESIGN_SCHEMA", "UI_SPEC_SCHEMA", "DESIGN_TOOL", "UI_SPEC_TOOL",
           "normalize_design", "normalize_ui_spec", "clean_narration", "attachments_text", "NARRATION_MAX_CHARS"]
