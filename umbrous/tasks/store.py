"""Durable task state (M4).

A task is one message that may act: the model's steps, each tool action the
model proposed, and how it all ended. It lives in its own tables, apart from
the chat history (which holds what was said) and from memory (M6, which will
hold what is kept across chats). Receipts (M3) stay the record of each
attempt; task state adds the timeline around them, using the same ids.

What is stored, and what is not:
  stored      ids, statuses, times, tool names, workspace-relative paths,
              decision codes, counts, hashes, line ranges, limits, the report
  not stored  message or reply text, prompts, file contents, search words
              (only a hash, as in receipts), anything that redaction catches
Every string goes through the M0 redactor before it is written, and is cut to
a short length. There is no diagnostic capture of full content.

Retention: kept until you purge (`bin/umbrous purge tasks`), the same as
receipts, until the retention decision is made. Purging deletes a task's
actions, events, checkpoints and artifacts with it and overwrites the freed
pages. Copies already in backups (Time Machine) are outside Umbrous's reach.

Statuses
  task    requested -> started -> completed | failed | cancelled
          requested | started -> unknown | cancelled   (M5: only by recovery,
          for a task found mid-run after a crash or restart; see recover.py)
  action  requested -> started -> completed | denied | failed | cancelled | unknown
          requested -> denied | failed | cancelled   (never started)
          (started -> denied: the file turned out to be a link or not a plain file)
          unknown: started, but no result came back in time (a hung tool)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from umbrous.timeutil import utc_now as _now
from umbrous.persistence.session import session
from pathlib import Path
from typing import Any, ContextManager, List, Optional


from umbrous.receipts.redaction import default_redactor

TASK_STATUSES = ("requested", "started", "completed", "failed", "cancelled", "unknown")
ACTION_STATUSES = ("requested", "started", "completed", "denied", "failed", "cancelled", "unknown")
TASK_NEXT = {"requested": {"started", "failed", "cancelled"},
             "started": {"completed", "failed", "cancelled"}}
ACTION_NEXT = {"requested": {"started", "denied", "failed", "cancelled"},
               "started": {"completed", "denied", "failed", "cancelled", "unknown"}}
# M5: recovery may end an action or task from any open state, because the
# record of a step in between can be what the crash cut off.
OPEN = ("requested", "started")
MAX_TEXT = 300


class TaskError(ValueError):
    pass



def _clip(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_TEXT else obj[: MAX_TEXT - 1] + "…"
    if isinstance(obj, dict):
        return {k: _clip(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clip(v) for v in obj]
    return obj


def clean(obj: Any) -> Any:
    """What every stored value goes through: redacted, then cut short."""
    return _clip(default_redactor.redact_obj(obj))


class TaskStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    def _db(self) -> ContextManager[sqlite3.Connection]:
        return session(self.db_path)

    # ---------------------------------------------------------------- writing
    def create(self, task_id: str, project_id: Optional[str], chat_id: Optional[str], model: str, limits: dict,
               workspace: Optional[str] = None) -> None:
        """`workspace` (M9): the folder the task works in (a path of yours, not content)."""
        now = _now()
        with self._lock, self._db() as c:
            c.execute("INSERT INTO tasks (task_id, project_id, chat_id, model, status, created_at, limits, workspace) "
                      "VALUES (?, ?, ?, ?, 'requested', ?, ?, ?)",
                      (task_id, project_id, chat_id, model, now, json.dumps(limits), workspace))
            self._event(c, task_id, "task.requested", None, {"model": model, "limits": limits}, now)

    def set_status(self, task_id: str, status: str, code: Optional[str] = None, **data) -> None:
        if status not in TASK_STATUSES:
            raise TaskError(f"unknown task status {status!r}")
        now = _now()
        with self._lock, self._db() as c:
            row = c.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskError("no such task")
            if status not in TASK_NEXT.get(row[0], set()):
                raise TaskError(f"a task cannot go from {row[0]} to {status}")
            col = "started_at" if status == "started" else "ended_at"
            c.execute(f"UPDATE tasks SET status = ?, code = ?, {col} = ? WHERE task_id = ?", (status, code, now, task_id))
            self._event(c, task_id, "task." + status, None, dict(data, code=code) if code else data, now)

    def add_action(self, task_id: str, action_id: str, step: int, tool: str, resource: Optional[str]) -> None:
        now = _now()
        with self._lock, self._db() as c:
            c.execute("INSERT INTO task_actions (action_id, task_id, step, tool, resource, status, requested_at) "
                      "VALUES (?, ?, ?, ?, ?, 'requested', ?)", (action_id, task_id, step, clean(tool), clean(resource), now))
            self._event(c, task_id, "action.requested", action_id, {"step": step, "tool": tool, "resource": resource}, now)

    def action_status(self, action_id: str, status: str, code: Optional[str] = None,
                      receipt_id: Optional[str] = None, **data) -> None:
        if status not in ACTION_STATUSES:
            raise TaskError(f"unknown action status {status!r}")
        now = _now()
        with self._lock, self._db() as c:
            row = c.execute("SELECT task_id, status FROM task_actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None:
                raise TaskError("no such action")
            if status not in ACTION_NEXT.get(row[1], set()):
                raise TaskError(f"an action cannot go from {row[1]} to {status}")
            c.execute("UPDATE task_actions SET status = ?, code = COALESCE(?, code), receipt_id = COALESCE(?, receipt_id), "
                      "ended_at = CASE WHEN ? = 'started' THEN ended_at ELSE ? END WHERE action_id = ?",
                      (status, code, receipt_id, status, now, action_id))
            payload = dict(data)
            if code:
                payload["code"] = code
            if receipt_id:
                payload["receipt_id"] = receipt_id
            self._event(c, row[0], "action." + status, action_id, payload, now)

    def recover_action(self, action_id: str, status: str, code: Optional[str], receipt_id: Optional[str] = None,
                       **data) -> None:
        """M5 recovery: end an action that was still open when the runtime stopped."""
        if status not in ACTION_STATUSES or status in OPEN:
            raise TaskError(f"recovery cannot set an action to {status!r}")
        now = _now()
        with self._lock, self._db() as c:
            row = c.execute("SELECT task_id, status FROM task_actions WHERE action_id = ?", (action_id,)).fetchone()
            if row is None or row[1] not in OPEN:
                raise TaskError("only an open action can be reconciled")
            c.execute("UPDATE task_actions SET status = ?, code = ?, receipt_id = COALESCE(?, receipt_id), ended_at = ? "
                      "WHERE action_id = ?", (status, code, receipt_id, now, action_id))
            self._event(c, row[0], "action.reconciled", action_id,
                        dict(data, was=row[1], status=status, code=code, receipt_id=receipt_id), now)

    def recover_task(self, task_id: str, status: str, code: str, **data) -> None:
        """M5 recovery: end a task that was still open when the runtime stopped
        (unknown: interrupted; cancelled: you had pressed Stop)."""
        if status not in ("unknown", "cancelled"):
            raise TaskError(f"recovery cannot set a task to {status!r}")
        now = _now()
        with self._lock, self._db() as c:
            row = c.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row[0] not in OPEN:
                raise TaskError("only an open task can be recovered")
            c.execute("UPDATE tasks SET status = ?, code = ?, ended_at = ? WHERE task_id = ?", (status, code, now, task_id))
            self._event(c, task_id, "task.interrupted" if status == "unknown" else "task.cancelled", None,
                        dict(data, was=row[0], code=code), now)

    def open_tasks(self) -> List[str]:
        """Tasks still requested or started: after a restart, ones the runtime stopped in the middle of."""
        with self._db() as c:
            return [r[0] for r in c.execute("SELECT task_id FROM tasks WHERE status IN ('requested', 'started') "
                                            "ORDER BY created_at").fetchall()]

    def event(self, task_id: str, kind: str, action_id: Optional[str] = None, **data) -> None:
        with self._lock, self._db() as c:
            self._event(c, task_id, kind, action_id, data, _now())

    def checkpoint(self, task_id: str, step: int, state: dict) -> None:
        now = _now()
        with self._lock, self._db() as c:
            c.execute("INSERT OR REPLACE INTO task_checkpoints (task_id, step, at, state) VALUES (?, ?, ?, ?)",
                      (task_id, step, now, json.dumps(clean(state))))
            self._event(c, task_id, "checkpoint", None, {"step": step}, now)

    def artifact(self, task_id: str, kind: str, body: dict) -> str:
        """A product of the task. In M4 the only kind is "report": statuses,
        counts, evidence references and reasons, never content."""
        artifact_id = f"art_{int(time.time() * 1000):012x}{secrets.token_hex(8)}"
        text = json.dumps(clean(body), sort_keys=True)
        now = _now()
        with self._lock, self._db() as c:
            c.execute("INSERT INTO task_artifacts (artifact_id, task_id, kind, created_at, sha256, body) VALUES (?, ?, ?, ?, ?, ?)",
                      (artifact_id, task_id, kind, now, hashlib.sha256(text.encode()).hexdigest(), text))
            self._event(c, task_id, "artifact", None, {"artifact_id": artifact_id, "kind": kind}, now)
        return artifact_id

    def _event(self, c: sqlite3.Connection, task_id: str, kind: str, action_id: Optional[str], data: dict, at: str) -> None:
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        c.execute("INSERT INTO task_events (task_id, seq, at, kind, action_id, data) VALUES (?, ?, ?, ?, ?, ?)",
                  (task_id, seq, at, kind, action_id, json.dumps(clean(data), sort_keys=True)))

    # ---------------------------------------------------------------- reading
    def get(self, task_id: str) -> Optional[dict]:
        with self._db() as c:
            t = c.execute("SELECT task_id, project_id, chat_id, model, status, code, created_at, started_at, ended_at, limits, "
                          "workspace FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if t is None:
                return None
            actions = c.execute("SELECT action_id, step, tool, resource, status, code, receipt_id, requested_at, ended_at "
                                "FROM task_actions WHERE task_id = ? ORDER BY requested_at, rowid", (task_id,)).fetchall()
            events = c.execute("SELECT seq, at, kind, action_id, data FROM task_events WHERE task_id = ? ORDER BY seq",
                               (task_id,)).fetchall()
            checkpoints = c.execute("SELECT step, at, state FROM task_checkpoints WHERE task_id = ? ORDER BY step",
                                    (task_id,)).fetchall()
            artifacts = c.execute("SELECT artifact_id, kind, created_at, sha256, body FROM task_artifacts WHERE task_id = ? "
                                  "ORDER BY created_at", (task_id,)).fetchall()
        keys = ("task_id", "project_id", "chat_id", "model", "status", "code", "created_at", "started_at", "ended_at")
        task = dict(zip(keys, t[:9]), limits=json.loads(t[9]), workspace=t[10])
        report = next((json.loads(a[4]) for a in artifacts if a[1] == "report"), None)
        return {
            "task": task,
            "actions": [dict(zip(("action_id", "step", "tool", "resource", "status", "code", "receipt_id",
                                  "requested_at", "ended_at"), a)) for a in actions],
            "events": [{"seq": e[0], "at": e[1], "kind": e[2], "action_id": e[3], "data": json.loads(e[4])} for e in events],
            "checkpoints": [{"step": k[0], "at": k[1], "state": json.loads(k[2])} for k in checkpoints],
            "artifacts": [{"artifact_id": a[0], "kind": a[1], "created_at": a[2], "sha256": a[3]} for a in artifacts],
            "report": report,
        }

    def for_chat(self, chat_id: str, limit: int = 500) -> List[dict]:
        """Each task of a chat with its report (no events): what the page needs to redraw a chat."""
        with self._db() as c:
            rows = c.execute("SELECT t.task_id, t.status, t.code, t.created_at, t.ended_at, "
                             "(SELECT body FROM task_artifacts a WHERE a.task_id = t.task_id AND a.kind = 'report') "
                             "FROM tasks t WHERE t.chat_id = ? ORDER BY t.created_at LIMIT ?", (chat_id, limit)).fetchall()
        return [{"task_id": r[0], "status": r[1], "code": r[2], "created_at": r[3], "ended_at": r[4],
                 "report": json.loads(r[5]) if r[5] else None} for r in rows]

    def latest(self, limit: int = 10) -> List[dict]:
        with self._db() as c:
            rows = c.execute("SELECT t.task_id, t.chat_id, t.model, t.status, t.code, t.created_at, t.ended_at, "
                             "(SELECT body FROM task_artifacts a WHERE a.task_id = t.task_id AND a.kind = 'report') "
                             "FROM tasks t ORDER BY t.created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ("task_id", "chat_id", "model", "status", "code", "created_at", "ended_at")
        return [dict(zip(keys, r[:7]), report=json.loads(r[7]) if r[7] else None) for r in reversed(rows)]

    def count(self, before: Optional[str] = None) -> int:
        with self._db() as c:
            if before:
                return c.execute("SELECT COUNT(*) FROM tasks WHERE created_at < ?", (before,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def purge(self, before: Optional[str] = None) -> int:
        """Delete tasks (all, or those created before an ISO date) with their actions,
        events, checkpoints and artifacts. Only `bin/umbrous purge tasks` calls this.
        Receipts are purged separately (`purge receipts`)."""
        with self._lock, self._db() as c:
            c.execute("PRAGMA secure_delete = ON")
            where, args = ("WHERE created_at < ?", (before,)) if before else ("", ())
            n = c.execute(f"DELETE FROM tasks {where}", args).rowcount   # ON DELETE CASCADE removes the rest
        return n
