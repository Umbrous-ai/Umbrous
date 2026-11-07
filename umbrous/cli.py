"""Command line: `bin/umbrous [command]`.

    bin/umbrous                 start the runtime and open the app (same as `serve`)
    bin/umbrous serve --no-open start the runtime and print the one-time URL instead
    bin/umbrous test            run the test suite
    bin/umbrous purge logs      delete the runtime's log files (asks for --yes)
    bin/umbrous models          list installed Ollama models, probe results and the exposure check
    bin/umbrous probe MODEL     run the tool-call probe on one model (or --all)
    bin/umbrous prototype       open the static prototype file, as before M0 (handled in bin/umbrous)
    bin/umbrous version
"""

from __future__ import annotations

import argparse
import json
import logging
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

    def _check_ollama_exposure() -> None:
        report = runtime.refresh_exposure()
        level = logging.WARNING if report.status == "exposed" else logging.INFO
        log_event(log, "ollama.exposure", level=level, **report.as_dict())
        prefix = "WARNING: " if report.status == "exposed" else ""
        print(f"{prefix}Ollama exposure check: {report.summary}. {report.limits}")
        if report.status == "exposed":
            print("  Anyone on those networks can use your models. Unset OLLAMA_HOST "
                  "(launchctl unsetenv OLLAMA_HOST) and restart Ollama to keep it on loopback.")
        sys.stdout.flush()

    threading.Thread(target=_check_ollama_exposure, daemon=True).start()

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



def models_command(config: Config, command: str, names: List[str], probe_all: bool) -> int:
    from umbrous.model.base import Deadlines, ModelError
    from umbrous.model.exposure import check_exposure
    from umbrous.model.ollama import OllamaAdapter
    from umbrous.model.router import ModelRouter
    from umbrous.persistence import db

    make_private_dir(config.home)
    db.connect(config.db_path).close()  # make sure the schema is current
    adapter = OllamaAdapter(config.ollama_url, Deadlines(first_token=config.first_token_timeout_s))
    router = ModelRouter(adapter, config.db_path)
    status = router.status()
    if not status["reachable"]:
        print(status["error"], file=sys.stderr)
        return 1
    if command == "models":
        print(f"Ollama {status['version']} at {status['url']}")
        report = check_exposure(adapter.port)
        print(f"Exposure: {report.summary}. {report.limits}")
        if report.ollama_host:
            print(f"OLLAMA_HOST is set to {report.ollama_host!r}")
        if not status["models"]:
            print("No models installed. Try: ollama pull qwen2.5:3b")
        for m in status["models"]:
            probe = m["probe"]
            label = ("tool calls verified" if probe and probe["passed"] else
                     f"plain chat only ({probe['detail']})" if probe else "not probed yet")
            ctx = f"ctx {m['context_length']}" if m["context_length"] else "ctx ?"
            print(f"  {m['name']:<32} {m['parameter_size']:>6} {ctx:<12} {label}")
        return 0
    targets = [m["name"] for m in status["models"]] if probe_all else names
    if not targets:
        print("Name a model to probe, or use --all.", file=sys.stderr)
        return 2
    failed = 0
    for name in targets:
        print(f"Probing {name} (loading the model can take a while)…", flush=True)
        try:
            result, _ = router.probe(name)
        except ModelError as exc:
            print(f"  error: {exc.message}")
            failed += 1
            continue
        print(f"  {'PASS' if result.passed else 'FAIL'} at stage '{result.stage}' in {result.ms / 1000:.1f}s: {result.detail}")
        failed += 0 if result.passed else 1
    return 0 if failed == 0 else 1


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
    sub.add_parser("models", help="list Ollama models, probe results and the exposure check")
    p_probe = sub.add_parser("probe", help="run the tool-call probe")
    p_probe.add_argument("model", nargs="*", help="model name(s)")
    p_probe.add_argument("--all", action="store_true", help="probe every installed model")

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
    if command in ("models", "probe"):
        return models_command(config, command, getattr(args, "model", []), getattr(args, "all", False))
    open_browser = config.open_browser and not (args.command == "serve" and args.no_open)
    return serve(config, open_browser)
