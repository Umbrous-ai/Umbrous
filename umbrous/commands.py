"""Data and model CLI commands; runtime startup stays in cli.py."""
from __future__ import annotations

import sys
from typing import List, Optional
from umbrous.config import Config, make_private_dir

def _stores(config: Config):
    from umbrous.gate.access import AccessStore
    from umbrous.history.store import HistoryStore
    from umbrous.persistence import db
    from umbrous.receipts.store import ReceiptStore
    make_private_dir(config.home)
    db.connect(config.db_path).close()  # make sure the schema is current
    return HistoryStore(config.db_path), AccessStore(config.db_path, config.home), ReceiptStore(config.db_path)


def _find_project(history, name_or_id: str):
    from umbrous.history.store import name_key
    for p in history.list_projects():
        if p.id == name_or_id or name_key(p.name) == name_key(name_or_id):
            return p
    return None


def workspace_command(config: Config, project: Optional[str], root: Optional[str], profile: Optional[str],
                      detach: bool) -> int:
    """Show or change which folder each project may read (the access view, in a terminal)."""
    from umbrous.gate.access import AccessError, PROFILES
    history, access, _ = _stores(config)
    if not project:
        for p in history.list_projects():
            view = access.view(p.id)
            ws = view["workspace"]
            print(f"{p.name} ({p.id})")
            print(f"  workspace: {ws['root'] + '  profile: ' + ws['profile_label'] if ws else 'none'}")
            for g in view["grants"]:
                print(f"  grant {g['id']}: {g['capability']} since {g['granted_at']}")
        return 0
    p = _find_project(history, project)
    if p is None:
        print(f"No project called {project!r}.", file=sys.stderr)
        return 2
    try:
        if detach:
            access.detach(p.id)
            print(f"{p.name}: workspace detached and its grants revoked. Nothing in the folder was touched.")
        elif root:
            ws = access.attach(p.id, root, profile or "workspace_read")
            print(f"{p.name}: workspace {ws.root} with the {PROFILES[ws.profile]['label']} profile.")
            print("Reminder: this is not an OS sandbox. Use a small test folder with no real secrets.")
        elif profile:
            ws = access.set_profile(p.id, profile)
            print(f"{p.name}: profile is now {PROFILES[ws.profile]['label']}.")
        else:
            ws = access.workspace(p.id)
            print(f"{p.name}: {ws.root + ' (' + PROFILES[ws.profile]['label'] + ')' if ws else 'no workspace'}")
    except AccessError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    return 0


def receipts_command(config: Config, chat: Optional[str], task: Optional[str], last: int) -> int:
    import json as _json
    _, _, receipts = _stores(config)
    records = receipts.for_task(task) if task else receipts.for_chat(chat) if chat else receipts.latest(last)
    for r in records:
        print(_json.dumps(r, indent=2) if (task or chat) else
              f"{r['started_at']}  {r['decision']:<9} {r['action']:<10} {r['scope']}  ({r['reason']})")
    if not records:
        print("No receipts.")
    return 0


def tasks_command(config: Config, chat: Optional[str], task: Optional[str], last: int) -> int:
    """Recent tasks, or one task's trace (events) and report."""
    import json as _json
    from umbrous.tasks.store import TaskStore
    _stores(config)
    store = TaskStore(config.db_path)
    if task:
        t = store.get(task)
        if t is None:
            print(f"No task {task}.", file=sys.stderr)
            return 1
        tk = t["task"]
        print(f"{tk['task_id']}  {tk['status']}{' (' + tk['code'] + ')' if tk['code'] else ''}  model {tk['model']}")
        print("trace:")
        for e in t["events"]:
            data = " ".join(f"{k}={_json.dumps(v)}" for k, v in sorted(e["data"].items()))
            print(f"  {e['seq']:>3} {e['at']}  {e['kind']:<18} {e['action_id'] or '':<32} {data}")
        print("report:")
        print(_json.dumps(t["report"], indent=2))
        if tk["status"] == "unknown":   # M5: interrupted by a crash or restart
            from umbrous.history.store import HistoryStore
            from umbrous.tasks.recover import resume_info
            info = resume_info(store, HistoryStore(config.db_path), task, t)
            print("resume: " + ("possible (in the app or the browser page). " if info["possible"] else "not possible: ")
                  + info["message"])
        return 0
    rows = store.for_chat(chat) if chat else store.latest(last)
    for r in rows:
        rep = r.get("report") or {}
        print(f"{r['created_at']}  {r['task_id']}  {r['status']:<9} {r.get('code') or '':<16} "
              f"steps={rep.get('steps', '?')} actions={rep.get('actions', {})}")
    if not rows:
        print("No tasks.")
    return 0


def memory_command(config: Config, action: str, rest: List[str], project: Optional[str], kind: Optional[str],
                   scope: Optional[List[str]], yes: bool, to: Optional[str] = None, all_: bool = False) -> int:
    """Inspect, add, edit and delete memory items (M6). Chats are never touched.
    M7: review what tasks suggested (suggestions, accept, reject)."""
    from umbrous.memory.review import Review
    from umbrous.memory.store import Memory, MemoryError_
    history, _, _ = _stores(config)
    mem = Memory(config.db_path)
    rev = Review(config.db_path, mem)
    p = None
    if project:
        p = _find_project(history, project)
        if p is None:
            print(f"No project called {project!r}.", file=sys.stderr)
            return 2

    def show(m) -> str:
        where = ("all projects" if m.scope == "all" else ", ".join(m.scope)) if m.store == "personal" else m.scope
        return f"{m.id}  {m.store:<8} {m.kind:<10} [{where}] {m.text}"

    def scope_ids() -> object:
        if not scope or scope == ["all"]:
            return "all"
        ids = []
        for name in scope:
            sp = _find_project(history, name)
            if sp is None:
                raise MemoryError_(f"No project called {name!r}.", "no_such_project")
            ids.append(sp.id)
        return ids

    def show_cand(c) -> str:
        where = ("all projects" if c.scope == "all" else ", ".join(c.scope)) if c.store == "personal" else c.scope
        if c.status != "pending":
            return f"{c.id}  {c.status:<8} {c.store:<8} {c.kind:<10}" + (f" -> {c.memory_id}" if c.memory_id else "")
        return (f"{c.id}  {c.store:<8} {c.kind:<10} [{where}] {c.text}\n"
                f"      from {'your message' if c.basis == 'message' else 'the reply'}: \"{c.quote}\""
                f"  (task {c.task_id}, chat {c.chat_id})")

    try:
        if action == "suggestions":
            items = rev.list(p.id if p else None, None if all_ else "pending")
            print("Suggestions (all):" if all_ else "Suggestions waiting for review:")
            for c in items:
                print("  " + show_cand(c))
            if not items:
                print("  (none)")
            else:
                print("Accept: umbrous memory accept CAND_ID [NEW TEXT] [--kind K] [--to personal|project] [--scope P|all]."
                      " Reject: umbrous memory reject CAND_ID.")
            return 0
        if action in ("accept", "reject"):
            if not rest:
                print(f"usage: umbrous memory {action} CAND_ID" + (" [NEW TEXT]" if action == "accept" else ""),
                      file=sys.stderr)
                return 2
            if action == "reject":
                c = rev.reject(rest[0])
                print(f"Rejected {c.id}. Its text is not kept, it was never in memory, and it will not be "
                      "suggested again in this project.")
                return 0
            c, m = rev.accept(rest[0], " ".join(rest[1:]) or None, kind, to, scope_ids() if scope else None,
                              project_exists=lambda pid: any(x.id == pid for x in history.list_projects()))
            print("Accepted " + show(m))
            return 0
        if action == "list":
            print("Personal memory:")
            items = mem.personal.list()
            for m in items:
                print("  " + show(m))
            if not items:
                print("  (none)")
            projects = [p] if p else history.list_projects()
            for pr in projects:
                items = mem.project.list(pr.id)
                if items or p:
                    print(f"Project memory of {pr.name} ({pr.id}):")
                    for m in items:
                        print("  " + show(m))
                    if not items:
                        print("  (none)")
            return 0
        if action == "add":
            if len(rest) < 2 or rest[0] not in ("personal", "project"):
                print("usage: umbrous memory add personal|project TEXT [--kind K] [--project P] [--scope P ...]",
                      file=sys.stderr)
                return 2
            text = " ".join(rest[1:])
            if rest[0] == "personal":
                m = mem.personal.add(text, kind or "fact", scope_ids())
            else:
                if p is None:
                    print("A project item needs --project NAME.", file=sys.stderr)
                    return 2
                m = mem.project.add(p.id, text, kind or "decision")
            print("Added " + show(m))
            return 0
        if not rest:
            print(f"usage: umbrous memory {action} MEM_ID" + (" TEXT" if action == "edit" else " --yes"),
                  file=sys.stderr)
            return 2
        item = mem.get(rest[0])
        if item is None:
            print(f"No memory item {rest[0]!r}.", file=sys.stderr)
            return 1
        if action == "edit":
            text = " ".join(rest[1:]) or None
            if item.store == "personal":
                m = mem.personal.update(item.id, text, kind, scope_ids() if scope else None)
            else:
                m = mem.project.update(item.id, text, kind)
            print("Edited " + show(m))
            return 0
        if not yes:
            print("Would delete " + show(item))
            print("Run again with --yes to delete it. Chats that already used it keep their text.")
            return 1
        mem.delete(item.id)
        print(f"Deleted {item.id}. It will not be retrieved for later replies. Chats that used it keep their text, "
              "and in those chats that text still goes back to the model as history.")
        return 0
    except MemoryError_ as exc:
        print(exc.message, file=sys.stderr)
        return 1


def purge_tasks(config: Config, before: Optional[str], purge_all: bool, yes: bool) -> int:
    from umbrous.tasks.store import TaskStore
    _stores(config)
    store = TaskStore(config.db_path)
    if not before and not purge_all:
        print("Say what to purge: --all, or --before YYYY-MM-DD.", file=sys.stderr)
        return 2
    n = store.count(before)
    print(f"{n} task(s) {'created before ' + before if before else 'in total'}, with their events, "
          "checkpoints and reports.")
    if not yes:
        print("Run again with --yes to delete them. Receipts are kept (purge them with `purge receipts`); "
              "this cannot be undone.")
        return 1
    print(f"Deleted {store.purge(before)} task(s).")
    return 0


def purge_receipts(config: Config, before: Optional[str], purge_all: bool, yes: bool) -> int:
    _, _, receipts = _stores(config)
    if not before and not purge_all:
        print("Say what to purge: --all, or --before YYYY-MM-DD.", file=sys.stderr)
        return 2
    n = receipts.count(before)
    print(f"{n} receipt(s) {'started before ' + before if before else 'in total'}.")
    if not yes:
        print("Run again with --yes to delete them. Receipts are the record of what tools did; this cannot be undone.")
        return 1
    print(f"Deleted {receipts.purge(before)} receipt(s).")
    return 0


def models_command(config: Config, command: str, names: List[str], probe_all: bool) -> int:
    from umbrous.model.factory import create_router
    from umbrous.persistence import db

    make_private_dir(config.home)
    db.connect(config.db_path).close()  # make sure the schema is current
    router = create_router(config)
    try:
        return _models_with_router(router, command, names, probe_all)
    finally:
        router.close()


def _models_with_router(router, command, names, probe_all):
    from umbrous.model.base import ModelError
    from umbrous.model.exposure import check_exposure
    status = router.status()
    if not status["reachable"]:
        print(status["error"], file=sys.stderr)
        return 1
    if command == "models":
        print(f"Models from Ollama and ChatGPT (Codex); local server: {status['url']}")
        report = check_exposure(router.adapter.port)
        print(f"Exposure: {report.summary}. {report.limits}")
        if report.ollama_host:
            print(f"OLLAMA_HOST is set to {report.ollama_host!r}")
        if not status["models"]:
            print("No models available. Connect ChatGPT in the app, or try: ollama pull qwen2.5:3b")
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
