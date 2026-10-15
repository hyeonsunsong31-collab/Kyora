"""Kyora command line: ``serve``, ``hub``, ``selfcheck``, ``bench``, ``version``."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__


def _build_app(args, hub_mode: bool = False):
    from .app import App
    from .config import Config
    cfg = Config.load(getattr(args, "config", None))
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    return App(cfg, home=home, hub_mode=hub_mode)


def cmd_serve(args, hub_mode: bool = False) -> int:
    from .server import KyoraServer, Router
    app = _build_app(args, hub_mode=hub_mode)
    host = args.host or ("0.0.0.0" if hub_mode else app.config.server.host)
    port = args.port if args.port is not None else app.config.server.port
    router = Router()
    app.register_routes(router)
    server = KyoraServer(app, host, port, router)
    app.start(server.url)
    print(f"Kyora {__version__} - {app.platform['kind']} - config: {app.config.config_dir}")
    print(f"Open:  {server.url}")
    if hub_mode:
        from .server import lan_addresses
        token = app.config.hub.token
        for ip in lan_addresses():
            print(f"Hub:   http://{ip}:{server.port}   token: {token}")
        from .hub import link_payload
        ip = (lan_addresses() or ["127.0.0.1"])[0]
        # The phone opens this link in its OWN Kyora (127.0.0.1:8765), which stores hub.url/token.
        link = link_payload(f"http://{ip}:{server.port}", token)
        print(f"Link:  {link}   (open on the phone while its own Kyora is running)")
        try:
            from .util.qr import qr_ansi
            print(qr_ansi(link))
        except Exception as exc:  # noqa: BLE001
            print(f"(QR unavailable: {exc})")
    if app.missing:
        print("Unavailable components: " + ", ".join(f"{k} ({v})" for k, v in app.missing.items()))
    if getattr(args, "open", False):
        threading.Timer(0.8, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()
        app.stop()
    return 0


def cmd_selfcheck(args) -> int:
    app = _build_app(args)
    if app.selfcheck is None:
        print("selfcheck component unavailable: " + app.missing.get("selfcheck", "unknown"))
        return 2
    report = app.selfcheck.run(probe_models=not args.no_probe, probe_claude=args.probe_claude)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report.get("items", []):
            mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}.get(item.get("status"), "?   ")
            print(f"[{mark}] {item.get('label')}: {item.get('detail')}")
        if report.get("lan_urls"):
            print("LAN URLs: " + ", ".join(report["lan_urls"]))
    app.stop()
    return 0 if all(i.get("status") != "fail" for i in report.get("items", [])) else 1


def cmd_bench(args) -> int:
    app = _build_app(args)
    if app.bench is None:
        print("bench component unavailable: " + app.missing.get("bench", "unknown"))
        return 2
    if args.list:
        for t in app.bench.list_tasks():
            print(f"{t['id']:16} {t.get('category',''):10} {t.get('title','')}")
        return 0
    ids = [x for x in (args.tasks or "").split(",") if x] or None
    est = app.bench.estimate(ids)
    print(json.dumps(est, indent=2))
    if args.dry_run:
        return 0
    if not args.yes:
        answer = input("This runs the full pipeline per task and spends NVIDIA tokens and Claude usage. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    import threading as _t
    result = app.bench.run(ids, confirm=True, cancel=_t.Event())
    print(json.dumps(result if isinstance(result, dict) else getattr(result, "to_dict", lambda: str(result))(), indent=2))
    app.stop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kyora", description="Kyora - AI game-development studio")
    parser.add_argument("--version", action="version", version=f"kyora {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", help="config directory containing keys.json and settings.json")
        p.add_argument("--home", help="runtime data directory (default $KYORA_HOME or ~/.kyora)")
        p.add_argument("-v", "--verbose", action="store_true")

    p_serve = sub.add_parser("serve", help="run the local Kyora server and UI")
    common(p_serve)
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--open", action="store_true", help="open the UI in a browser")
    p_serve.add_argument("--hub", action="store_true", help="also expose the code engine on the LAN (hub mode)")

    p_hub = sub.add_parser("hub", help="run as a Hub: serve + expose the code engine on the LAN")
    common(p_hub)
    p_hub.add_argument("--host")
    p_hub.add_argument("--port", type=int)
    p_hub.add_argument("--open", action="store_true")

    p_check = sub.add_parser("selfcheck", help="check models, engines, tools and Tokkio reachability")
    common(p_check)
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--no-probe", action="store_true", help="skip live model probes")
    p_check.add_argument("--probe-claude", action="store_true", help="run a 1-turn Claude Code probe to resolve the model")

    p_bench = sub.add_parser("bench", help="run Kyora Bench (real pipeline runs; costs tokens)")
    common(p_bench)
    p_bench.add_argument("--list", action="store_true")
    p_bench.add_argument("--tasks", help="comma-separated task ids (default: all)")
    p_bench.add_argument("--yes", action="store_true")
    p_bench.add_argument("--dry-run", action="store_true")

    sub.add_parser("version", help="print the version")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd in (None, "version"):
        if args.cmd is None:
            parser.print_help()
        print(f"kyora {__version__}")
        return 0
    if args.cmd == "serve":
        return cmd_serve(args, hub_mode=args.hub)
    if args.cmd == "hub":
        return cmd_serve(args, hub_mode=True)
    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)
    if args.cmd == "bench":
        return cmd_bench(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
