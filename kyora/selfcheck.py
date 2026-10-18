"""Self-check: live status of the models, the code engines, local tools and Tokkio.

Every line of the report is measured on this device right now; nothing is
assumed or estimated. A probe that crashes or times out is reported as such
and never aborts the rest of the report. Probes run concurrently in a
``ThreadPoolExecutor`` with per-item time budgets; the finished report is
cached (:meth:`SelfCheck.last`) and announced with a ``selfcheck.update`` event.

Item statuses: ``ok`` (works), ``warn`` (works with a caveat or optional piece
missing), ``fail`` (a required piece does not work), ``skip`` (not applicable or
not requested). Standard library only.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from .models.kimi import EXTRA_BODY_EFFORT_NOTE
from .pipeline.events import SELFCHECK_UPDATE
from .server import lan_addresses
from .util.fs import which
from .util.ids import now_iso
from .util.procs import run as run_cmd

log = logging.getLogger("kyora.selfcheck")

STATUSES = ("ok", "warn", "fail", "skip")
MIN_FREE_BYTES = 2 * 1024 ** 3
MODEL_PROBE_TIMEOUT_S = 20.0
TOOL_TIMEOUT_S = 15.0
NODE_MISSING = "Node not found: JS syntax checks run in the browser instead; Claude Code needs Node on this device"
ISH_CLAUDE = "Claude Code cannot run on iSH; use a Kyora Hub"
NO_ENGINE = ("no code engine: install Claude Code and run claude login, or link a Kyora Hub (hub.url), "
             "or add ANTHROPIC_API_KEY + pip install anthropic")
TOKKIO_NOT_DEPLOYED = "Tokkio not deployed (tokkio.ui_url empty) - see docs/tokkio-overlay.md"
KIMI_EFFORT_HINT = "set kimi.effort_field to extra_body in Settings"
KIMI_EFFORT_HINT_TOP = "set kimi.effort_field back to top_level in Settings"
KIMI_EFFORT_BEST_EFFORT = EXTRA_BODY_EFFORT_NOTE  # kimi.effort_field = extra_body is never reported as applied
LABELS: dict[str, str] = {
    "python": "Python", "platform": "Platform", "git": "git", "node": "Node.js", "playwright": "Playwright (optional)",
    "claude_cli": "Claude Code CLI", "claude_auth": "Claude Code login", "claude_model": "Claude Code model",
    "nemotron": "NVIDIA Nemotron-3-Ultra", "kimi": "Kimi K3 (Moonshot AI)", "anthropic_api": "Anthropic Messages API (optional)",
    "engine_selected": "Selected code engine", "hub": "Kyora Hub", "tokkio_ui": "Tokkio UI",
    "tokkio_configurator": "Tokkio ACE Configurator", "disk": "Disk space", "lan": "LAN",
}
ORDER = tuple(LABELS)


def item(id_: str, status: str, detail: str, measured: dict | None = None) -> dict:
    """One report line ``{id, label, status, detail, measured}``."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    return {"id": id_, "label": LABELS.get(id_, id_), "status": status, "detail": str(detail),
            "measured": dict(measured or {})}


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _excerpt(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def _effort_sent(extra: Any, prefix: str = "") -> str:
    """The body field that carries the reasoning effort, as sent: ``reasoning_effort=max`` or
    ``chat_template_kwargs.reasoning_effort=low``; the whole ``extra`` when no such field is in it."""
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key == "reasoning_effort" and not isinstance(value, dict):
                return f"{prefix}{key}={value}"
            found = _effort_sent(value, f"{prefix}{key}.") if isinstance(value, dict) else ""
            if found:
                return found
    return "" if prefix else _excerpt(extra, 120)


class _Probe:
    """A probe callable producing one or more items, with the ids it is responsible for and a time budget."""

    def __init__(self, ids: tuple[str, ...], fn: Callable[[], list[dict] | dict], timeout_s: float):
        self.ids = ids
        self.fn = fn
        self.timeout_s = timeout_s


class SelfCheck:
    """Builds the self-check report for ``app`` (the composition root, :class:`kyora.app.App`)."""

    def __init__(self, app, *, max_workers: int = 8, model_timeout_s: float = MODEL_PROBE_TIMEOUT_S,
                 claude_probe_timeout_s: float = 150.0):
        self.app = app
        self.max_workers = max(1, int(max_workers))
        self.model_timeout_s = float(model_timeout_s)
        self.claude_probe_timeout_s = float(claude_probe_timeout_s)
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._last: dict | None = None

    # ------------------------------------------------------------------ public
    def last(self) -> dict | None:
        """The most recent report (``None`` before the first run)."""
        with self._lock:
            return self._last

    def run(self, *, probe_models: bool = True, probe_claude: bool = False) -> dict:
        """Run every probe and return ``{items, platform, lan_urls, generated_at, summary, ...}``.

        ``probe_models`` sends a 1-token chat to Nemotron and Kimi; ``probe_claude``
        runs a 1-turn ``claude -p`` to resolve the model that actually answers.
        Concurrent calls are serialised.
        """
        with self._run_lock:
            started = time.monotonic()
            results: dict[str, dict] = {}
            phase1 = self._phase1(bool(probe_models), bool(probe_claude))
            results.update(self._execute(phase1))
            # the engine choice reuses the availability results cached by phase 1
            results.update(self._execute([_Probe(("engine_selected",), self._p_engine_selected, 60.0)]))
            self._claude_not_needed(results)
            items = [results[i] for i in ORDER if i in results]
            summary = {s: sum(1 for it in items if it["status"] == s) for s in STATUSES}
            overall = "fail" if summary["fail"] else "warn" if summary["warn"] else "ok"
            lan = results.get("lan", {}).get("measured", {}).get("urls") or []
            report = {
                "items": items,
                "platform": dict(getattr(self.app, "platform", None) or {}),
                "lan_urls": list(lan),
                "generated_at": now_iso(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "probe_models": bool(probe_models),
                "probe_claude": bool(probe_claude),
                "summary": summary,
                "overall": overall,
            }
            with self._lock:
                self._last = report
            self._publish(report)
            return report

    # ------------------------------------------------------------------ orchestration
    def _phase1(self, probe_models: bool, probe_claude: bool) -> list[_Probe]:
        mt = self.model_timeout_s
        return [
            _Probe(("python",), self._p_python, 5.0),
            _Probe(("platform",), self._p_platform, 5.0),
            _Probe(("git",), self._p_git, TOOL_TIMEOUT_S + 5),
            _Probe(("node",), self._p_node, TOOL_TIMEOUT_S + 5),
            _Probe(("playwright",), self._p_playwright, TOOL_TIMEOUT_S + 5),
            _Probe(("claude_cli", "claude_auth", "claude_model"), lambda: self._p_claude(probe_claude),
                   60.0 + (self.claude_probe_timeout_s + 30.0 if probe_claude else 0.0)),
            _Probe(("nemotron",), lambda: self._p_nemotron(probe_models), 3 * mt + 15),
            _Probe(("kimi",), lambda: self._p_kimi(probe_models), 5 * mt + 15),
            _Probe(("anthropic_api",), self._p_anthropic, 5.0),
            _Probe(("hub",), self._p_hub, 45.0),
            _Probe(("tokkio_ui", "tokkio_configurator"), self._p_tokkio, 20.0),
            _Probe(("disk",), self._p_disk, 10.0),
            _Probe(("lan",), self._p_lan, 10.0),
        ]

    def _execute(self, probes: list[_Probe]) -> dict[str, dict]:
        """Run ``probes`` concurrently; each gets its own deadline counted from the start."""
        out: dict[str, dict] = {}
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(probes))),
                                  thread_name_prefix="selfcheck")
        try:
            started = time.monotonic()
            futures = [(p, pool.submit(self._guard, p)) for p in probes]
            for probe, fut in futures:
                remaining = max(0.0, started + probe.timeout_s - time.monotonic())
                try:
                    produced = fut.result(timeout=remaining)
                except FutureTimeout:
                    log.warning("selfcheck: %s timed out after %.0f s", "/".join(probe.ids), probe.timeout_s)
                    produced = [item(i, "fail", f"no answer within {probe.timeout_s:.0f} s (probe timed out)")
                                for i in probe.ids]
                for it in produced:
                    out[it["id"]] = it
                for i in probe.ids:  # a probe that forgot an id must still produce a line
                    out.setdefault(i, item(i, "fail", "probe returned no result"))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out

    @staticmethod
    def _guard(probe: _Probe) -> list[dict]:
        try:
            res = probe.fn()
        except Exception as exc:  # noqa: BLE001 - one probe must never abort the report
            log.exception("selfcheck probe %s failed", "/".join(probe.ids))
            return [item(i, "fail", f"probe error: {type(exc).__name__}: {exc}") for i in probe.ids]
        return [res] if isinstance(res, dict) else list(res or [])

    def _publish(self, report: dict) -> None:
        bus = getattr(self.app, "bus", None)
        if bus is None:
            return
        try:
            bus.publish(SELFCHECK_UPDATE, {"summary": report["summary"], "overall": report["overall"],
                                           "generated_at": report["generated_at"],
                                           "statuses": {it["id"]: it["status"] for it in report["items"]}})
        except Exception as exc:  # noqa: BLE001
            log.error("selfcheck: publish failed: %s", exc)

    # ------------------------------------------------------------------ helpers
    def _platform(self) -> dict:
        return dict(getattr(self.app, "platform", None) or {})

    def _is_ish(self) -> bool:
        p = self._platform()
        return p.get("kind") == "ish" or bool(p.get("is_ish"))

    def _missing(self, name: str, default: str) -> str:
        return (getattr(self.app, "missing", None) or {}).get(name, default)

    def _claude_not_needed(self, results: dict[str, dict]) -> None:
        """A missing Claude Code CLI is no failure while another engine (a linked Hub on Termux or a
        ``install.ps1 -SkipClaude`` PC, or the Messages API) is the selected one; no engine at all is
        reported by ``engine_selected``."""
        cli, sel = results.get("claude_cli"), results.get("engine_selected")
        if not cli or not sel or cli["status"] != "fail" or sel["status"] != "ok":
            return
        backend = (sel.get("measured") or {}).get("backend") or ""
        if not backend or backend == getattr(getattr(self.app, "claude_engine", None), "name", "claude_code"):
            return
        results["claude_cli"] = item("claude_cli", "skip", f"{cli['detail']}; not needed: the selected code engine "
                                     f"is {backend}", {**cli["measured"], "not_needed_for": backend})

    # ------------------------------------------------------------------ local probes
    def _p_python(self) -> dict:
        ver = ".".join(str(x) for x in sys.version_info[:3])
        measured = {"version": ver, "executable": sys.executable, "implementation": sys.implementation.name}
        if sys.version_info >= (3, 10):
            return item("python", "ok", f"Python {ver} ({sys.executable})", measured)
        return item("python", "fail", f"Python {ver}: Kyora needs Python 3.10 or newer", measured)

    def _p_platform(self) -> dict:
        p = self._platform()
        kind = p.get("kind") or "unknown"
        detail = f"{kind} ({p.get('system') or '?'} {p.get('machine') or '?'})"
        if p.get("frozen_zipapp"):
            detail += ", running from kyora.pyz"
        if kind == "ish":
            detail += "; the code engine must run on a Kyora Hub"
        return item("platform", "ok" if p else "warn", detail if p else "platform information unavailable",
                    {k: p.get(k) for k in ("kind", "system", "machine", "python") if k in p})

    @staticmethod
    def _version_of(exe: str, args: list[str]) -> tuple[int, str]:
        rc, out, err = run_cmd([exe] + args, timeout=TOOL_TIMEOUT_S)
        return rc, _first_line(out) or _first_line(err)

    def _p_git(self) -> dict:
        exe = which("git")
        if not exe:
            return item("git", "warn", "git not found: projects are versioned as snapshots instead of git commits")
        rc, ver = self._version_of(exe, ["--version"])
        if rc != 0:
            return item("git", "warn", f"'git --version' failed (rc={rc}): {_excerpt(ver)}", {"path": exe})
        return item("git", "ok", f"{ver} ({exe})", {"path": exe, "version": ver})

    def _p_node(self) -> dict:
        exe = which("node")
        if not exe:
            return item("node", "warn", NODE_MISSING)
        rc, ver = self._version_of(exe, ["--version"])
        if rc != 0:
            return item("node", "warn", f"'node --version' failed (rc={rc}): {_excerpt(ver)}", {"path": exe})
        return item("node", "ok", f"Node {ver} ({exe})", {"path": exe, "version": ver})

    def _p_playwright(self) -> dict:
        try:
            spec = importlib.util.find_spec("playwright")
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            ver = None
            try:
                from importlib import metadata
                ver = metadata.version("playwright")
            except Exception:  # noqa: BLE001 - version is informational
                ver = None
            return item("playwright", "ok", f"Python playwright {ver or ''} installed".replace("  ", " "),
                        {"runtime": "python", "version": ver})
        node = which("node")
        if node:
            env = dict(os.environ)
            node_dir = Path(node).resolve().parent
            global_root = node_dir / "node_modules" if os.name == "nt" else node_dir.parent / "lib" / "node_modules"
            env["NODE_PATH"] = os.pathsep.join([p for p in (env.get("NODE_PATH"), str(global_root)) if p])
            rc, out, _err = run_cmd([node, "-e", "process.stdout.write(require('playwright/package.json').version)"],
                                    env=env, timeout=TOOL_TIMEOUT_S)
            ver = _first_line(out)
            if rc == 0 and ver:
                return item("playwright", "ok", f"Node playwright {ver} installed", {"runtime": "node", "version": ver})
        return item("playwright", "skip",
                    "Playwright not installed (optional): browser tests run in any browser through the test harness")

    def _p_disk(self) -> dict:
        home = Path(getattr(self.app, "home", None) or ".")
        usage = shutil.disk_usage(str(home))
        measured = {"free_bytes": usage.free, "total_bytes": usage.total, "path": str(home)}
        detail = f"{_gb(usage.free)} free of {_gb(usage.total)} at {home}"
        if usage.free >= MIN_FREE_BYTES:
            return item("disk", "ok", detail, measured)
        return item("disk", "warn", detail + " (less than 2 GB: builds, slides and screenshots may not fit)", measured)

    def _p_lan(self) -> dict:
        addrs = lan_addresses()
        server_url = str(getattr(self.app, "server_url", "") or "")
        cfg = getattr(self.app, "config", None)
        port = server_url.rsplit(":", 1)[-1] if server_url and server_url.count(":") >= 2 else (
            str(cfg.server.port) if cfg is not None else "8765")
        urls = [f"http://{ip}:{port}" for ip in addrs]
        measured = {"addresses": addrs, "urls": urls}
        if not addrs:
            return item("lan", "warn", "no LAN IPv4 address found: other devices cannot reach this one directly", measured)
        detail = ", ".join(urls)
        host = cfg.server.host if cfg is not None else "127.0.0.1"
        if host in ("127.0.0.1", "localhost", "::1"):
            detail += (f" (the server listens on {host} only; set server.host to 0.0.0.0 and enable hub.expose_ui "
                       "to open Kyora from a phone)")
        return item("lan", "ok", detail, measured)

    # ------------------------------------------------------------------ Claude Code
    def _p_claude(self, probe_claude: bool) -> list[dict]:
        if self._is_ish():
            return [item("claude_cli", "skip", ISH_CLAUDE), item("claude_auth", "skip", ISH_CLAUDE),
                    item("claude_model", "skip", ISH_CLAUDE)]
        eng = getattr(self.app, "claude_engine", None)
        if eng is None:
            reason = self._missing("claude_code", "Claude Code engine component unavailable")
            return [item("claude_cli", "fail", reason),
                    item("claude_auth", "skip", "Claude Code CLI not available"),
                    item("claude_model", "skip", "Claude Code CLI not available")]
        avail = eng.availability()
        d = dict(getattr(avail, "details", None) or {})
        measured = {k: d.get(k) for k in ("version", "path", "command") if d.get(k) is not None}
        if not avail.available and not d.get("version"):
            cli = item("claude_cli", "fail", avail.reason or "Claude Code CLI unavailable", measured)
            return [cli, item("claude_auth", "skip", "Claude Code CLI not available"),
                    item("claude_model", "skip", "Claude Code CLI not available")]
        cli_detail = f"{d.get('version') or 'version unknown'} ({d.get('path') or '?'})"
        cli = item("claude_cli", "ok", cli_detail, measured)

        logged_in = d.get("logged_in")
        method = d.get("auth_method")
        auth_measured = {"logged_in": logged_in, "auth_method": method, "api_provider": d.get("api_provider")}
        if logged_in is True:
            auth = item("claude_auth", "ok", f"logged in ({method or 'method unknown'})", auth_measured)
        elif logged_in is False:
            auth = item("claude_auth", "fail", avail.reason or "not logged in: run 'claude auth login'", auth_measured)
        else:
            auth = item("claude_auth", "warn",
                        f"login state unknown: {d.get('auth_status_error') or avail.reason or 'claude auth status unsupported'}",
                        auth_measured)

        if not probe_claude:
            model = item("claude_model", "skip", "run with probe_claude to resolve the active model")
        elif not avail.available:
            model = item("claude_model", "skip", f"not probed: {avail.reason}")
        else:
            model = self._claude_model(eng)
        return [cli, auth, model]

    def _claude_model(self, eng) -> dict:
        cfg = self.app.config.claude_code
        res = eng.probe_model(cfg.model, list(cfg.fallback_models), timeout_s=self.claude_probe_timeout_s)
        measured = {k: res.get(k) for k in ("requested", "active", "fallback_used", "duration_ms", "refused")
                    if k in res}
        refused = res.get("refused") or []
        notes = "; ".join(f"{r.get('model')} refused: {_excerpt(r.get('reason'), 160)}" for r in refused
                          if isinstance(r, dict))
        if res.get("ok") and res.get("active"):
            detail = f"active model {res['active']}"
            if res.get("fallback_used"):
                detail += f" (fallback from {res.get('requested') or cfg.model})"
            detail += f", {res.get('duration_ms', 0)} ms"
            if notes:
                detail += f"; {notes}"
            return item("claude_model", "ok", detail, measured)
        if res.get("ok"):
            return item("claude_model", "warn", "probe succeeded but the CLI did not report which model answered", measured)
        detail = f"model probe failed: {_excerpt(res.get('error') or 'unknown error', 400)}"
        if notes:
            detail += f"; {notes}"
        return item("claude_model", "fail", detail, measured)

    # ------------------------------------------------------------------ NVIDIA models
    def _model_probe(self, client, model: str, name: str, key_present: bool, chain=None,
                     own_key: str = "") -> tuple[str, str, dict, dict]:
        """Probe a role's model (and, when the endpoint says it is gone, its fallbacks) -> ``(status, detail, measured, raw)``.

        ``fail`` when the role cannot answer at all (key refused, model gone with no working fallback,
        endpoint unreachable); ``warn`` when it answers only through a fallback model or the other
        role's key, or the failure may be temporary (429/5xx); ``ok`` otherwise.
        """
        from .models.nim_client import MODEL_GONE_STATUSES, explain_http_failure
        res = client.probe(model, timeout_s=self.model_timeout_s)
        notes = client.pop_key_notes() if callable(getattr(client, "pop_key_notes", None)) else []
        measured = {"model": model, "latency_ms": res.get("latency_ms"), "model_listed": res.get("model_listed"),
                    "status": res.get("status"), "chat_ok": bool(res.get("chat_ok"))}
        err = str(res.get("error") or "")
        if not res.get("reachable"):
            return "fail", f"{name} endpoint unreachable: {_excerpt(err or 'no answer', 400)}", measured, res
        status_code = res.get("status")
        answered_by, fallback_note = model, ""
        if not res.get("chat_ok") and status_code in MODEL_GONE_STATUSES and chain is not None:
            why = explain_http_failure(status_code, err, model)
            for fb in [m for m in chain.candidates() if m != model]:
                fres = client.probe(fb, timeout_s=self.model_timeout_s)
                notes += client.pop_key_notes() if callable(getattr(client, "pop_key_notes", None)) else []
                if fres.get("chat_ok"):
                    chain.mark_gone(model, why)  # runs start with the fallback instead of re-asking every call
                    answered_by, res = fb, {**fres, "requested_status": status_code}
                    fallback_note = (f"{model}: HTTP {status_code} - {why}. The role is answered by the fallback "
                                     f"model {fb}")
                    measured.update(fallback_model=fb, latency_ms=fres.get("latency_ms"), chat_ok=True)
                    break
        key_label = res.get("key_label") or ""
        if key_label:
            measured["key"] = key_label
        key_note = ""
        if res.get("chat_ok") and own_key and key_label and key_label != own_key:
            refused = next((n for n in notes if n.get("from_key") == own_key), None)
            code = f" (HTTP {refused['status']})" if refused and refused.get("status") else ""
            key_note = (f"the {own_key} was refused{code}; the {key_label} answered "
                        f"(nvidia.share_keys) - {explain_http_failure(refused.get('status') if refused else 403, '', model)}")
            measured["key_fallback"] = True
        if not res.get("chat_ok"):
            head = f"reachable, but the 1-token chat to {model} failed" + (f" (HTTP {status_code})" if status_code else "")
            detail = f"{head}: {_excerpt(err, 300)}"
            hint = explain_http_failure(status_code, err, model)
            if hint:
                detail += f" - {hint}"
                measured["hint"] = hint
            if status_code in (401, 403) and getattr(client, "alt_key_labels", None) == [] and notes:
                detail += " (the other configured key was refused too)"
            if not key_present:
                detail += " (no key configured)"
            definite = status_code in (401, 403, 404, 410) or not key_present
            return ("fail" if definite else "warn"), detail, measured, res
        detail = f"{answered_by}: 1-token chat {res.get('latency_ms')} ms"
        status = "ok"
        if fallback_note:
            detail, status = f"{fallback_note} (1-token chat {res.get('latency_ms')} ms)", "warn"
        if key_note:
            detail, status = f"{detail}; {key_note}", "warn"
        if res.get("model_listed") is False and not fallback_note:
            detail += f"; {answered_by} is not listed by /models"
            status = "warn"
        return status, detail, measured, res

    def _p_nemotron(self, probe_models: bool) -> dict:
        if not probe_models:
            return item("nemotron", "skip", "live probe skipped (run with probe_models)")
        client = getattr(self.app, "nim_nemotron", None)
        if client is None:
            return item("nemotron", "fail", self._missing("nim_client", "NVIDIA API client unavailable"))
        nv = self.app.config.nvidia
        role = getattr(self.app, "nemotron", None)
        status, detail, measured, _res = self._model_probe(client, nv.nemotron.model, "Nemotron", bool(nv.nemotron.api_key),
                                                           chain=getattr(role, "chain", None), own_key="nemotron key")
        return item("nemotron", status, detail, measured)

    def _p_kimi(self, probe_models: bool) -> dict:
        if not probe_models:
            return item("kimi", "skip", "live probe skipped (run with probe_models)")
        client = getattr(self.app, "nim_kimi", None)
        if client is None:
            return item("kimi", "fail", self._missing("nim_client", "NVIDIA API client unavailable"))
        nv = self.app.config.nvidia
        model = nv.kimi.model
        role = getattr(self.app, "kimi", None)
        status, detail, measured, res = self._model_probe(client, model, "Kimi", bool(nv.kimi.api_key),
                                                          chain=getattr(role, "chain", None), own_key="kimi key")
        measured["reasoning_seen"] = res.get("reasoning_seen")
        errors = [str(res.get("error") or "")]
        if status in ("ok", "warn") and measured.get("chat_ok"):
            check = self._kimi_effort_check(client, measured.get("fallback_model") or model)
            if check is not None:
                measured["effort_check"] = check["status"]
                measured["effort_sent"] = check["sent"]
                if check.get("field") == "extra_body":
                    measured["effort_best_effort"] = True  # the model may ignore the chat-template keyword
                detail += f"; {check['note']}"
                if check["status"] == "failed":
                    status = "warn"
                    errors.append(check["error"])
                elif check.get("reasoning_seen"):
                    measured["reasoning_seen"] = True
            detail += "; reasoning_content seen" if measured.get("reasoning_seen") else \
                "; no reasoning_content in the 1-token reply"
        if any("reasoning_effort" in e for e in errors):
            # Never suggest the mode that was just rejected.
            hint = KIMI_EFFORT_HINT_TOP if getattr(getattr(self.app, "kimi", None), "effort_field",
                                                   "top_level") == "extra_body" else KIMI_EFFORT_HINT
            detail += f" - {hint}"
            measured["hint"] = hint
        return item("kimi", status, detail, measured)

    def _kimi_effort_check(self, client, model: str) -> dict | None:
        """1-token chat carrying the configured reasoning effort exactly as the Kimi wrapper sends it.

        Returns ``None`` when the check is not possible (no Kimi wrapper), else
        ``{status: "ok"|"failed", note, error, reasoning_seen}``.
        """
        kimi = getattr(self.app, "kimi", None)
        chat = getattr(client, "chat", None)
        if kimi is None or not callable(chat) or not callable(getattr(kimi, "effort_extra", None)):
            return None
        extra = kimi.effort_extra(None, model)
        if not extra:
            return None
        field = getattr(kimi, "effort_field", "top_level")
        if "chat_template_kwargs" in extra and "enable_thinking" in extra["chat_template_kwargs"]:
            field = "fallback model"
        sent = _effort_sent(extra)
        try:
            res = chat(model, [{"role": "user", "content": "ping"}], stream=False, max_tokens=1, extra=extra,
                       timeout_s=self.model_timeout_s)
        except Exception as exc:  # noqa: BLE001 - reported in the item, never raised
            status = getattr(exc, "status", None)
            msg = str(exc)
            return {"status": "failed", "error": msg, "reasoning_seen": None, "field": field, "sent": sent,
                    "note": f"chat with {sent} ({field}) failed"
                            f"{f' (HTTP {status})' if status else ''}: {_excerpt(msg, 300)}"}
        # Accepted means the gateway answered 200 to a body carrying exactly ``sent``; a 1-token reply
        # cannot show whether the effort was applied. Kimi K3's template is not documented to read the
        # extra_body mode's chat_template_kwargs.reasoning_effort at all, so that mode is labelled best-effort.
        mode = f"{field}; {KIMI_EFFORT_BEST_EFFORT}" if field == "extra_body" else field
        return {"status": "ok", "error": "", "field": field, "sent": sent,
                "note": f"request with {sent} accepted ({mode})",
                "reasoning_seen": bool(getattr(res, "reasoning", ""))}

    # ------------------------------------------------------------------ engines
    def _p_anthropic(self) -> dict:
        cfg = getattr(self.app, "config", None)
        key = bool(cfg.anthropic_api_key) if cfg is not None else False
        try:
            pkg = importlib.util.find_spec("anthropic") is not None
        except (ImportError, ValueError):
            pkg = "anthropic" in sys.modules
        ver = None
        if pkg:
            try:
                from importlib import metadata
                ver = metadata.version("anthropic")
            except Exception:  # noqa: BLE001 - version is informational
                ver = None
        measured = {"api_key_present": key, "package_installed": pkg, "package_version": ver}
        if key and pkg:
            return item("anthropic_api", "ok", f"API key set, anthropic {ver or ''} installed: the Messages-API engine "
                        "is available".replace("  ", " "), measured)
        if key:
            return item("anthropic_api", "warn",
                        "ANTHROPIC_API_KEY set but the anthropic package is not installed (pip install anthropic)", measured)
        return item("anthropic_api", "skip", "no ANTHROPIC_API_KEY (optional; the default engine is the Claude Code CLI "
                    "with your subscription login)", measured)

    def _p_engine_selected(self) -> dict:
        engine_fn = getattr(self.app, "engine", None)
        eng = engine_fn() if callable(engine_fn) else None
        if eng is None:
            return item("engine_selected", "fail", NO_ENGINE)
        name = getattr(eng, "name", type(eng).__name__)
        detail = name
        measured: dict = {"backend": name}
        try:
            av = eng.availability()
            d = getattr(av, "details", None) or {}
            if d.get("model"):
                measured["model"] = d.get("model")
                detail += f" (model {d['model']})"
            if getattr(av, "reason", ""):
                detail += f"; {av.reason}"
        except Exception as exc:  # noqa: BLE001 - selection already succeeded; details are extra
            log.warning("selfcheck: availability details for %s failed: %s", name, exc)
        return item("engine_selected", "ok", detail, measured)

    def _p_hub(self) -> dict:
        cfg = getattr(self.app, "config", None)
        url = cfg.hub.url if cfg is not None else ""
        if not url:
            return item("hub", "skip", "no Kyora Hub linked (hub.url empty)")
        hub = getattr(self.app, "hub_client", None)
        if hub is None:
            return item("hub", "fail", f"{url}: {self._missing('hub_client', 'hub client component unavailable')}",
                        {"url": url})
        t0 = time.monotonic()
        av = hub.availability()
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        d = dict(getattr(av, "details", None) or {})
        measured = {"url": url, "latency_ms": ms, **{k: v for k, v in d.items() if isinstance(v, (str, int, float, bool))}}
        if av.available:
            extra = f" ({av.reason})" if av.reason else ""
            return item("hub", "ok", f"{url} reachable, code engine available{extra}", measured)
        return item("hub", "fail", f"{url}: {av.reason or 'unavailable'}", measured)

    def _p_tokkio(self) -> list[dict]:
        tk = getattr(self.app, "tokkio", None)
        if tk is None:
            reason = self._missing("tokkio", "Tokkio connector unavailable")
            return [item("tokkio_ui", "fail", reason), item("tokkio_configurator", "skip", reason)]
        cfg = getattr(self.app, "config", None)
        if cfg is not None and hasattr(tk, "update_config"):
            tk.update_config(cfg.tokkio)
        if not tk.configured():
            return [item("tokkio_ui", "skip", TOKKIO_NOT_DEPLOYED), item("tokkio_configurator", "skip", TOKKIO_NOT_DEPLOYED)]
        h = tk.health()
        ui = h.get("ui") or {}
        ui_m = {k: ui.get(k) for k in ("url", "reachable", "status", "latency_ms")}
        if ui.get("reachable") and ui.get("ok"):
            ui_item = item("tokkio_ui", "ok", f"{ui.get('url')} answered HTTP {ui.get('status')} in {ui.get('latency_ms')} ms",
                           ui_m)
        elif ui.get("reachable"):
            ui_item = item("tokkio_ui", "warn", f"{ui.get('url')} reachable but answered {ui.get('error') or ui.get('status')}",
                           ui_m)
        else:
            ui_item = item("tokkio_ui", "fail", f"{ui.get('url')}: {ui.get('error') or 'unreachable'}", ui_m)
        conf = h.get("configurator") or {}
        conf_m = {k: conf.get(k) for k in ("url", "reachable", "status", "latency_ms", "openapi_found", "paths")}
        if not conf.get("configured"):
            conf_item = item("tokkio_configurator", "skip",
                             "tokkio.configurator_url empty (needed only to upload the Kyora overlay) - see docs/tokkio-overlay.md")
        elif conf.get("openapi_found"):
            conf_item = item("tokkio_configurator", "ok",
                             f"{conf.get('url')}/openapi.json found ({conf.get('paths')} paths, {conf.get('latency_ms')} ms)",
                             conf_m)
        elif conf.get("reachable"):
            conf_item = item("tokkio_configurator", "warn",
                             f"{conf.get('url')} reachable but no OpenAPI document: {conf.get('error') or conf.get('status')}",
                             conf_m)
        else:
            conf_item = item("tokkio_configurator", "fail", f"{conf.get('url')}: {conf.get('error') or 'unreachable'}", conf_m)
        return [ui_item, conf_item]


def register_routes(router, app) -> None:
    """``GET /api/selfcheck`` (last report or a quick local run) and ``POST /api/selfcheck/run``."""
    from .server import Response

    def _checker():
        sc = getattr(app, "selfcheck", None)
        if sc is None:
            raise RuntimeError((getattr(app, "missing", None) or {}).get("selfcheck", "selfcheck unavailable"))
        return sc

    def _flag(body: dict, name: str, default: bool) -> bool:
        val = body.get(name, default)
        if isinstance(val, bool):
            return val
        if val in (0, 1):
            return bool(val)
        if isinstance(val, str) and val.lower() in ("true", "false", "1", "0", "yes", "no"):
            return val.lower() in ("true", "1", "yes")
        raise ValueError(f"{name} must be a boolean")

    def get_last(req, params):
        try:
            sc = _checker()
        except RuntimeError as exc:
            return Response.error(503, str(exc))
        report = sc.last()
        if report is None:
            report = sc.run(probe_models=False, probe_claude=False)
        return Response.json(report)

    def post_run(req, params):
        try:
            sc = _checker()
        except RuntimeError as exc:
            return Response.error(503, str(exc))
        body = req.json() if req.body else {}
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        report = sc.run(probe_models=_flag(body, "probe_models", True), probe_claude=_flag(body, "probe_claude", False))
        return Response.json(report)

    router.add("GET", "/api/selfcheck", get_last)
    router.add("POST", "/api/selfcheck/run", post_run)
