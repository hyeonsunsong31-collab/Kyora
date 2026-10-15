#!/usr/bin/env python3
"""Upload the Kyora overlay into a running Tokkio 5.0.0-ga ACE controller (standard library only).

Dry run (default): print the file map, the .env additions and the pyproject note; upload nothing.

    python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180

Apply:

    python tokkio-kyora/deploy/upload_overlay.py --configurator http://<node-ip>:30180 --apply \
        [--kubectl-fallback] [--namespace app] [--events-url http://<kyora-host>:8765] [--token <hub token>]

What --apply does, in order, and what it can and cannot verify:

1. ``GET <configurator>/openapi.json`` (the ACE Configurator API, NodePort 30180 in the
   one-click deployments). The upload route of the Configurator is not documented in the
   vendored tree, so the script searches the OpenAPI paths for a PUT/POST file-upload
   operation addressed by service name and volume name, prints every candidate it found and
   the one it picked, then uploads each file with a multipart body to
   service ``ace-controller-ace-controller-deployment`` / volume ``app-storage-volume``
   (names from ``llm-rag/tokkio-1stream-with-ui/tokkio-app-params.yaml`` -> ace-configurator
   config; that volume has ``refresh_policy: HotReload``). A file counts as uploaded only on an
   HTTP 2xx; if the spec also offers a GET for the same path the file is read back and its
   sha256 compared ("verified by read-back").
2. If there is no OpenAPI document, no matching path, or an upload fails: with
   ``--kubectl-fallback`` the script runs ``kubectl -n <ns> get pods -l app=ace-controller -o name``
   (then a name-prefix search if the label matches nothing), checks that ``/app/configs/config.yaml``
   and ``/app/src/bot.py`` exist in container ``ace-controller-container`` (the Dockerfile's
   WORKDIR is /app and entrypoint.sh uses ./configs and src.bot:app), ``kubectl cp``s each file to
   ``/app/<path>`` and compares ``sha256sum`` inside the pod with the local file. uvicorn runs with
   ``--reload --reload-dir ./ --reload-include <config>`` (entrypoint.sh) and picks the change up.

The script never claims success without an HTTP 2xx or a kubectl exit status 0, and the final
summary lists separately what was verified and what was not. Whether uvicorn actually reloaded
and the pipeline came up with the overlay is NOT verified here: check the pod log for the line
``Kyora overlay: events_url=...`` and ``KyoraProcessor: connected to Kyora events``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERLAY_DIR = HERE.parent  # tokkio-kyora/
SERVICE = "ace-controller-ace-controller-deployment"
VOLUME = "app-storage-volume"
CONTAINER = "ace-controller-container"
APP_ROOT = "/app"
# (local file relative to tokkio-kyora/, path inside the app-storage-volume). bot.py last: its reload
# makes uvicorn re-import everything (processor module, .env) in one go.
FILE_MAP: list[tuple[str, str]] = [
    ("configs/config.kyora.yaml", "configs/config.yaml"),
    ("src/kyora_processor.py", "src/kyora_processor.py"),
    ("src/bot.kyora.py", "src/bot.py"),
]
PYPROJECT_NOTE = (
    "pyproject.toml: no new dependencies. kyora_processor.py imports pipecat, nvidia_pipecat, pydantic and httpx. "
    "httpx is not a direct pipecat-ai 0.0.68 dependency (that is aiohttp~=3.11.12) but it is required by "
    "openai~=1.70.0, which both pipecat-ai and nvidia-pipecat 0.2.0 require; it is pinned in src/llm-rag/uv.lock "
    "(httpx 0.27.2) and nvidia_pipecat/services/nvidia_llm.py imports it. If a future lock drops httpx, add "
    "\"httpx>=0.27\" to [project].dependencies and re-run `uv lock` (the container runs `uv sync --frozen`)."
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def info(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- file preparation
def prepare_files(overlay_dir: Path, events_url: str | None, token: str | None) -> list[tuple[str, str, bytes]]:
    """Return ``(local_rel, target_rel, bytes)``; KyoraProcessor values substituted into the config when given."""
    out = []
    for local_rel, target_rel in FILE_MAP:
        p = overlay_dir / local_rel
        if not p.is_file():
            raise SystemExit(f"missing overlay file: {p}")
        data = p.read_bytes()
        if local_rel.endswith("config.kyora.yaml") and (events_url or token):
            text = data.decode("utf-8")
            if events_url:
                text, n = re.subn(r'(?m)^(\s*events_url:\s*)"[^"\n]*"', lambda m: f'{m.group(1)}"{events_url}"', text, count=1)
                if n != 1:
                    raise SystemExit("could not find the KyoraProcessor.events_url line in config.kyora.yaml")
            if token:
                text, n = re.subn(r'(?m)^(\s*token:\s*)"[^"\n]*"', lambda m: f'{m.group(1)}"{token}"', text, count=1)
                if n != 1:
                    raise SystemExit("could not find the KyoraProcessor.token line in config.kyora.yaml")
            data = text.encode("utf-8")
        out.append((local_rel, target_rel, data))
    return out


def print_plan(files: list[tuple[str, str, bytes]], args) -> None:
    mode = "APPLY" if args.apply else "DRY RUN (nothing is uploaded; add --apply)"
    info(f"Kyora overlay for Tokkio 5.0.0-ga - {mode}")
    info(f"Target: service {SERVICE}, volume {VOLUME} (container {CONTAINER}, refresh_policy HotReload), "
         f"namespace {args.namespace}")
    info(f"Configurator: {args.configurator or '(none given)'}")
    info("File map:")
    for local_rel, target_rel, data in files:
        info(f"  {local_rel:28} -> {target_rel:26} ({len(data)} bytes, sha256 {sha256_bytes(data)[:12]}...)")
    ev = args.events_url or "<not set: config KyoraProcessor.events_url stays empty>"
    tk = "set (hidden)" if args.token else "<not set>"
    info(f"  .env additions (fallback, not uploaded by this script): KYORA_EVENTS_URL={ev}  KYORA_HUB_TOKEN={tk}")
    if args.events_url or args.token:
        info(f"  config.yaml: KyoraProcessor.events_url={args.events_url or '(unchanged)'} token={'set' if args.token else '(unchanged)'}")
    else:
        info("  note: without --events-url/--token the processor reads KYORA_EVENTS_URL / KYORA_HUB_TOKEN from the "
             "container environment or /app/.env (see tokkio-kyora/.env.kyora.example)")
    info(PYPROJECT_NOTE)


# ----------------------------------------------------------------------------- HTTP helpers
def http(method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 20.0) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied URL
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b"", dict(e.headers or {})


def multipart(field: str, filename: str, data: bytes, extra_fields: dict | None = None) -> tuple[bytes, str]:
    boundary = f"----KyoraOverlay{uuid.uuid4().hex}"
    buf = bytearray()
    for k, v in (extra_fields or {}).items():
        buf += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
    buf += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    buf += data
    buf += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------------------- OpenAPI discovery
def _param_names(op: dict, path_item: dict) -> list[str]:
    names = []
    for p in list(path_item.get("parameters") or []) + list(op.get("parameters") or []):
        if isinstance(p, dict) and p.get("name"):
            names.append(str(p["name"]).lower())
    names += [m.lower() for m in re.findall(r"{(\w+)}", "")]
    return names


def _multipart_field(op: dict) -> str | None:
    body = ((op.get("requestBody") or {}).get("content") or {})
    mp = body.get("multipart/form-data")
    if not isinstance(mp, dict):
        return None
    props = ((mp.get("schema") or {}).get("properties") or {})
    for name, schema in props.items():
        if isinstance(schema, dict) and (schema.get("format") == "binary" or schema.get("type") == "file"):
            return str(name)
    for name, schema in props.items():
        if isinstance(schema, dict) and schema.get("type") == "array":
            items = schema.get("items") or {}
            if isinstance(items, dict) and items.get("format") == "binary":
                return str(name)
    return next(iter(props), None) or "file"


def find_upload_ops(spec: dict) -> list[dict]:
    """Score every PUT/POST/PATCH operation for 'file upload addressed by service + volume'."""
    cands = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in ("put", "post", "patch"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            text = (path + " " + str(op.get("summary") or "") + " " + str(op.get("operationId") or "")).lower()
            params = _param_names(op, item) + [m.lower() for m in re.findall(r"{(\w+)}", path)]
            score = 0
            why = []
            if "upload" in text:
                score += 3; why.append("upload")
            if "file" in text:
                score += 2; why.append("file")
            if "sync" in text or "asset" in text:
                score += 1; why.append("sync/asset")
            if any("service" in p for p in params):
                score += 2; why.append("service param")
            if any("volume" in p for p in params):
                score += 2; why.append("volume param")
            if any(("path" in p or "file" in p or "name" in p) for p in params):
                score += 1; why.append("path param")
            field = _multipart_field(op)
            if field:
                score += 3; why.append(f"multipart field '{field}'")
            cands.append({"path": path, "method": method.upper(), "score": score, "why": why, "field": field or "file",
                          "params": params, "op": op, "item": item})
    cands.sort(key=lambda c: -c["score"])
    return cands


def find_download_op(spec: dict, upload_path: str) -> dict | None:
    item = (spec.get("paths") or {}).get(upload_path) or {}
    if isinstance(item, dict) and isinstance(item.get("get"), dict):
        return {"path": upload_path, "method": "GET"}
    for path, it in (spec.get("paths") or {}).items():
        if isinstance(it, dict) and isinstance(it.get("get"), dict) and "download" in path.lower() \
                and all(k in path for k in re.findall(r"{(\w+)}", upload_path)):
            return {"path": path, "method": "GET"}
    return None


def fill_path(template: str, target_rel: str) -> tuple[str, dict]:
    """Substitute path params; returns the path and the query params still to add."""
    query = {}

    def sub(m):
        name = m.group(1).lower()
        if "service" in name:
            return urllib.parse.quote(SERVICE, safe="")
        if "volume" in name:
            return urllib.parse.quote(VOLUME, safe="")
        if "path" in name or "file" in name or "name" in name or "key" in name:
            return urllib.parse.quote(target_rel, safe="/")
        return m.group(0)

    filled = re.sub(r"{(\w+)}", sub, template)
    return filled, query


def upload_via_configurator(base: str, files, timeout: float) -> dict:
    """Return ``{target_rel: {"ok": bool, "detail": str, "verified": bool}}`` plus ``"_spec_found"``."""
    results: dict = {"_spec_found": False, "_route": None}
    url = base.rstrip("/") + "/openapi.json"
    try:
        status, body, _ = http("GET", url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        info(f"Configurator: GET {url} failed: {type(exc).__name__}: {exc}")
        return results
    info(f"Configurator: GET {url} -> HTTP {status}")
    if status != 200:
        return results
    try:
        spec = json.loads(body)
    except json.JSONDecodeError as exc:
        info(f"Configurator: /openapi.json is not JSON ({exc}); cannot discover the upload route")
        return results
    results["_spec_found"] = True
    cands = find_upload_ops(spec)
    info(f"Configurator: {len(spec.get('paths') or {})} paths in the spec; upload candidates:")
    for c in cands[:8]:
        info(f"  score {c['score']:2}  {c['method']:5} {c['path']}  [{', '.join(c['why']) or 'no signal'}]")
    if not cands:
        info("  (none)")
    chosen = next((c for c in cands if c["score"] >= 6 and any("volume" in p or "service" in p for p in c["params"])), None)
    if chosen is None:
        info("Configurator: no PUT/POST operation looks like a per-file upload addressed by service/volume; "
             "not guessing. Use --kubectl-fallback or upload via the ACE Configurator VS Code view.")
        return results
    results["_route"] = f"{chosen['method']} {chosen['path']}"
    info(f"Configurator: using {chosen['method']} {chosen['path']} (multipart field '{chosen['field']}')")
    dl = find_download_op(spec, chosen["path"])
    for local_rel, target_rel, data in files:
        path, query = fill_path(chosen["path"], target_rel)
        if "{" in path:
            results[target_rel] = {"ok": False, "verified": False,
                                   "detail": f"unresolved path parameter in {path}; refusing to guess"}
            info(f"  {target_rel}: {results[target_rel]['detail']}")
            continue
        extra = {}
        for p in chosen["params"]:
            if p not in path.lower():
                if "service" in p:
                    extra[p] = SERVICE
                elif "volume" in p:
                    extra[p] = VOLUME
                elif "path" in p or "file" in p or "name" in p:
                    extra[p] = target_rel
        full = base.rstrip("/") + path
        if extra and chosen["op"].get("parameters"):
            qp = {k: v for k, v in extra.items() if any(isinstance(pp, dict) and pp.get("in") == "query" and
                                                        str(pp.get("name", "")).lower() == k for pp in chosen["op"]["parameters"])}
            if qp:
                full += ("&" if "?" in full else "?") + urllib.parse.urlencode(qp)
        body, ctype = multipart(chosen["field"], Path(target_rel).name, data, {k: v for k, v in extra.items()})
        try:
            status, resp, _ = http(chosen["method"], full, body, {"Content-Type": ctype, "Content-Length": str(len(body))}, timeout)
        except Exception as exc:  # noqa: BLE001
            results[target_rel] = {"ok": False, "verified": False, "detail": f"{chosen['method']} {full}: {type(exc).__name__}: {exc}"}
            info(f"  {target_rel}: FAILED {results[target_rel]['detail']}")
            continue
        ok = 200 <= status < 300
        detail = f"{chosen['method']} {full} -> HTTP {status} {resp[:160].decode('utf-8', 'replace').strip()}"
        verified = False
        if ok and dl:
            dpath, _ = fill_path(dl["path"], target_rel)
            try:
                s2, back, _ = http("GET", base.rstrip("/") + dpath, timeout=timeout)
                if s2 == 200 and sha256_bytes(back) == sha256_bytes(data):
                    verified = True
                    detail += "; verified by read-back (sha256 match)"
                else:
                    detail += f"; read-back GET {dpath} -> HTTP {s2}, sha256 {'match' if sha256_bytes(back) == sha256_bytes(data) else 'MISMATCH'}"
            except Exception as exc:  # noqa: BLE001
                detail += f"; read-back failed: {exc}"
        results[target_rel] = {"ok": ok, "verified": verified, "detail": detail}
        info(f"  {target_rel}: {'OK' if ok else 'FAILED'} {detail}")
    return results


# ----------------------------------------------------------------------------- kubectl fallback
def run(cmd: list[str], timeout: float = 120.0, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def find_pod(ns: str) -> str | None:
    code, out, err = run(["kubectl", "-n", ns, "get", "pods", "-l", "app=ace-controller", "-o", "name"])
    info(f"kubectl -n {ns} get pods -l app=ace-controller -o name -> exit {code}: {out.strip() or err.strip() or '(empty)'}")
    pods = [l.strip() for l in out.splitlines() if l.strip()]
    if code == 0 and pods:
        return pods[0]
    code, out, err = run(["kubectl", "-n", ns, "get", "pods", "-o", "name"])
    if code != 0:
        info(f"kubectl -n {ns} get pods -o name -> exit {code}: {err.strip()}")
        return None
    pods = [l.strip() for l in out.splitlines() if SERVICE in l]
    info(f"kubectl -n {ns} get pods -o name | grep {SERVICE} -> {pods or '(none)'}")
    return pods[0] if pods else None


def upload_via_kubectl(ns: str, files, timeout: float) -> dict:
    results: dict = {}
    if not shutil.which("kubectl"):
        info("kubectl fallback: kubectl not found on PATH; nothing copied")
        return results
    pod = find_pod(ns)
    if not pod:
        info("kubectl fallback: no ace-controller pod found; nothing copied")
        return results
    pod_name = pod.split("/", 1)[-1]
    check = f"test -f {APP_ROOT}/configs/config.yaml && test -f {APP_ROOT}/src/bot.py && echo KYORA_LAYOUT_OK"
    code, out, err = run(["kubectl", "-n", ns, "exec", pod_name, "-c", CONTAINER, "--", "sh", "-c", check], timeout)
    info(f"kubectl exec {pod_name} -c {CONTAINER} -- sh -c '{check}' -> exit {code}: {out.strip() or err.strip()}")
    if code != 0 or "KYORA_LAYOUT_OK" not in out:
        info(f"kubectl fallback: {APP_ROOT}/configs/config.yaml or {APP_ROOT}/src/bot.py not found in the container; "
             "the app-storage-volume mount path differs from the Dockerfile WORKDIR assumption. Not guessing.")
        return results
    tmpdir = Path(os.environ.get("TMPDIR") or "/tmp") / f"kyora-overlay-{uuid.uuid4().hex[:8]}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        for local_rel, target_rel, data in files:
            local = tmpdir / Path(target_rel).name
            local.write_bytes(data)
            dest = f"{pod_name}:{APP_ROOT}/{target_rel}"
            code, out, err = run(["kubectl", "-n", ns, "cp", str(local), dest, "-c", CONTAINER], timeout)
            detail = f"kubectl cp -> {dest} exit {code} {err.strip()}"
            ok = code == 0
            verified = False
            if ok:
                c2, o2, e2 = run(["kubectl", "-n", ns, "exec", pod_name, "-c", CONTAINER, "--", "sha256sum", f"{APP_ROOT}/{target_rel}"], timeout)
                remote = o2.split()[0] if c2 == 0 and o2.split() else ""
                verified = remote == sha256_bytes(data)
                detail += f"; sha256sum in pod {'matches' if verified else 'does NOT match: ' + (remote or e2.strip())}"
            results[target_rel] = {"ok": ok, "verified": verified, "detail": detail}
            info(f"  {target_rel}: {'OK' if ok else 'FAILED'} {detail}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upload the Kyora overlay into a Tokkio ACE controller (dry run by default).")
    ap.add_argument("--configurator", help="ACE Configurator API base URL, e.g. http://<node-ip>:30180")
    ap.add_argument("--apply", action="store_true", help="actually upload (default is a dry run)")
    ap.add_argument("--kubectl-fallback", action="store_true", help="fall back to kubectl cp into the ace-controller pod")
    ap.add_argument("--namespace", default="app", help="kubernetes namespace of the Tokkio app (default app)")
    ap.add_argument("--events-url", help="Kyora base URL reachable from the cluster; written into config.yaml KyoraProcessor.events_url")
    ap.add_argument("--token", help="Kyora hub token; written into config.yaml KyoraProcessor.token")
    ap.add_argument("--overlay-dir", default=str(OVERLAY_DIR), help=argparse.SUPPRESS)
    ap.add_argument("--timeout", type=float, default=20.0, help="HTTP / kubectl timeout in seconds")
    args = ap.parse_args(argv)

    files = prepare_files(Path(args.overlay_dir), args.events_url, args.token)
    print_plan(files, args)
    if not args.apply:
        info("Dry run complete. Nothing was uploaded.")
        return 0

    results: dict = {}
    if args.configurator:
        results = upload_via_configurator(args.configurator, files, args.timeout)
    else:
        info("Configurator: no --configurator given; skipping OpenAPI discovery")
    targets = [t for _, t, _ in files]
    done = {t for t in targets if results.get(t, {}).get("ok")}
    if len(done) < len(targets):
        if args.kubectl_fallback:
            info("Falling back to kubectl (" + ("no OpenAPI document" if not results.get("_spec_found") else
                                              "no usable upload route" if not results.get("_route") else "upload incomplete") + ")")
            pending = [f for f in files if f[1] not in done]
            kres = upload_via_kubectl(args.namespace, pending, args.timeout)
            for t, r in kres.items():
                results[t] = r
        else:
            info("Not falling back to kubectl (pass --kubectl-fallback to allow `kubectl cp` into the pod).")

    verified = [t for t in targets if results.get(t, {}).get("ok") and results[t].get("verified")]
    uploaded_unverified = [t for t in targets if results.get(t, {}).get("ok") and not results[t].get("verified")]
    failed = [t for t in targets if t in results and not results[t].get("ok")]
    not_done = [t for t in targets if t not in results]
    info("Summary:")
    info(f"  uploaded and verified (2xx/exit 0 + sha256 read-back): {verified or 'none'}")
    info(f"  uploaded, not read back (2xx/exit 0 only):             {uploaded_unverified or 'none'}")
    info(f"  failed:                                                 {failed or 'none'}")
    info(f"  not attempted:                                          {not_done or 'none'}")
    info("  not verified by this script: that uvicorn reloaded and the pipeline started with the overlay. Check:")
    info(f"    kubectl -n {args.namespace} logs deploy/{SERVICE} -c {CONTAINER} | grep -E 'Kyora overlay|KyoraProcessor'")
    all_ok = not failed and not not_done and len(verified) + len(uploaded_unverified) == len(targets)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
