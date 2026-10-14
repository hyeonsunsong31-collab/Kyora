"""Configuration: ``keys.json`` (secrets, endpoints) and ``settings.json`` (behaviour).

Search order for the config directory (first hit wins):
1. explicit ``--config DIR`` / ``Config.load(explicit_dir)``
2. ``$KYORA_CONFIG_DIR``
3. ``./config`` (current working directory)
4. ``<dir of sys.argv[0]>/config`` (next to ``kyora.pyz`` in the release ZIP)
5. ``$KYORA_HOME/config`` - seeded from the packaged defaults (``kyora/defaults/``)
   or, when running from a source checkout, from ``<repo>/config``.

The user chose to ship real NVIDIA keys inside ``keys.json``; ``to_public``
masks them for the UI unless ``reveal=True``.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util.fs import atomic_write_json, kyora_home, read_json

DEFAULT_KEYS: dict = {
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "nemotron": {"model": "nvidia/nemotron-3-ultra-550b-a55b", "api_key": ""},
        "kimi": {"model": "moonshotai/kimi-k3", "api_key": ""},
    },
    "anthropic": {"api_key": ""},
    "hub": {"url": "", "token": ""},
    "tokkio": {"ui_url": "", "configurator_url": ""},
}

DEFAULT_SETTINGS: dict = {
    "server": {"host": "127.0.0.1", "port": 8765, "workers": 2},
    "claude_code": {
        "command": ["claude"],
        "model": "claude-fable-5-1",
        "fallback_models": ["claude-opus-5-5", "claude-sonnet-5-5"],
        "effort": "max",
        "max_turns": 60,
        "timeout_s": 3600,
        "idle_timeout_s": 900,
        "resume_for_fixes": True,
        "judge_tools": ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"],
        "extra_args": [],
    },
    "kimi": {"reasoning_effort": "max", "judge_reasoning_effort": "max", "effort_field": "top_level"},
    "nemotron": {"thinking_for_design": True},
    "quality": {
        "min_pass_rate": 0.94,
        "max_fix_iterations": 3,
        "browser_wait_s": 1800,
        "require_plan_approval": False,
        "auto_run_browser_tests": True,
    },
    "hub": {"expose_ui": False},
}

# Settings paths the UI may change (dotted). Everything else is read-only from the UI.
PATCHABLE_KEYS = {
    "nvidia.base_url", "nvidia.nemotron.model", "nvidia.nemotron.api_key",
    "nvidia.kimi.model", "nvidia.kimi.api_key", "anthropic.api_key",
    "hub.url", "hub.token", "tokkio.ui_url", "tokkio.configurator_url",
}
PATCHABLE_SETTINGS = {
    "server.port", "server.host",
    "claude_code.command", "claude_code.model", "claude_code.fallback_models", "claude_code.effort",
    "claude_code.max_turns", "claude_code.timeout_s", "claude_code.idle_timeout_s", "claude_code.resume_for_fixes",
    "claude_code.extra_args",
    "kimi.reasoning_effort", "kimi.judge_reasoning_effort", "kimi.effort_field",
    "nemotron.thinking_for_design",
    "quality.min_pass_rate", "quality.max_fix_iterations", "quality.browser_wait_s",
    "quality.require_plan_approval", "quality.auto_run_browser_tests",
    "hub.expose_ui",
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
KIMI_EFFORTS = ("low", "high", "max")


@dataclass
class NvidiaModelCfg:
    model: str
    api_key: str


@dataclass
class NvidiaCfg:
    base_url: str
    nemotron: NvidiaModelCfg
    kimi: NvidiaModelCfg


@dataclass
class ClaudeCodeCfg:
    command: list[str]
    model: str
    fallback_models: list[str]
    effort: str
    max_turns: int
    timeout_s: int
    idle_timeout_s: int
    resume_for_fixes: bool
    judge_tools: list[str]
    extra_args: list[str] = field(default_factory=list)


@dataclass
class KimiCfg:
    reasoning_effort: str
    judge_reasoning_effort: str
    effort_field: str  # "top_level" | "extra_body"


@dataclass
class NemotronCfg:
    thinking_for_design: bool


@dataclass
class QualityCfg:
    min_pass_rate: float
    max_fix_iterations: int
    browser_wait_s: int
    require_plan_approval: bool
    auto_run_browser_tests: bool


@dataclass
class HubCfg:
    url: str
    token: str
    expose_ui: bool


@dataclass
class TokkioCfg:
    ui_url: str
    configurator_url: str


@dataclass
class ServerCfg:
    host: str
    port: int
    workers: int


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _get(d: dict, dotted: str, default=None):
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:6] + "…" + secret[-4:]


def _repo_config_dir() -> Path | None:
    """``<repo>/config`` when running from a source checkout."""
    here = Path(__file__).resolve().parent.parent / "config"
    return here if (here / "keys.json").exists() else None


def _packaged_defaults_dir() -> Path | None:
    d = Path(__file__).resolve().parent / "defaults"
    return d if (d / "keys.json").exists() else None


class Config:
    """Loaded configuration with typed views and save/patch support."""

    def __init__(self, config_dir: Path, keys: dict, settings: dict):
        self.config_dir = Path(config_dir)
        self.keys_path = self.config_dir / "keys.json"
        self.settings_path = self.config_dir / "settings.json"
        self.raw_keys = _deep_merge(DEFAULT_KEYS, keys)
        self.raw_settings = _deep_merge(DEFAULT_SETTINGS, settings)

    # ------------------------------------------------------------------ loading
    @staticmethod
    def resolve_dir(explicit_dir: Path | str | None = None) -> Path:
        candidates: list[Path] = []
        if explicit_dir:
            candidates.append(Path(explicit_dir).expanduser())
        if os.environ.get("KYORA_CONFIG_DIR"):
            candidates.append(Path(os.environ["KYORA_CONFIG_DIR"]).expanduser())
        candidates.append(Path.cwd() / "config")
        if sys.argv and sys.argv[0]:
            candidates.append(Path(sys.argv[0]).resolve().parent / "config")
        for c in candidates:
            if (c / "keys.json").exists():
                return c
        # seed KYORA_HOME/config
        home_cfg = kyora_home() / "config"
        if not (home_cfg / "keys.json").exists():
            src = _packaged_defaults_dir() or _repo_config_dir()
            home_cfg.mkdir(parents=True, exist_ok=True)
            if src:
                for name in ("keys.json", "settings.json"):
                    data = read_json(src / name, default=None)
                    if data is not None:
                        atomic_write_json(home_cfg / name, data)
            if not (home_cfg / "keys.json").exists():
                atomic_write_json(home_cfg / "keys.json", DEFAULT_KEYS)
            if not (home_cfg / "settings.json").exists():
                atomic_write_json(home_cfg / "settings.json", DEFAULT_SETTINGS)
        return home_cfg

    @classmethod
    def load(cls, explicit_dir: Path | str | None = None) -> "Config":
        d = cls.resolve_dir(explicit_dir)
        keys = read_json(d / "keys.json", default={}) or {}
        settings = read_json(d / "settings.json", default={}) or {}
        # Environment overrides for secrets (never written back unless saved from UI)
        env_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        if env_anthropic and not _get(keys, "anthropic.api_key"):
            _set(keys, "anthropic.api_key", env_anthropic)
        env_nvidia = os.environ.get("NVIDIA_API_KEY")
        if env_nvidia:
            if not _get(keys, "nvidia.nemotron.api_key"):
                _set(keys, "nvidia.nemotron.api_key", env_nvidia)
            if not _get(keys, "nvidia.kimi.api_key"):
                _set(keys, "nvidia.kimi.api_key", env_nvidia)
        return cls(d, keys, settings)

    def save(self) -> None:
        atomic_write_json(self.keys_path, self.raw_keys)
        atomic_write_json(self.settings_path, self.raw_settings)

    # ------------------------------------------------------------------ typed views
    @property
    def nvidia(self) -> NvidiaCfg:
        k = self.raw_keys["nvidia"]
        return NvidiaCfg(
            base_url=str(k.get("base_url") or DEFAULT_KEYS["nvidia"]["base_url"]).rstrip("/"),
            nemotron=NvidiaModelCfg(model=k["nemotron"]["model"], api_key=k["nemotron"].get("api_key", "")),
            kimi=NvidiaModelCfg(model=k["kimi"]["model"], api_key=k["kimi"].get("api_key", "")),
        )

    @property
    def anthropic_api_key(self) -> str:
        return self.raw_keys.get("anthropic", {}).get("api_key", "") or ""

    @property
    def claude_code(self) -> ClaudeCodeCfg:
        s = self.raw_settings["claude_code"]
        return ClaudeCodeCfg(
            command=list(s.get("command") or ["claude"]),
            model=s.get("model", "claude-fable-5-1"),
            fallback_models=list(s.get("fallback_models") or []),
            effort=s.get("effort", "max"),
            max_turns=int(s.get("max_turns", 60)),
            timeout_s=int(s.get("timeout_s", 3600)),
            idle_timeout_s=int(s.get("idle_timeout_s", 900)),
            resume_for_fixes=bool(s.get("resume_for_fixes", True)),
            judge_tools=list(s.get("judge_tools") or DEFAULT_SETTINGS["claude_code"]["judge_tools"]),
            extra_args=list(s.get("extra_args") or []),
        )

    @property
    def kimi(self) -> KimiCfg:
        s = self.raw_settings["kimi"]
        return KimiCfg(
            reasoning_effort=s.get("reasoning_effort", "max"),
            judge_reasoning_effort=s.get("judge_reasoning_effort", "max"),
            effort_field=s.get("effort_field", "top_level"),
        )

    @property
    def nemotron(self) -> NemotronCfg:
        return NemotronCfg(thinking_for_design=bool(self.raw_settings["nemotron"].get("thinking_for_design", True)))

    @property
    def quality(self) -> QualityCfg:
        q = self.raw_settings["quality"]
        return QualityCfg(
            min_pass_rate=float(q.get("min_pass_rate", 0.94)),
            max_fix_iterations=int(q.get("max_fix_iterations", 3)),
            browser_wait_s=int(q.get("browser_wait_s", 1800)),
            require_plan_approval=bool(q.get("require_plan_approval", False)),
            auto_run_browser_tests=bool(q.get("auto_run_browser_tests", True)),
        )

    @property
    def hub(self) -> HubCfg:
        return HubCfg(
            url=(self.raw_keys.get("hub", {}).get("url") or "").rstrip("/"),
            token=self.raw_keys.get("hub", {}).get("token") or "",
            expose_ui=bool(self.raw_settings.get("hub", {}).get("expose_ui", False)),
        )

    @property
    def tokkio(self) -> TokkioCfg:
        t = self.raw_keys.get("tokkio", {})
        return TokkioCfg(ui_url=(t.get("ui_url") or "").rstrip("/"), configurator_url=(t.get("configurator_url") or "").rstrip("/"))

    @property
    def server(self) -> ServerCfg:
        s = self.raw_settings["server"]
        return ServerCfg(host=s.get("host", "127.0.0.1"), port=int(s.get("port", 8765)), workers=int(s.get("workers", 2)))

    # ------------------------------------------------------------------ UI support
    def to_public(self, reveal: bool = False) -> dict:
        keys = copy.deepcopy(self.raw_keys)
        if not reveal:
            for path in ("nvidia.nemotron.api_key", "nvidia.kimi.api_key", "anthropic.api_key", "hub.token"):
                _set(keys, path, _mask(_get(keys, path, "") or ""))
        return {
            "config_dir": str(self.config_dir),
            "keys": keys,
            "settings": copy.deepcopy(self.raw_settings),
            "patchable": sorted(PATCHABLE_KEYS | PATCHABLE_SETTINGS),
        }

    def apply_patch(self, patch: dict) -> list[str]:
        """Apply ``{"keys": {...dotted or nested...}, "settings": {...}}``; returns changed paths.

        Masked values (containing the mask ellipsis) are ignored so a round-trip of
        ``to_public`` output never overwrites a real secret with its mask.
        """
        changed: list[str] = []
        for section, allowed, target in (("keys", PATCHABLE_KEYS, self.raw_keys), ("settings", PATCHABLE_SETTINGS, self.raw_settings)):
            items = _flatten(patch.get(section) or {})
            for dotted, value in items.items():
                if dotted not in allowed:
                    raise ValueError(f"not patchable: {section}.{dotted}")
                if isinstance(value, str) and "…" in value:
                    continue
                _validate(section, dotted, value)
                if _get(target, dotted) != value:
                    _set(target, dotted, value)
                    changed.append(f"{section}.{dotted}")
        return changed


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and not key.endswith("command") and not key.endswith("extra_args"):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _validate(section: str, dotted: str, value) -> None:
    if dotted == "claude_code.effort" and value not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {EFFORT_LEVELS}")
    if dotted in ("kimi.reasoning_effort", "kimi.judge_reasoning_effort") and value not in KIMI_EFFORTS:
        raise ValueError(f"kimi effort must be one of {KIMI_EFFORTS}")
    if dotted == "kimi.effort_field" and value not in ("top_level", "extra_body"):
        raise ValueError("effort_field must be top_level or extra_body")
    if dotted == "quality.min_pass_rate":
        if not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
            raise ValueError("min_pass_rate must be between 0 and 1")
    if dotted in ("server.port",) and (not isinstance(value, int) or not (1 <= value <= 65535)):
        raise ValueError("port must be 1-65535")
    if dotted in ("claude_code.command", "claude_code.fallback_models", "claude_code.extra_args"):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{dotted} must be a list of strings")
    if dotted in ("claude_code.max_turns", "claude_code.timeout_s", "claude_code.idle_timeout_s",
                  "quality.max_fix_iterations", "quality.browser_wait_s"):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{dotted} must be a non-negative integer")
