"""Kyora command line: ``serve``, ``hub``, ``service``, ``selfcheck``, ``bench``, ``version``."""
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

# seconds between the iSH listener probes (iOS may reclaim the listening socket of a paused app)
ISH_LISTENER_WATCH_S = 3.0


def _build_app(args, hub_mode: bool = False):
    from .app import App
    from .config import Config
    cfg = Config.load(getattr(args, "config", None))
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    return App(cfg, home=home, hub_mode=hub_mode)


def _claim_home(app):
    """Take the KYORA_HOME instance lock before app.start(); None (message printed) when another Kyora holds it."""
    from .util.instance_lock import InstanceLock
    lock = InstanceLock(app.home)
    if lock.acquire():
        return lock
    pid = lock.holder_pid()
    print(f"Another Kyora is already running with this home ({app.home}{', pid ' + pid if pid else ''}).\n"
          "Stop it first (if it is the background service: kyora service stop), or start this one with a "
          "different --home.", file=sys.stderr)
    app.stop()
    return None


def _watch_stop_file(server, path: str) -> None:
    """Under the background service: shut the server down cleanly once the service's stop file appears."""
    started = time.time()

    def loop() -> None:
        stop_file = Path(path)
        while True:
            try:
                if stop_file.stat().st_mtime >= started - 1.0:
                    print("Stopping (kyora service stop) ...", flush=True)
                    server.shutdown()
                    return
            except OSError:
                pass
            time.sleep(1.0)

    threading.Thread(target=loop, name="kyora-service-stop", daemon=True).start()


def cmd_serve(args, hub_mode: bool = False) -> int:
    from .server import KyoraServer, Router
    app = _build_app(args, hub_mode=hub_mode)
    lock = _claim_home(app)
    if lock is None:
        return 2
    host = args.host or ("0.0.0.0" if hub_mode else app.config.server.host)
    port = args.port if args.port is not None else app.config.server.port
    on_ish = (getattr(app, "platform", None) or {}).get("kind") == "ish"
    router = Router()
    app.register_routes(router)
    try:
        server = KyoraServer(app, host, port, router, preview_port=getattr(args, "preview_port", None),
                             watch_s=ISH_LISTENER_WATCH_S if on_ish else None)
    except OSError as exc:
        # Before app.start(): a second Kyora must not touch the first one's runs.
        print(f"Kyora cannot listen on {host}:{port} (or its preview port): {exc}\n"
              "Another Kyora or another program is using that port. Stop it, or start with --port N "
              "(and --preview-port M).", file=sys.stderr)
        app.stop()
        lock.release()
        return 2
    app.start(server.url, server.preview_url)
    print(f"Kyora {__version__} - {app.platform['kind']} - config: {app.config.config_dir}")
    print(f"Open:  {server.url}")
    print(f"Games: {server.preview_url}   (separate origin for game previews and browser tests)")
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
    keeper = _ish_background(server.url) if on_ish else None
    from .service import ENV_SERVICE, ENV_STOP_FILE
    if os.environ.get(ENV_STOP_FILE):
        _watch_stop_file(server, os.environ[ENV_STOP_FILE])
    if not os.environ.get(ENV_SERVICE):
        print("Kyora runs while this terminal stays open. To keep it running after you close the terminal, "
              "and start it again by itself, run: kyora service install", flush=True)
    if getattr(args, "open", False):
        threading.Timer(0.8, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if keeper is not None:
            keeper.stop()
        server.shutdown()
        app.stop()
        lock.release()
    return 0


def _ish_background(url: str):
    """On iSH: keep iSH running while Safari is in front (unless turned off) and say what to open."""
    from .util import ish
    keeper = None if ish.background_disabled() else ish.BackgroundKeeper(ish.LOCATION_DEVICE)
    if keeper is not None:
        keeper.start()
    for line in ish.serve_notes(url, keeper):
        print(line, flush=True)
    if keeper is not None and keeper.state == "waiting":
        def hint() -> None:
            note = ish.no_update_note(keeper)
            if note:
                print(note, flush=True)
        timer = threading.Timer(ish.NO_UPDATE_HINT_S, hint)
        timer.daemon = True
        timer.start()
    return keeper


def _service_home(args) -> Path:
    from .util.fs import kyora_home
    return Path(args.home).expanduser().resolve() if getattr(args, "home", None) else kyora_home()


def _print_status(st: dict) -> None:
    if not st["running"]:
        print(f"Kyora service: not running (home {st['home']}).")
    else:
        server = f"server pid {st['server_pid']}" if st.get("server_pid") else "server starting or restarting"
        print(f"Kyora service: running since {st.get('since') or '?'} (supervisor pid {st['supervisor_pid']}, "
              f"{server}, restarts {st.get('restarts', 0)}).")
        print(f"  {st['url'] or '(address not known yet)'} - "
              + ("answering" if st["answering"] else "not answering yet (iSH: the first start takes a while)"))
    inst = st.get("installed") or {}
    if inst:
        how = {"task": "Task Scheduler task 'Kyora' at logon", "runkey": "the Run key at logon",
               "profile": f"login shells ({inst.get('target')})"}.get(inst.get("method"), inst.get("method"))
        print(f"  starts by itself: {how}")
    else:
        print("  starts by itself: no (kyora service install sets that up)")
    if st.get("last_exit") is not None:
        print(f"  last server exit code: {st['last_exit']}")
    print(f"  log: {st['log']}")


def cmd_service(args) -> int:
    from . import service
    home = _service_home(args)
    act = args.action
    if act == "run":
        return service.Supervisor(home).run()
    if act in ("install", "start", "restart"):
        serve_args: list[str] | None = None
        if any(v is not None for v in (args.host, args.port, args.preview_port)):
            serve_args = []
            for flag, value in (("--host", args.host), ("--port", args.port), ("--preview-port", args.preview_port)):
                if value is not None:
                    serve_args += [flag, str(value)]
        from .config import Config
        spec_before = service._read_json(service.Paths(home).spec)
        # The config folder is chosen by install (or an explicit --config), never by an autostart: that runs
        # from another working directory (a login shell, Task Scheduler) and could resolve another folder.
        if args.config:
            config_dir = Path(args.config).expanduser().resolve()
        elif act == "install" or not spec_before.get("config"):
            config_dir = Config.resolve_dir(None).resolve()
        else:
            config_dir = None
        spec = service.write_spec(home, args.mode, serve_args, config_dir)
        if act == "install":
            try:
                info = service.install(home, method=args.method)
            except RuntimeError as exc:
                print(f"Could not set up the autostart: {exc}", file=sys.stderr)
                return 1
            for note in info.get("notes") or []:
                print(f"Note: {note}; using the Run key instead.")
            if not args.quiet:
                print({"task": "Kyora now starts at every logon (Task Scheduler task 'Kyora', restarted if it fails).",
                       "runkey": "Kyora now starts at every logon (Run key).",
                       "profile": f"Kyora now starts whenever a login shell starts ({info.get('target')}); "
                                  "on iSH that is whenever the iSH app opens."}[info["method"]])
        def what(sp: dict) -> tuple:
            return sp.get("mode"), sp.get("args"), sp.get("config")
        changed = bool(spec_before) and what(spec_before) != what(spec)
        if act == "restart" or (changed and service.supervisor_running(home)[0]):
            service.stop(home)  # a running service picks up a new mode, port or config folder
        try:
            st = service.start(home, wait_s=args.wait if args.wait is not None else 90.0)
        except RuntimeError as exc:
            print(f"Could not start the Kyora service: {exc}", file=sys.stderr)
            return 1
        if not args.quiet:
            _print_status(st)
            print(f"  started: {st.get('started_by')}")
            print("It keeps running after you close this terminal. Stop it: kyora service stop; "
                  "remove the autostart: kyora service uninstall.")
        if args.open and st.get("url") and st.get("answering"):
            webbrowser.open(st["url"])
        return 0 if st["running"] else 1
    if act == "stop":
        res = service.stop(home)
        if not args.quiet:
            print("Kyora service stopped." + (" (ended by force)" if res.get("forced") else "")
                  if res.get("stopped") else "Kyora service was not running.")
            if service._read_json(service.Paths(home).install):
                print("It starts again at the next logon / login shell; kyora service uninstall removes that.")
        return 0
    if act == "uninstall":
        res = service.uninstall(home)
        if not args.quiet:
            print("Kyora service " + ("stopped" if res["stopped"] else "was not running")
                  + "; removed: " + (", ".join(res["removed"]) or "nothing to remove"))
        return 0
    if act == "status":
        st = service.status(home)
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            _print_status(st)
        return 0 if st["running"] else 3
    if act == "logs":
        text = service.tail(home, args.lines)
        print(text or f"(no service log yet: {service.Paths(home).log})")
        return 0
    return 2


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
        answer = input("This runs the full pipeline per task and spends NVIDIA and OpenRouter tokens and Claude usage. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    lock = _claim_home(app)
    if lock is None:
        return 2
    import threading as _t
    cancel = _t.Event()
    box: dict = {}
    worker = _t.Thread(target=lambda: box.update(result=app.bench.run(ids, confirm=True, cancel=cancel)),
                       name="kyora-bench", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        # Ctrl-C cancels the current pipeline run (working tree rolled back) instead of killing it mid-write.
        print("\ncancelling the bench (current run is rolled back) ...", flush=True)
        cancel.set()
        worker.join()
    result = box.get("result")
    print(json.dumps(result if isinstance(result, dict) else getattr(result, "to_dict", lambda: str(result))(), indent=2))
    app.stop()
    lock.release()
    return 0 if not cancel.is_set() else 130


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
    p_serve.add_argument("--preview-port", type=int,
                         help="port of the game preview listener (default: settings server.preview_port, else port + 1)")
    p_serve.add_argument("--open", action="store_true", help="open the UI in a browser")
    p_serve.add_argument("--hub", action="store_true", help="also expose the code engine on the LAN (hub mode)")

    p_hub = sub.add_parser("hub", help="run as a Hub: serve + expose the code engine on the LAN")
    common(p_hub)
    p_hub.add_argument("--host")
    p_hub.add_argument("--port", type=int)
    p_hub.add_argument("--preview-port", type=int,
                       help="port of the game preview listener (default: settings server.preview_port, else port + 1)")
    p_hub.add_argument("--open", action="store_true")

    p_svc = sub.add_parser("service", help="keep Kyora running in the background: it survives closing the "
                                           "terminal, restarts when it stops, and starts at logon / when iSH opens")
    p_svc.add_argument("action", choices=["install", "start", "stop", "restart", "status", "logs", "uninstall", "run"],
                       help="install: autostart + start now; start/stop/restart: now only; status; logs; "
                            "uninstall: stop + remove the autostart; run: the supervisor itself (used by the others)")
    p_svc.add_argument("mode", nargs="?", choices=["serve", "hub"], default=None,
                       help="what the service runs (default: as before, else serve)")
    common(p_svc)
    p_svc.add_argument("--host")
    p_svc.add_argument("--port", type=int)
    p_svc.add_argument("--preview-port", type=int)
    p_svc.add_argument("--method", choices=["auto", "task", "runkey"], default="auto",
                       help="Windows autostart: Task Scheduler (task), the Run key (runkey), or task then runkey (auto)")
    p_svc.add_argument("--open", action="store_true", help="open the UI in a browser once it answers")
    p_svc.add_argument("--wait", type=float, default=None, help="seconds to wait until the server answers (default 90)")
    p_svc.add_argument("--quiet", action="store_true")
    p_svc.add_argument("--json", action="store_true", help="status as JSON")
    p_svc.add_argument("--lines", type=int, default=60, help="logs: how many lines")

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
    if args.cmd == "service":
        return cmd_service(args)
    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)
    if args.cmd == "bench":
        return cmd_bench(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
