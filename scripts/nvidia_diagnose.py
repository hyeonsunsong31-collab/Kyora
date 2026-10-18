#!/usr/bin/env python3
"""Diagnose what the configured NVIDIA keys can actually call (no Kyora code involved).

For each key in config/keys.json (the Nemotron and the Kimi slot) this prints, without ever
printing the key itself (only a sha256 fingerprint):

  * ``GET /models`` with and without the key (status, how many models, the Kimi/Moonshot and
    Nemotron ids that are listed)
  * a 1-token ``POST /chat/completions`` to the role's configured model, to every listed
    ``moonshotai/*`` (Kimi slot) or ``nvidia/nemotron-3*`` (Nemotron slot) model, to the other
    slot's model, and to two small control models, so a key that is rejected for every model
    (account permission, revoked key) is told apart from a model that is retired or gated

Answers are reported as they came back (HTTP status + the first 300 characters of the body).
``--out FILE`` writes the full JSON. Exit code 0 always: this is a report, not a gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTROL_MODELS = ("meta/llama-3.1-8b-instruct", "meta/llama-3.2-3b-instruct", "meta/llama-3.3-70b-instruct",
                  "nvidia/llama-3.1-nemotron-nano-8b-v1", "mistralai/mistral-7b-instruct-v0.3")


def _req(url: str, key: str | None, body: dict | None = None, timeout: float = 60.0) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "kyora-diagnose"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers,
                                                           method="POST" if data else "GET"), timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "error": f"{type(exc).__name__}: {exc}", "ms": round((time.monotonic() - t0) * 1000)}
    out = {"status": status, "ms": round((time.monotonic() - t0) * 1000), "body": text[:300]}
    if key and key in text:  # never echo a key, even if a server reflects it
        out["body"] = text[:300].replace(key, "<key>")
    try:
        out["json"] = json.loads(text)
    except ValueError:
        pass
    return out


def _model_ids(resp: dict) -> list[str]:
    data = (resp.get("json") or {}).get("data") if isinstance(resp.get("json"), dict) else None
    return sorted(str(m.get("id")) for m in data or [] if isinstance(m, dict) and m.get("id"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keys", default=str(REPO / "config" / "keys.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--all-models", action="store_true", help="also try every listed chat model with each key")
    args = ap.parse_args(argv)
    keys = json.loads(Path(args.keys).read_text(encoding="utf-8"))
    nv = keys["nvidia"]
    base = str(nv.get("base_url") or "https://integrate.api.nvidia.com/v1").rstrip("/")
    report: dict = {"base_url": base, "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "slots": {}}

    anon = _req(base + "/models", None)
    listed = _model_ids(anon)
    report["models_anonymous"] = {"status": anon.get("status"), "count": len(listed), "error": anon.get("error")}
    interesting = [m for m in listed if any(s in m.lower() for s in ("kimi", "moonshot", "nemotron"))]
    report["listed_kimi_nemotron"] = interesting
    report["listed_all"] = listed
    print(f"GET /models (no key): HTTP {anon.get('status')} {anon.get('error') or ''} - {len(listed)} models")
    print("  Kimi / Moonshot / Nemotron ids listed:")
    for m in interesting:
        print(f"    {m}")

    controls = [m for m in CONTROL_MODELS if m in listed][:2] or list(CONTROL_MODELS[:2])
    for slot in ("nemotron", "kimi"):
        key = str(nv.get(slot, {}).get("api_key") or "")
        own = str(nv.get(slot, {}).get("model") or "")
        other = str(nv.get("kimi" if slot == "nemotron" else "nemotron", {}).get("model") or "")
        fp = hashlib.sha256(key.encode()).hexdigest()[:10] if key else "(none)"
        print(f"\n== slot {slot}: key sha256:{fp} (length {len(key)}), configured model {own}")
        entry: dict = {"key_sha256_10": fp, "key_length": len(key), "model": own, "calls": []}
        with_key = _req(base + "/models", key)
        entry["models_with_key"] = {"status": with_key.get("status"), "count": len(_model_ids(with_key))}
        print(f"  GET /models with key: HTTP {with_key.get('status')} ({len(_model_ids(with_key))} models)")
        family = [m for m in interesting if (m.startswith("moonshotai/") if slot == "kimi" else "nemotron-3" in m)]
        targets: list[str] = []
        for m in [own, *family, other, *controls]:
            if m and m not in targets:
                targets.append(m)
        for model in targets:
            r = _req(base + "/chat/completions", key, {"model": model, "max_tokens": 1, "stream": False,
                                                       "messages": [{"role": "user", "content": "ping"}]})
            listed_mark = "listed" if model in listed else "NOT listed"
            line = f"  chat {model} ({listed_mark}): HTTP {r.get('status')} {r.get('ms')} ms"
            if r.get("status") != 200:
                line += f" - {r.get('error') or r.get('body')}"
            print(line)
            entry["calls"].append({"model": model, "listed": model in listed, "status": r.get("status"),
                                   "ms": r.get("ms"), "body": None if r.get("status") == 200 else (r.get("error") or r.get("body"))})
        report["slots"][slot] = entry
        sys.stdout.flush()

    # Every listed chat model with each key: which models could serve a role (or a code engine) today.
    if args.all_models:
        skip = ("embed", "rerank", "parse", "safety", "guard", "reward", "clip", "vila", "neva", "kosmos", "deplot",
                "paligemma", "fuyu", "detector", "nv-yolox", "ocdr", "cosmos", "riva", "audio", "asr", "tts")
        chat_models = [m for m in listed if not any(k in m.lower() for k in skip)]
        print(f"\n== every listed chat model ({len(chat_models)} of {len(listed)}), 1-token chat with each key")
        report["all_models"] = {}
        for slot in ("nemotron", "kimi"):
            key = str(nv.get(slot, {}).get("api_key") or "")
            ok, refused, missing = [], [], []
            for model in chat_models:
                r = _req(base + "/chat/completions", key, {"model": model, "max_tokens": 1, "stream": False,
                                                           "messages": [{"role": "user", "content": "ping"}]}, timeout=30)
                (ok if r.get("status") == 200 else refused if r.get("status") in (401, 403) else missing).append(
                    (model, r.get("status"), r.get("ms")))
            report["all_models"][slot] = {"ok": ok, "refused": refused, "other": missing}
            print(f"  {slot} key: {len(ok)} answered, {len(refused)} refused (401/403), {len(missing)} other")
            for model, status, ms in ok:
                print(f"    200 {ms:>6} ms  {model}")
            sys.stdout.flush()

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
