"""Command line: `bin/umbrous [command]`.

    bin/umbrous                 start the runtime and open the app (same as `serve`)
    bin/umbrous serve --no-open start the runtime and print the one-time URL instead
    bin/umbrous test            run the test suite
    bin/umbrous purge logs      delete the runtime's log files (asks for --yes)
    bin/umbrous prototype       open the static prototype file, as before M0 (handled in bin/umbrous)
    bin/umbrous version
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import unittest
from pathlib import Path
from typing import List, Optional

from umbrous import MILESTONE, __version__
from umbrous.config import Config, ConfigError, load_config, make_private_dir

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_private_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def serve(config: Config, open_browser: bool) -> int:
    from umbrous.api.server import make_server
    from umbrous.browser import open_page
    from umbrous.logging_setup import log_event, setup_logging
    from umbrous.persistence import db
    from umbrous.receipts.redaction import default_redactor

    make_private_dir(config.home)
    make_private_dir(config.run_dir)
    log = setup_logging(config).getChild("runtime")

    runtime_file = config.run_dir / "runtime.json"
    if runtime_file.exists():
        try:
            other = json.loads(runtime_file.read_text())
            if _pid_alive(int(other["pid"])) and int(other["pid"]) != os.getpid():
                print(f"Umbrous is already running (pid {other['pid']}, {other['origin']}). "
                      "Stop it first, or use --home for a separate instance.", file=sys.stderr)
                return 1
        except (ValueError, KeyError, OSError):
            pass  # stale or unreadable: overwrite below

    conn = db.connect(config.db_path)
    launch_id = db.record_launch(conn, __version__)
    try:
        server, runtime = make_server(config, db.schema_version(conn), db.launch_count(conn))
    except OSError as exc:
        db.record_stop(conn, launch_id)
        print(f"Cannot listen on {config.host}:{config.port}: {exc.strerror}. "
              "Is another program using that port? Try --port.", file=sys.stderr)
        return 1

    policy = runtime.policy
    default_redactor.register(policy.session_token)
    default_redactor.register(policy.launch_code)
    _write_private_json(runtime_file, {"pid": os.getpid(), "origin": policy.origin, "version": __version__})

    log_event(log, "runtime.started", version=__version__, milestone=MILESTONE, bind=f"127.0.0.1:{policy.port}",
              schema_version=runtime.schema_version, home=str(config.home))
    launch_url = f"{policy.origin}/#launch={policy.launch_code}"
    print(f"Umbrous {__version__} ({MILESTONE}) listening on {policy.origin} (loopback only). Ctrl-C stops it.")
    if open_browser:
        print(f"Opened in {open_page(launch_url, config.browser_profile_dir)}.")
    else:
        print("Open this URL within 2 minutes. It works once:")
        print(f"  {launch_url}")
    sys.stdout.flush()

    def _stop(signum, frame):  # Ctrl-C (SIGINT) and SIGTERM both stop cleanly
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.record_stop(conn, launch_id)
        conn.close()
        try:
            runtime_file.unlink()
        except FileNotFoundError:
            pass
        log_event(log, "runtime.stopped", uptime_s=runtime.health()["uptime_s"])
        print("Umbrous stopped.")
    return 0


def run_tests(verbosity: int = 2) -> int:
    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def purge_logs(config: Config, yes: bool) -> int:
    files = sorted(config.log_dir.glob("runtime.log*")) if config.log_dir.exists() else []
    if not files:
        print("No log files to purge.")
        return 0
    for f in files:
        print(f"  {f}")
    if not yes:
        print(f"{len(files)} file(s) would be deleted. Run again with --yes to delete them.")
        return 1
    for f in files:
        f.unlink()
    print(f"Deleted {len(files)} log file(s).")
    return 0



def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="umbrous", description="Umbrous local agent harness runtime")
    parser.add_argument("--home", type=Path, help="data directory (default: ~/Library/Application Support/Umbrous)")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="start the runtime (default)")
    p_serve.add_argument("--port", type=int, help="loopback port (default 8417)")
    p_serve.add_argument("--no-open", action="store_true", help="print the one-time URL instead of opening a browser")
    p_serve.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub.add_parser("test", help="run the test suite")
    p_purge = sub.add_parser("purge", help="delete stored records")
    p_purge.add_argument("what", choices=["logs"], help="what to purge (receipts arrive in M3)")
    p_purge.add_argument("--yes", action="store_true", help="really delete")
    sub.add_parser("version")

    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command == "version":
        print(f"umbrous {__version__} ({MILESTONE})")
        return 0
    if command == "test":
        return run_tests()

    try:
        overrides = {}
        if command == "serve" and args.command:
            overrides = {"port": args.port, "log_level": args.log_level}
        config = load_config(args.home, overrides)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if command == "purge":
        return purge_logs(config, args.yes)
    open_browser = config.open_browser and not (args.command == "serve" and args.no_open)
    return serve(config, open_browser)
