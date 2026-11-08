"""Command line: `bin/umbrous [command]`.

    bin/umbrous                 start the runtime and open the app (same as `serve`)
    bin/umbrous serve --no-open start the runtime and print the one-time URL instead
    bin/umbrous serve --desktop how the desktop app starts it: JSON lines on stdout, commands on stdin
    bin/umbrous test            run the test suite
    bin/umbrous purge logs      delete the runtime's log files (asks for --yes)
    bin/umbrous models          list available models, probe results and the local exposure check
    bin/umbrous probe MODEL     run the tool-call probe on one model (or --all)
    bin/umbrous workspace [PROJECT [FOLDER]] [--profile P] [--detach]
                                show or set which folder a project may read (M3)
    bin/umbrous receipts        recent tool receipts (--chat ID, --task ID for full records)
    bin/umbrous purge receipts --all|--before DATE   delete receipts (asks for --yes)
    bin/umbrous tasks           recent tasks (--chat ID; --task ID for the trace and report)
    bin/umbrous purge tasks --all|--before DATE      delete task state (asks for --yes)
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

from umbrous.commands import (_find_project, _stores, memory_command, models_command,
                              purge_receipts, purge_tasks, receipts_command,
                              tasks_command, workspace_command)
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


def serve(config: Config, open_browser: bool, desktop: bool = False) -> int:
    """Run until Ctrl-C / SIGTERM. With `desktop`, speak the desktop app's
    protocol instead (see umbrous/desktop.py): JSON lines on stdout, commands on
    stdin, and stop when stdin closes."""
    from umbrous.api.server import make_server
    from umbrous.browser import open_page
    from umbrous.logging_setup import log_event, setup_logging
    from umbrous.persistence import db
    from umbrous.receipts.redaction import default_redactor
    from umbrous import desktop as desk

    def say(text: str) -> None:  # human-readable output; stdout belongs to the protocol in desktop mode
        print(text, file=sys.stderr if desktop else sys.stdout)

    def fail(code: str, text: str, rc: int, **fields) -> int:
        print(text, file=sys.stderr)
        if desktop:
            desk.emit("error", code=code, message=text, **fields)
        return rc

    make_private_dir(config.home)
    make_private_dir(config.run_dir)
    log = setup_logging(config).getChild("runtime")

    runtime_file = config.run_dir / "runtime.json"
    if runtime_file.exists():
        try:
            other = json.loads(runtime_file.read_text())
            if _pid_alive(int(other["pid"])) and int(other["pid"]) != os.getpid():
                where = "the desktop app" if other.get("mode") == "desktop" else "a terminal"
                return fail("already_running",
                            f"Umbrous is already running (pid {other['pid']}, {other['origin']}, started from {where}). "
                            "Stop it first, or use --home for a separate instance.", 1,
                            pid=int(other["pid"]), origin=str(other.get("origin", "")),
                            mode=str(other.get("mode", "cli")))
        except (ValueError, KeyError, OSError, TypeError):
            pass  # stale or unreadable: overwrite below

    try:
        conn = db.connect(config.db_path)
    except Exception as exc:  # SchemaError, sqlite3 errors, permissions
        return fail("database_error", f"Cannot open the database at {config.db_path}: {exc}", 1)
    launch_id = db.record_launch(conn, __version__)
    try:
        server, runtime = make_server(config, db.schema_version(conn), db.launch_count(conn))
    except OSError as exc:
        db.record_stop(conn, launch_id)
        conn.close()
        return fail("port_in_use", f"Cannot listen on {config.host}:{config.port}: {exc.strerror}. "
                    "Is another program using that port? Try --port.", 1, port=config.port)

    policy = runtime.policy
    default_redactor.register(policy.session_token)
    default_redactor.register(policy.launch_code)
    _write_private_json(runtime_file, {"pid": os.getpid(), "origin": policy.origin, "version": __version__,
                                       "mode": "desktop" if desktop else "cli"})

    log_event(log, "runtime.started", version=__version__, milestone=MILESTONE, bind=f"127.0.0.1:{policy.port}",
              schema_version=runtime.schema_version, home=str(config.home), mode="desktop" if desktop else "cli")
    launch_url = f"{policy.origin}/#launch={policy.launch_code}"
    say(f"Umbrous {__version__} ({MILESTONE}) listening on {policy.origin} (loopback only). "
        + ("Closing stdin stops it." if desktop else "Ctrl-C stops it."))
    if desktop:
        desk.emit("ready", origin=policy.origin, launch_code=policy.launch_code, pid=os.getpid(),
                  version=__version__, milestone=MILESTONE, home=str(config.home))
    elif open_browser:
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
        say(f"{prefix}Ollama exposure check: {report.summary}. {report.limits}")
        if report.status == "exposed":
            say("  Anyone on those networks can use your models. Unset OLLAMA_HOST "
                "(launchctl unsetenv OLLAMA_HOST) and restart Ollama to keep it on loopback.")
        sys.stdout.flush()

    threading.Thread(target=_check_ollama_exposure, daemon=True).start()

    def _stop(signum, frame):  # Ctrl-C (SIGINT) and SIGTERM both stop cleanly
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if desktop:
        def _reissue() -> str:
            code = policy.reissue_launch_code()
            default_redactor.register(code)
            log_event(log, "desktop.launch_code_reissued")
            return code

        desk.watch_stdin(on_stop=lambda: _stop(None, None), on_reissue=_reissue)
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
        log_event(log, "runtime.stopped", uptime_s=int(runtime._clock() - runtime._started_mono))
        say("Umbrous stopped.")
        if desktop:
            try:
                desk.emit("stopped")
            except (OSError, ValueError):
                pass  # the app is gone; nobody to tell
    return 0


def run_tests(verbosity: int = 2) -> int:
    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def purge_logs(config: Config, yes: bool) -> int:
    files = sorted([*config.log_dir.glob("runtime.log*"), *config.log_dir.glob("desktop.log*")]) \
        if config.log_dir.exists() else []  # desktop.log: the desktop app's own events (desktop/)
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





















def _runtime_running(config: Config) -> Optional[int]:
    try:
        other = json.loads((config.run_dir / "runtime.json").read_text())
        pid = int(other["pid"])
        return pid if _pid_alive(pid) and pid != os.getpid() else None
    except (ValueError, KeyError, OSError, TypeError):
        return None


def reset_command(config: Config, chats: List[str], all_chats: List[str], personal: List[str],
                  project_memory: List[str], items: List[str], start_fresh: bool, confirm: Optional[str]) -> int:
    """M8: preview a selective reset; with --confirm PREVIEW_ID, delete exactly that preview."""
    from umbrous.memory.store import Memory
    from umbrous.reset import Reset, ResetError
    history, _, _ = _stores(config)
    mem = Memory(config.db_path)
    sel: dict = {"chats": list(chats), "chats_in": [], "personal": [], "project": {}, "start_fresh": start_fresh}
    try:
        for name in all_chats:
            p = _find_project(history, name)
            if p is None:
                print(f"No project called {name!r}.", file=sys.stderr)
                return 2
            sel["chats_in"].append(p.id)
        if "all" in personal:
            sel["personal"] = "all"
        else:
            sel["personal"] = list(personal)
        for name in project_memory:
            p = _find_project(history, name)
            if p is None:
                print(f"No project called {name!r}.", file=sys.stderr)
                return 2
            sel["project"][p.id] = "all"
        for mid in items:
            m = mem.get(mid)
            if m is None or m.store != "project":
                print(f"No project memory item {mid!r}.", file=sys.stderr)
                return 2
            if sel["project"].get(m.scope) != "all":
                sel["project"].setdefault(m.scope, []).append(mid)
        rs = Reset(config.db_path)
        if confirm:
            pid = _runtime_running(config)
            if pid:
                print(f"Umbrous is running (pid {pid}). Quit it first, or reset from the app, so no reply is "
                      "written into a chat while it is deleted. Nothing was deleted.", file=sys.stderr)
                return 1
            out = rs.confirm(sel, confirm)
            d = out["deleted"]
            print(f"Reset done: {d['chats']} chat(s) with {d['messages']} message(s), {d['personal']} personal and "
                  f"{d['project']} project memory item(s), {d['suggestions']} waiting suggestion(s).")
            for k in out["kept"]:
                print("Kept: " + k["text"])
            print(out["not_touched"])
            print(out["backups"])
            return 0
        pv = rs.preview(sel)
    except ResetError as exc:
        print(exc.message, file=sys.stderr)
        if exc.preview is not None:
            print(f"New preview: {exc.preview['preview_id']}. Run the preview again to see it.", file=sys.stderr)
        return 1
    print("A reset would delete exactly these:")
    for i in pv["items"]:
        if i["type"] == "chat":
            print(f"  chat      {i['id']}  {i['project']}: {i['title']} ({i['messages']} messages"
                  + (", archived" if i["archived"] else "") + (", a reply is running" if i["busy"] else "") + ")")
        elif i["type"] == "suggestion":
            print(f"  waiting   {i['id']}  a {i['store']} {i['kind']} suggestion from {i['chat_id']}")
        else:
            print(f"  {i['type']:<9} {i['id']}  {i['kind']}: {i['text']}")
    if pv["empty"]:
        print("  (nothing: the selection matches no chat or memory item)")
        return 0
    for k in pv["kept"]:
        print("Kept: " + k["text"])
    print(pv["not_touched"])
    print(pv["backups"])
    print(f"Nothing has been deleted. To delete exactly these, run the same command with --confirm {pv['preview_id']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="umbrous", description="Umbrous local agent harness runtime")
    parser.add_argument("--home", type=Path, help="data directory (default: ~/Library/Application Support/Umbrous)")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="start the runtime (default)")
    p_serve.add_argument("--port", type=int, help="loopback port (default 8417)")
    p_serve.add_argument("--no-open", action="store_true", help="print the one-time URL instead of opening a browser")
    p_serve.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_serve.add_argument("--desktop", action="store_true",
                         help="started by the desktop app: JSON lines on stdout, commands on stdin (umbrous/desktop.py)")
    sub.add_parser("test", help="run the test suite")
    p_purge = sub.add_parser("purge", help="delete stored records")
    p_purge.add_argument("what", choices=["logs", "receipts", "tasks"], help="what to purge")
    p_purge.add_argument("--yes", action="store_true", help="really delete")
    p_purge.add_argument("--all", dest="purge_all", action="store_true", help="receipts or tasks: all of them")
    p_purge.add_argument("--before", help="receipts or tasks: those from before this date (YYYY-MM-DD)")
    p_ws = sub.add_parser("workspace", help="show or set a project's workspace folder and access profile")
    p_ws.add_argument("project", nargs="?", help="project name or id (omit to list all)")
    p_ws.add_argument("root", nargs="?", help="folder to attach")
    p_ws.add_argument("--profile", choices=["workspace_read", "ask_before_read", "workspace_code", "code_ask_commands"])
    p_ws.add_argument("--detach", action="store_true", help="forget the folder and revoke its grants")
    p_rc = sub.add_parser("receipts", help="show tool receipts")
    p_rc.add_argument("--chat", help="all receipts of one chat (chat_…)")
    p_rc.add_argument("--task", help="all receipts of one task (tsk_…)")
    p_rc.add_argument("--last", type=int, default=20, help="how many recent receipts (default 20)")
    p_tk = sub.add_parser("tasks", help="show task state: recent tasks, or one task's trace and report")
    p_tk.add_argument("--chat", help="the tasks of one chat (chat_…)")
    p_tk.add_argument("--task", help="one task's trace and report (tsk_…)")
    p_tk.add_argument("--last", type=int, default=10, help="how many recent tasks (default 10)")
    p_mem = sub.add_parser("memory", help="personal and project memory: list, add, edit, delete (M6); "
                           "review suggestions from tasks: suggestions, accept, reject (M7)")
    p_mem.add_argument("action", nargs="?", default="list",
                       choices=["list", "add", "edit", "delete", "suggestions", "accept", "reject"])
    p_mem.add_argument("rest", nargs="*", help="add: personal|project TEXT; edit: MEM_ID TEXT; delete: MEM_ID; "
                       "accept: CAND_ID [NEW TEXT]; reject: CAND_ID (M7)")
    p_mem.add_argument("--project", help="project name or id (project items; list one project)")
    p_mem.add_argument("--kind", help="personal: fact|preference; project: decision|state|lesson")
    p_mem.add_argument("--scope", nargs="+", help="personal: the projects it applies to (default: all)")
    p_mem.add_argument("--yes", action="store_true", help="delete: really delete")
    p_mem.add_argument("--all", action="store_true", dest="all_", help="suggestions: also the decided ones (M7)")
    p_mem.add_argument("--to", choices=["personal", "project"], help="accept: the store to write it to (M7)")
    p_rs = sub.add_parser("reset", help="selective start-fresh reset: preview, then --confirm (M8)")
    p_rs.add_argument("--chat", action="append", default=[], help="a chat to delete (chat_…); repeat for more")
    p_rs.add_argument("--all-chats", action="append", default=[], metavar="PROJECT",
                      help="every chat of this project, archived too")
    p_rs.add_argument("--personal", action="append", default=[], metavar="all|MEM_ID",
                      help="personal memory: all of it, or one item; repeat for more")
    p_rs.add_argument("--project-memory", action="append", default=[], metavar="PROJECT",
                      help="all of this project's memory")
    p_rs.add_argument("--item", action="append", default=[], metavar="MEM_ID", help="one project memory item")
    p_rs.add_argument("--start-fresh", action="store_true", help="say that a new chat should follow (the app)")
    p_rs.add_argument("--confirm", metavar="PREVIEW_ID", help="delete exactly what this preview showed")
    sub.add_parser("version")
    sub.add_parser("models", help="list available models, probe results and the local exposure check")
    p_probe = sub.add_parser("probe", help="run the tool-call probe")
    p_probe.add_argument("model", nargs="*", help="model name(s)")
    p_probe.add_argument("--all", action="store_true", help="probe every available model (including ChatGPT if connected)")

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
        if getattr(args, "desktop", False):
            from umbrous.desktop import emit
            emit("error", code="config_error", message=f"Configuration error: {exc}")
        return 2

    if command == "purge":
        if args.what == "receipts":
            return purge_receipts(config, args.before, args.purge_all, args.yes)
        if args.what == "tasks":
            return purge_tasks(config, args.before, args.purge_all, args.yes)
        return purge_logs(config, args.yes)
    if command == "workspace":
        return workspace_command(config, args.project, args.root, args.profile, args.detach)
    if command == "receipts":
        return receipts_command(config, args.chat, args.task, args.last)
    if command == "tasks":
        return tasks_command(config, args.chat, args.task, args.last)
    if command == "memory":
        return memory_command(config, args.action, args.rest, args.project, args.kind, args.scope, args.yes,
                              args.to, args.all_)
    if command == "reset":
        return reset_command(config, args.chat, args.all_chats, args.personal, args.project_memory, args.item,
                             args.start_fresh, args.confirm)
    if command in ("models", "probe"):
        return models_command(config, command, getattr(args, "model", []), getattr(args, "all", False))
    desktop = args.command == "serve" and args.desktop
    open_browser = config.open_browser and not (args.command == "serve" and (args.no_open or desktop))
    return serve(config, open_browser, desktop=desktop)
