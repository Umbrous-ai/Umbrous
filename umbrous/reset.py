"""Selective start-fresh reset (M8).

You choose what goes: chats, personal memory and/or project memory. Umbrous
shows the exact items (a preview), and only a confirmation that names that
preview deletes anything. There is no "delete everything": each part of a
selection names a store, and "all" only ever means all of one store (all
personal memory, all of ONE project's memory, all chats of ONE project).

    preview(selection)                 -> the exact items, what stays, preview_id
    confirm(selection, preview_id)     -> deletes exactly those items, or refuses

The preview id is a fingerprint of the exact items and their state, so it is
also how a change is caught. Confirm resolves the selection again, inside the
write transaction that deletes, and refuses (`preview_changed`, with the new
preview) if anything differs: a chat got a message, a memory item was edited,
a new chat or item now matches "all", an item is already gone. Nothing is
deleted then, and you look at the new preview. Cancelling is not an action at
all: a preview stores nothing, so not confirming changes nothing.

What a reset deletes, in one transaction with secure_delete on (freed pages
are overwritten in the database file):

  chat        its row, every message, its pending turn (M5), and the
              suggestions from it still waiting for review (M7, they quote it)
  personal    the item's row
  project     the item's row

What it keeps, and says so in every preview and result (`kept`):
task records and receipts of reset chats (ids, statuses, file paths, no
message text) until you purge them; memory items that came from a reset chat
unless you select them too; replies in other chats that used or repeated a
reset memory item; the review history (accepted and rejected suggestions:
ids and a hash, no text); projects, workspaces, grants and settings; logs
(ids only). And backups: Umbrous makes none, and cannot reach copies made by
Time Machine or anything else before the reset.

Memory has no index or cache (docs/m6-memory.md): retrieval reads the tables
each time, so a deleted item is never retrieved again. The only runtime state
per chat is its reply lock, which the server forgets after a reset. The page
and the app reload what they show.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from umbrous.persistence.session import session
from pathlib import Path
from typing import Callable, Dict, ContextManager, List, Optional

CHAT_ID = re.compile(r"^chat_[A-Za-z0-9_-]{8,64}$")
MEM_ID = re.compile(r"^mem_[0-9a-f]{16}$")
PROJECT_ID = re.compile(r"^prj_[A-Za-z0-9_-]{4,64}$")
MAX_IDS = 500

BACKUPS = ("Umbrous keeps no backup or undo of its own: a confirmed reset cannot be reversed in Umbrous. "
           "Inside the database file the freed space is overwritten. A copy of the data folder made before "
           "the reset (Time Machine, another backup tool, a copied folder) still holds these items, and "
           "Umbrous cannot reach it.")
NOT_TOUCHED = ("Projects themselves, workspace folders, access grants and settings are not part of a reset. "
               "Logs never held message or memory text; they keep ids, including the ids of what was reset.")


class ResetError(ValueError):
    def __init__(self, message: str, code: str, preview: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.preview = preview


def _ids(value: object, pattern: "re.Pattern[str]", what: str) -> List[str]:
    if not isinstance(value, list) or len(value) > MAX_IDS or not all(isinstance(v, str) for v in value):
        raise ResetError(f"{what} is a list of ids.", "bad_selection")
    bad = [v for v in value if not pattern.match(v)]
    if bad:
        raise ResetError(f"Not a valid id in {what}: {bad[0]!r}.", "bad_selection")
    return sorted(set(value))


def normalize(selection: object) -> dict:
    """The selection in one canonical form. Keys: chats (ids), chats_in
    (project ids: all their chats, archived too), personal ("all" or ids),
    project ({project id: "all" or ids}), start_fresh (bool)."""
    if not isinstance(selection, dict):
        raise ResetError("The selection is a JSON object.", "bad_selection")
    unknown = set(selection) - {"chats", "chats_in", "personal", "project", "start_fresh"}
    if unknown:
        raise ResetError(f"Unknown part of a selection: {sorted(unknown)[0]!r}.", "bad_selection")
    out: dict = {"chats": _ids(selection.get("chats", []), CHAT_ID, "chats"),
                 "chats_in": _ids(selection.get("chats_in", []), PROJECT_ID, "chats_in")}
    personal = selection.get("personal", [])
    out["personal"] = "all" if personal == "all" else _ids(personal, MEM_ID, "personal")
    project = selection.get("project", {})
    if not isinstance(project, dict):
        raise ResetError('project is {"prj_…": "all" or [ids]}.', "bad_selection")
    out["project"] = {}
    for pid in sorted(project):
        if not PROJECT_ID.match(pid):
            raise ResetError(f"Not a valid project id: {pid!r}.", "bad_selection")
        v = project[pid]
        out["project"][pid] = "all" if v == "all" else _ids(v, MEM_ID, f"project {pid}")
    out["start_fresh"] = selection.get("start_fresh", False) is True
    if not (out["chats"] or out["chats_in"] or out["personal"] or any(out["project"].values())):
        raise ResetError("Nothing is selected. Choose chats or memory items to reset.", "nothing_selected")
    return out


class Reset:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _db(self) -> ContextManager[sqlite3.Connection]:
        return session(self.db_path)

    # ---------------------------------------------------------------- preview
    def preview(self, selection: object, busy: Optional[Callable[[str], bool]] = None) -> dict:
        sel = normalize(selection)
        with self._db() as c:
            return self._preview(c, sel, busy)

    def _preview(self, c: sqlite3.Connection, sel: dict, busy: Optional[Callable[[str], bool]]) -> dict:
        projects = dict(c.execute("SELECT id, name FROM projects").fetchall())
        for pid in list(sel["chats_in"]) + list(sel["project"]):
            if pid not in projects:
                raise ResetError(f"There is no project {pid}.", "no_such_project")

        # chats: the named ones, and every chat of each named project
        chat_rows = {}
        q = ("SELECT c.id, c.project_id, c.title, c.updated_at, c.archived_at, "
             "(SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id), "
             "(SELECT COALESCE(MAX(seq), 0) FROM messages m WHERE m.chat_id = c.id), "
             "(SELECT COUNT(*) FROM pending_turns p WHERE p.chat_id = c.id) FROM chats c ")
        for cid in sel["chats"]:
            row = c.execute(q + "WHERE c.id = ?", (cid,)).fetchone()
            if row is None:
                raise ResetError(f"There is no chat {cid}. It may have been reset already.", "no_such_chat")
            chat_rows[cid] = row
        for pid in sel["chats_in"]:
            for row in c.execute(q + "WHERE c.project_id = ? ORDER BY c.updated_at DESC, c.id", (pid,)):
                chat_rows[row[0]] = row
        chat_ids = sorted(chat_rows)

        def mem_rows(table: str, where: str, args: tuple) -> list:
            cols = ("id, kind, text, scope, updated_at, NULL" if table == "personal_memory"
                    else "id, kind, text, NULL, updated_at, project_id")
            return c.execute(f"SELECT {cols} FROM {table} WHERE {where} ORDER BY created_at, id", args).fetchall()

        personal = []
        if sel["personal"] == "all":
            personal = mem_rows("personal_memory", "1 = 1", ())
        else:
            for mid in sel["personal"]:
                rows = mem_rows("personal_memory", "id = ?", (mid,))
                if not rows:
                    raise ResetError(f"There is no personal memory item {mid}.", "no_such_memory")
                personal += rows
        project = []
        for pid, ids in sel["project"].items():
            if ids == "all":
                project += mem_rows("project_memory", "project_id = ?", (pid,))
                continue
            for mid in ids:
                rows = mem_rows("project_memory", "id = ? AND project_id = ?", (mid, pid))
                if not rows:
                    raise ResetError(f"There is no memory item {mid} in project {pid}.", "no_such_memory")
                project += rows

        marks = ",".join("?" * len(chat_ids))
        suggestions = c.execute(
            f"SELECT id, chat_id, store, kind, created_at, text, quote, scope, edited FROM memory_candidates "
            f"WHERE status = 'pending' "
            f"AND chat_id IN ({marks}) ORDER BY rowid", chat_ids).fetchall() if chat_ids else []

        items: List[dict] = []
        blocked = []
        for cid in chat_ids:
            _, pid, title, updated, archived, n, seq, pend = chat_rows[cid]
            b = bool(busy and busy(cid))
            if b:
                blocked.append(cid)
            items.append({"type": "chat", "id": cid, "title": title, "project_id": pid,
                          "project": projects.get(pid, pid), "messages": n, "archived": archived is not None,
                          "busy": b, "_fp": [updated, archived, n, seq, pend]})
        for mid, kind, text, scope, updated, _ in personal:
            sc = "all" if scope == "all" else json.loads(scope)
            items.append({"type": "personal", "id": mid, "kind": kind, "text": text, "scope": sc,
                          "_fp": [kind, text, scope, updated]})
        for mid, kind, text, _, updated, pid in project:
            items.append({"type": "project", "id": mid, "kind": kind, "text": text, "project_id": pid,
                          "project": projects.get(pid, pid), "_fp": [kind, text, updated]})
        for sid, cid, store, kind, created, text, quote, scope, edited in suggestions:
            # every field review can change, so an edit after the preview is caught
            items.append({"type": "suggestion", "id": sid, "chat_id": cid, "store": store, "kind": kind,
                          "_fp": [created, store, kind, text, quote, scope, edited]})

        fp = hashlib.sha256(json.dumps([sel, [(i["type"], i["id"], i["_fp"]) for i in items]],
                                       sort_keys=True).encode()).hexdigest()
        for i in items:
            del i["_fp"]
        counts = {"chats": len(chat_ids), "messages": sum(chat_rows[x][5] for x in chat_ids),
                  "personal": len(personal), "project": len(project), "suggestions": len(suggestions)}
        mem_ids = [r[0] for r in personal] + [r[0] for r in project]
        return {"preview_id": "rst_" + fp[:32], "selection": sel, "items": items, "counts": counts,
                "empty": not (chat_ids or mem_ids), "blocked": blocked, "start_fresh": sel["start_fresh"],
                "kept": self._kept(c, chat_ids, mem_ids, sel), "not_touched": NOT_TOUCHED, "backups": BACKUPS}

    def _kept(self, c: sqlite3.Connection, chat_ids: List[str], mem_ids: List[str], sel: dict) -> List[dict]:
        """What stays that refers to the selection, with counts. Shown before
        and after, so a reset never reads as complete erasure."""
        kept = []
        if chat_ids:
            marks = ",".join("?" * len(chat_ids))
            tasks = c.execute(f"SELECT COUNT(*) FROM tasks WHERE chat_id IN ({marks})", chat_ids).fetchone()[0]
            receipts = c.execute(f"SELECT COUNT(*) FROM receipts WHERE chat_id IN ({marks})", chat_ids).fetchone()[0]
            if tasks or receipts:
                kept.append({"what": "task_records", "count": tasks + receipts,
                             "text": f"{tasks} task record(s) and {receipts} receipt(s) of these chats stay until "
                                     "you purge them (bin/umbrous purge tasks, purge receipts). They hold ids, "
                                     "statuses, file paths and line numbers, not message or reply text, and "
                                     "they will name chat ids that no longer exist."})
            from_chats = 0
            for (mid, source) in c.execute("SELECT id, source FROM personal_memory UNION ALL "
                                           "SELECT id, source FROM project_memory"):
                if mid in mem_ids:
                    continue
                try:
                    if json.loads(source).get("chat_id") in chat_ids:
                        from_chats += 1
                except (ValueError, AttributeError):
                    pass
            if from_chats:
                kept.append({"what": "memory_from_chats", "count": from_chats,
                             "text": f"{from_chats} memory item(s) you accepted from these chats stay in memory, "
                                     "because you did not select them. Their source names a chat that will no "
                                     "longer exist."})
            decided = c.execute(f"SELECT COUNT(*) FROM memory_candidates WHERE status != 'pending' AND chat_id IN "
                                f"({marks})", chat_ids).fetchone()[0]
            if decided:
                kept.append({"what": "review_history", "count": decided,
                             "text": f"{decided} accepted or rejected suggestion(s) from these chats stay in the "
                                     "review history: ids and a hash, no text. A rejected one is still not "
                                     "offered again in its project."})
        if mem_ids:
            used = 0
            for (ids,) in c.execute("SELECT memory_ids FROM messages WHERE memory_ids IS NOT NULL"
                                    + (f" AND chat_id NOT IN ({','.join('?' * len(chat_ids))})" if chat_ids else ""),
                                    chat_ids):
                try:
                    if set(json.loads(ids)) & set(mem_ids):
                        used += 1
                except (ValueError, TypeError):
                    pass
            if used:
                kept.append({"what": "replies_using_memory", "count": used,
                             "text": f"{used} reply(ies) in chats you keep were given these memory items. They "
                                     "stay as written and may repeat an item's text; that text goes back to the "
                                     "model as recent history of its own chat. Reset those chats too, or start "
                                     "a new chat, for a context without them."})
        return kept

    # ---------------------------------------------------------------- confirm
    def confirm(self, selection: object, preview_id: str, locks=None) -> dict:
        """Delete exactly what preview `preview_id` showed, or nothing.

        `locks` (the server's ChatLocks) keeps replies out of the chats while
        they are deleted: a chat with a reply running is refused (chat_busy)."""
        sel = normalize(selection)
        busy = (lambda cid: locks.get(cid).locked()) if locks is not None else None
        first = self.preview(sel, busy)
        if first["preview_id"] != preview_id:
            raise ResetError("What this selection covers changed since the preview. Check the new preview; "
                             "nothing was deleted.", "preview_changed", first)
        if first["empty"]:
            raise ResetError("The selection matches nothing, so there is nothing to reset.", "nothing_selected")
        chat_ids = [i["id"] for i in first["items"] if i["type"] == "chat"]
        held = []
        try:
            for cid in chat_ids:
                if locks is None:
                    break
                lock = locks.get(cid)
                if not lock.acquire(blocking=False):
                    raise ResetError("A reply is running in a selected chat. Stop it or let it finish, then "
                                     "reset; nothing was deleted.", "chat_busy", first)
                held.append(lock)
            with self._db() as c:
                c.execute("PRAGMA secure_delete = ON")
                c.execute("BEGIN IMMEDIATE")
                try:
                    again = self._preview(c, sel, None)
                    if again["preview_id"] != preview_id:
                        raise ResetError("What this selection covers changed since the preview. Check the new "
                                         "preview; nothing was deleted.", "preview_changed", again)
                    deleted = self._delete(c, again)
                    c.execute("COMMIT")
                except BaseException:
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                    raise
        finally:
            for lock in held:
                lock.release()
        if locks is not None and hasattr(locks, "forget"):
            locks.forget(chat_ids)
        return {"done": True, "deleted": deleted, "items": [{"type": i["type"], "id": i["id"]} for i in first["items"]],
                "kept": first["kept"], "not_touched": NOT_TOUCHED, "backups": BACKUPS,
                "start_fresh": sel["start_fresh"]}

    @staticmethod
    def _delete(c: sqlite3.Connection, preview: dict) -> Dict[str, int]:
        by: Dict[str, List[str]] = {"chat": [], "personal": [], "project": [], "suggestion": []}
        for i in preview["items"]:
            by[i["type"]].append(i["id"])

        def run(sql: str, ids: List[str]) -> int:
            if not ids:
                return 0
            return c.execute(sql.format(",".join("?" * len(ids))), ids).rowcount

        out = {"suggestions": run("DELETE FROM memory_candidates WHERE status = 'pending' AND id IN ({})",
                                  by["suggestion"]),
               "pending_turns": run("DELETE FROM pending_turns WHERE chat_id IN ({})", by["chat"]),
               "messages": run("DELETE FROM messages WHERE chat_id IN ({})", by["chat"]),
               "chats": run("DELETE FROM chats WHERE id IN ({})", by["chat"]),
               "personal": run("DELETE FROM personal_memory WHERE id IN ({})", by["personal"]),
               "project": run("DELETE FROM project_memory WHERE id IN ({})", by["project"])}
        return out
