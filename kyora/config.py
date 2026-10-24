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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util.fs import atomic_write_json, kyora_home, read_json

DEFAULT_KEYS: dict = {
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        # fallback_models: tried in order when the requested model is gone (404/410) or overloaded (503 after
        # retries); every substitution is shown. Measured on 2026-10-18: Nemotron-3-Ultra answers but is often
        # "temporarily overloaded" on the free tier; moonshotai/kimi-k3 left the free endpoint on 2026-10-15. On
        # 2026-10-24 nemotron-3-super answered 404 ("not found for account") for the committed key while
        # nemotron-3.5-super-vl answered with tool calls: the served one comes first, the other stays for accounts
        # that still have it (a 404 is skipped for good after one request).
        "nemotron": {"model": "nvidia/nemotron-3-ultra-550b-a55b", "api_key": "", "base_url": "",
                     "fallback_models": ["nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3-super-120b-a12b"]},
        "kimi": {"model": "moonshotai/kimi-k3", "api_key": "", "base_url": "",
                 "fallback_models": ["nvidia/nemotron-3-ultra-550b-a55b", "z-ai/glm-5.3",
                                     "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3-super-120b-a12b"]},
        # a role whose own key is refused (401/403) may use the other role's key on the same endpoint
        "share_keys": True,
    },
    "anthropic": {"api_key": ""},
    # The Kyora agent engine (no Claude needed): any OpenAI-compatible endpoint. Empty = the NVIDIA
    # endpoint above with the NVIDIA keys (each tried in turn, see nvidia.share_keys).
    "agent": {"base_url": "", "api_key": ""},
    "hub": {"url": "", "token": ""},
    "tokkio": {"ui_url": "", "configurator_url": ""},
}

DEFAULT_SETTINGS: dict = {
    # preview_port: the second listener (separate origin) for game previews and browser tests;
    # null = port + 1. allowed_hosts: extra Host names accepted besides loopback/LAN (reverse proxies).
    "server": {"host": "127.0.0.1", "port": 8765, "preview_port": None, "allowed_hosts": [], "workers": 2},
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
    # The Kyora agent engine: Kyora's own tool loop (bash + file editor, every write judged) on an
    # OpenAI-compatible model - BUILD/FIX keep working without a Claude subscription or API key.
    "agent_engine": {
        "enabled": True,
        # measured live 2026-10-18 (CI, NVIDIA free tier): glm-5.3 without thinking built a game that passed the
        # gate 28/28 on its first iteration; with thinking it had not finished BUILD after 85 minutes. Fallbacks:
        # nemotron-3-ultra did most turns of the live runs of 2026-10-24 that passed the gate; nemotron-3.5-super-vl
        # was chosen from that day's tool-call probe and answered 31 of 213 turns of one of them (nemotron-3-super
        # answered 404 for the committed key)
        "model": "z-ai/glm-5.3",
        "fallback_models": ["nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3.5-super-vl-120b-a12b",
                            "nvidia/nemotron-3-super-120b-a12b"],
        "max_turns": 80,
        "max_tokens": 16384,
        "temperature": 0.6,
        "top_p": 0.95,
        "thinking": False,
        "timeout_s": 5400,
        "context_chars": 400000,
        "self_check_tool": True,
    },
    # Which code engine runs a BUILD/FIX, in order of preference (the first available one), and what
    # happens when it fails for account reasons (usage limit, subscription ended, credits, login):
    # failover = continue the same job with the next available engine; auto_resume_on_limit = when no
    # other engine is available, resume the run by itself at the reset time the engine reported.
    "engines": {
        "order": ["hub", "claude_code", "anthropic_api", "openai_agent"],
        "failover": True,
        "auto_resume_on_limit": True,
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
# claude_code.command / extra_args are the argv of a process Kyora executes: changing them over HTTP
# would be command execution, so they are edited only in settings.json (FILE_ONLY_SETTINGS).
PATCHABLE_KEYS = {
    "nvidia.base_url", "nvidia.nemotron.model", "nvidia.nemotron.api_key",
    "nvidia.kimi.model", "nvidia.kimi.api_key", "anthropic.api_key",
    "nvidia.nemotron.base_url", "nvidia.kimi.base_url", "nvidia.nemotron.fallback_models",
    "nvidia.kimi.fallback_models", "nvidia.share_keys", "agent.base_url", "agent.api_key",
    "hub.url", "hub.token", "tokkio.ui_url", "tokkio.configurator_url",
}
PATCHABLE_SETTINGS = {
    "server.port", "server.host",
    "claude_code.model", "claude_code.fallback_models", "claude_code.effort",
    "claude_code.max_turns", "claude_code.timeout_s", "claude_code.idle_timeout_s", "claude_code.resume_for_fixes",
    "kimi.reasoning_effort", "kimi.judge_reasoning_effort", "kimi.effort_field",
    "nemotron.thinking_for_design",
    "quality.min_pass_rate", "quality.max_fix_iterations", "quality.browser_wait_s",
    "quality.require_plan_approval", "quality.auto_run_browser_tests",
    "hub.expose_ui",
    "agent_engine.enabled", "agent_engine.model", "agent_engine.fallback_models", "agent_engine.max_turns",
    "agent_engine.max_tokens", "agent_engine.temperature", "agent_engine.top_p", "agent_engine.thinking",
    "agent_engine.timeout_s", "agent_engine.context_chars", "agent_engine.self_check_tool",
    "engines.order", "engines.failover", "engines.auto_resume_on_limit",
}
ENGINE_NAMES = ("hub", "claude_code", "anthropic_api", "openai_agent")
FILE_ONLY_SETTINGS = {"claude_code.command", "claude_code.extra_args"}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
KIMI_EFFORTS = ("low", "high", "max")


@dataclass
class NvidiaModelCfg:
    model: str
    api_key: str
    base_url: str = ""  # resolved: the role's own endpoint, else nvidia.base_url
    fallback_models: list[str] = field(default_factory=list)


@dataclass
class NvidiaCfg:
    base_url: str
    nemotron: NvidiaModelCfg
    kimi: NvidiaModelCfg
    share_keys: bool = True

    def alt_keys(self, role: str) -> list[tuple[str, str]]:
        """Keys a role may fall back to when its own is refused: the other role's, on the same endpoint."""
        if not self.share_keys:
            return []
        mine, other_name = (self.nemotron, "kimi") if role == "nemotron" else (self.kimi, "nemotron")
        other = getattr(self, other_name)
        if other.api_key and other.api_key != mine.api_key and other.base_url == mine.base_url:
            return [(f"{other_name} key", other.api_key)]
        return []


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
class AgentEngineCfg:
    """settings.agent_engine + the endpoint it talks to (resolved from keys.agent / keys.nvidia)."""
    enabled: bool
    model: str
    fallback_models: list[str]
    max_turns: int
    max_tokens: int
    temperature: float
    top_p: float
    thinking: bool
    timeout_s: int
    context_chars: int
    self_check_tool: bool
    base_url: str
    api_key: str
    key_label: str
    alt_keys: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class EnginesCfg:
    order: list[str]
    failover: bool
    auto_resume_on_limit: bool


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
    preview_port: int | None = None  # None = port + 1
    allowed_hosts: list[str] = field(default_factory=list)


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


def _read_packaged_default(name: str):
    """``kyora/defaults/<name>`` via importlib.resources (works inside ``kyora.pyz``); None if absent."""
    try:
        from importlib import resources
        node = resources.files("kyora").joinpath("defaults").joinpath(name)
        return json.loads(node.read_text(encoding="utf-8")) if node.is_file() else None
    except (OSError, ValueError) as exc:
        print(f"kyora: cannot read packaged default {name}: {exc}", file=sys.stderr)
        return None


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
            for name in ("keys.json", "settings.json"):
                # inside kyora.pyz the filesystem checks above fail; importlib.resources reads the archive
                data = read_json(src / name, default=None) if src else _read_packaged_default(name)
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
        base = str(k.get("base_url") or DEFAULT_KEYS["nvidia"]["base_url"]).rstrip("/")

        def role(name: str) -> NvidiaModelCfg:
            r = k.get(name) or {}
            fallbacks = r.get("fallback_models", DEFAULT_KEYS["nvidia"][name]["fallback_models"])
            return NvidiaModelCfg(model=r.get("model") or DEFAULT_KEYS["nvidia"][name]["model"],
                                  api_key=r.get("api_key", "") or "",
                                  base_url=str(r.get("base_url") or base).rstrip("/"),
                                  fallback_models=[str(m) for m in (fallbacks or []) if m])
        return NvidiaCfg(base_url=base, nemotron=role("nemotron"), kimi=role("kimi"),
                         share_keys=bool(k.get("share_keys", DEFAULT_KEYS["nvidia"]["share_keys"])))

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
    def agent_engine(self) -> AgentEngineCfg:
        a = self.raw_settings.get("agent_engine") or {}
        d = DEFAULT_SETTINGS["agent_engine"]
        k = self.raw_keys.get("agent") or {}
        nv = self.nvidia
        own_url = str(k.get("base_url") or "").rstrip("/")
        own_key = str(k.get("api_key") or "")
        base = own_url or nv.base_url
        if own_key:  # the user's own agent key: the only key ever sent to the agent endpoint
            key, label, alts = own_key, "agent key", []
        else:
            # the role keys, each tried in turn when one is refused - but only a role's key whose endpoint IS the
            # agent endpoint: with the Kimi role moved to Moonshot, its sk- key is never sent to NVIDIA
            cands: list[tuple[str, str]] = []
            for name in ("kimi", "nemotron"):
                r = getattr(nv, name)
                if r.api_key and r.base_url == base and r.api_key not in (kk for _, kk in cands):
                    cands.append((f"{name} key", r.api_key))
            (label, key), alts = (cands[0], cands[1:]) if cands else (("kimi key", ""), [])

        def num(name, cast):
            try:
                return cast(a.get(name, d[name]))
            except (TypeError, ValueError):
                return cast(d[name])
        return AgentEngineCfg(
            enabled=bool(a.get("enabled", d["enabled"])), model=str(a.get("model") or d["model"]),
            fallback_models=[str(m) for m in (a.get("fallback_models") or []) if m],
            max_turns=num("max_turns", int), max_tokens=num("max_tokens", int), temperature=num("temperature", float),
            top_p=num("top_p", float), thinking=bool(a.get("thinking", d["thinking"])), timeout_s=num("timeout_s", int),
            context_chars=num("context_chars", int), self_check_tool=bool(a.get("self_check_tool", d["self_check_tool"])),
            base_url=base, api_key=key, key_label=label, alt_keys=alts)

    @property
    def engines(self) -> EnginesCfg:
        e = self.raw_settings.get("engines") or {}
        order = [str(n) for n in (e.get("order") or DEFAULT_SETTINGS["engines"]["order"]) if str(n) in ENGINE_NAMES]
        return EnginesCfg(order=list(dict.fromkeys(order)) or list(DEFAULT_SETTINGS["engines"]["order"]),
                          failover=bool(e.get("failover", True)),
                          auto_resume_on_limit=bool(e.get("auto_resume_on_limit", True)))

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
        pp = s.get("preview_port")
        hosts = s.get("allowed_hosts") or []
        return ServerCfg(host=s.get("host", "127.0.0.1"), port=int(s.get("port", 8765)), workers=int(s.get("workers", 2)),
                         preview_port=None if pp in (None, "") else int(pp),
                         allowed_hosts=[str(h) for h in hosts] if isinstance(hosts, list) else [])

    # ------------------------------------------------------------------ UI support
    def to_public(self, reveal: bool = False) -> dict:
        keys = copy.deepcopy(self.raw_keys)
        if not reveal:
            for path in ("nvidia.nemotron.api_key", "nvidia.kimi.api_key", "anthropic.api_key", "hub.token", "agent.api_key"):
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
                if section == "settings" and dotted in FILE_ONLY_SETTINGS:
                    if _get(target, dotted) == value:
                        continue  # an unchanged round trip is not a change
                    raise ValueError(f"settings.{dotted} cannot be changed over HTTP (it is a command Kyora runs); "
                                     f"edit {self.settings_path} and restart Kyora")
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
    if dotted == "agent.base_url" and (not isinstance(value, str) or (value and not re.match(r"^https?://[^\s/]+", value))):
        raise ValueError("agent.base_url must be an http(s) URL or empty")
    if dotted == "agent_engine.fallback_models" and (not isinstance(value, list)
                                                     or not all(isinstance(x, str) and x.strip() for x in value)):
        raise ValueError("agent_engine.fallback_models must be a list of model ids")
    if dotted == "agent_engine.model" and (not isinstance(value, str) or not value.strip()):
        raise ValueError("agent_engine.model must be a model id")
    if dotted in ("agent_engine.max_turns", "agent_engine.max_tokens", "agent_engine.timeout_s",
                  "agent_engine.context_chars") and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise ValueError(f"{dotted} must be a positive integer")
    if dotted == "agent_engine.temperature" and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 2):
        raise ValueError(f"{dotted} must be a number between 0 and 2")
    if dotted == "agent_engine.top_p" and (  # OpenAI-compatible servers (vLLM, NIM) refuse top_p outside (0, 1]
            not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < float(value) <= 1):
        raise ValueError(f"{dotted} must be greater than 0 and at most 1")
    if dotted in ("agent_engine.enabled", "agent_engine.thinking", "agent_engine.self_check_tool", "engines.failover",
                  "engines.auto_resume_on_limit") and not isinstance(value, bool):
        raise ValueError(f"{dotted} must be true or false")
    if dotted == "engines.order" and (not isinstance(value, list) or not value
                                      or any(v not in ENGINE_NAMES for v in value) or len(set(value)) != len(value)):
        raise ValueError(f"engines.order must list engines from {ENGINE_NAMES} without repeats")
    if dotted in ("nvidia.base_url", "nvidia.nemotron.base_url", "nvidia.kimi.base_url"):
        if not isinstance(value, str) or (value and not re.match(r"^https?://[^\s/]+", value)):
            raise ValueError(f"{dotted} must be an http(s) URL" + ("" if dotted == "nvidia.base_url" else " or empty"))
        if dotted == "nvidia.base_url" and not value:
            raise ValueError("nvidia.base_url must not be empty")
    if dotted in ("nvidia.nemotron.fallback_models", "nvidia.kimi.fallback_models"):
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            raise ValueError(f"{dotted} must be a list of model ids")
    if dotted == "nvidia.share_keys" and not isinstance(value, bool):
        raise ValueError("nvidia.share_keys must be true or false")
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
