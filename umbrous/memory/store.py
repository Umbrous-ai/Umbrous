"""Personal Memory and Project Operational Memory (M6).

Two logical stores, each with its own table and its own repository class.
Nothing else writes to these tables, and these classes touch no other table:
chat history, task records and receipts live elsewhere, and deleting from one
never deletes from another.

  personal_memory   facts and preferences about you. Each item has a scope:
                    every project, or a list of projects. It is used only in
                    chats of a project inside that scope.
  project_memory    decisions, useful state and lessons of ONE project. It is
                    used only in chats of that project, never in another.

Every item has an id (mem_…), its store, its scope, a kind, its text, a
source (how it got here: typed by you, or later proposed from a chat), times,
and an approval state. In M6 every item is entered by you, so it is
`approved`; `proposed` and `rejected` are reserved for M7, where proposed
writes need your approval first. Only `approved` items are ever retrieved.

Deleting an item removes its row (secure_delete on, so the freed pages are
overwritten). There is no index, embedding or cache of memory anywhere:
retrieval reads the table each time, so a deleted item cannot come back in a
later reply. Replies already written (and the transcript that holds them) are
not changed; they keep the memory ids they used, as a reference.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from umbrous.receipts.redaction import default_redactor

MAX_TEXT = 500
MAX_ITEMS = 200                 # per personal store, and per project
PERSONAL_KINDS = ("fact", "preference")
PROJECT_KINDS = ("decision", "state", "lesson")
APPROVALS = ("approved", "proposed", "rejected")


class MemoryError_(ValueError):
    code = "memory_error"

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_memory_id() -> str:
    return "mem_" + secrets.token_hex(8)


@dataclass(frozen=True)
class MemoryItem:
    id: str
    store: str                      # personal | project
    kind: str
    text: str
    scope: object                   # personal: "all" or [project ids]; project: the project id
    source: dict                    # {"type": "manual"} (M6); M7 adds {"type": "chat", "chat_id", "task_id"}
    approval: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def check_text(text: object) -> str:
    if not isinstance(text, str):
        raise MemoryError_("A memory item needs text.", "bad_text")
    text = " ".join(text.split())
    if not text or len(text) > MAX_TEXT:
        raise MemoryError_(f"A memory item is 1 to {MAX_TEXT} characters.", "bad_text")
    # Memory goes into prompts. Something that looks like a key, token or
    # password is refused rather than stored and sent to every reply.
    if default_redactor.redact(text) != text:
        raise MemoryError_("That looks like a key, token or password. Memory never stores secrets.", "looks_secret")
    return text


class _Repo:
    table = ""
    store = ""
    kinds: Sequence[str] = ()

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _kind(self, kind: object) -> str:
        if kind not in self.kinds:
            raise MemoryError_(f"kind must be one of {', '.join(self.kinds)}", "bad_kind")
        return str(kind)

    def delete(self, item_id: str) -> bool:
        """Remove the item. Transcripts, task records and receipts are not touched."""
        with self._lock, self._db() as c:
            c.execute("PRAGMA secure_delete = ON")
            return c.execute(f"DELETE FROM {self.table} WHERE id = ?", (item_id,)).rowcount > 0

    def count(self) -> int:
        with self._db() as c:
            return c.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]


class PersonalMemory(_Repo):
    table, store, kinds = "personal_memory", "personal", PERSONAL_KINDS

    def _row(self, r) -> MemoryItem:
        scope = "all" if r[3] == "all" else json.loads(r[3])
        return MemoryItem(r[0], "personal", r[1], r[2], scope, json.loads(r[4]), r[5], r[6], r[7])

    _cols = "id, kind, text, scope, source, approval, created_at, updated_at"

    @staticmethod
    def _scope(scope: object, project_exists) -> str:
        if scope is None or scope == "all":
            return "all"
        if isinstance(scope, list) and scope and all(isinstance(p, str) for p in scope) and len(scope) <= 50:
            for p in scope:
                if not project_exists(p):
                    raise MemoryError_(f"There is no project {p!r}.", "no_such_project")
            return json.dumps(sorted(set(scope)))
        raise MemoryError_('scope is "all" or a list of project ids', "bad_scope")

    def add(self, text: object, kind: object = "fact", scope: object = "all", project_exists=lambda p: True,
            source: Optional[dict] = None, approval: str = "approved") -> MemoryItem:
        text, kind, sc = check_text(text), self._kind(kind), self._scope(scope, project_exists)
        if approval not in APPROVALS:
            raise MemoryError_("bad approval state", "bad_approval")
        item_id, now = new_memory_id(), _now()
        with self._lock, self._db() as c:
            if c.execute("SELECT COUNT(*) FROM personal_memory").fetchone()[0] >= MAX_ITEMS:
                raise MemoryError_(f"Personal memory holds at most {MAX_ITEMS} items.", "memory_full")
            c.execute(f"INSERT INTO personal_memory ({self._cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (item_id, kind, text, sc, json.dumps(source or {"type": "manual"}), approval, now, now))
        return self.get(item_id)

    def get(self, item_id: str) -> Optional[MemoryItem]:
        with self._db() as c:
            r = c.execute(f"SELECT {self._cols} FROM personal_memory WHERE id = ?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def update(self, item_id: str, text: object = None, kind: object = None, scope: object = None,
               project_exists=lambda p: True) -> MemoryItem:
        old = self.get(item_id)
        if old is None:
            raise MemoryError_("That memory item does not exist.", "no_such_memory")
        text = check_text(text) if text is not None else old.text
        kind = self._kind(kind) if kind is not None else old.kind
        sc = self._scope(scope, project_exists) if scope is not None else (
            "all" if old.scope == "all" else json.dumps(old.scope))
        with self._lock, self._db() as c:
            c.execute("UPDATE personal_memory SET text = ?, kind = ?, scope = ?, updated_at = ? WHERE id = ?",
                      (text, kind, sc, _now(), item_id))
        return self.get(item_id)

    def list(self) -> List[MemoryItem]:
        with self._db() as c:
            rows = c.execute(f"SELECT {self._cols} FROM personal_memory ORDER BY created_at, id").fetchall()
        return [self._row(r) for r in rows]

    def for_project(self, project_id: str) -> List[MemoryItem]:
        """Approved personal items whose scope includes this project."""
        return [m for m in self.list() if m.approval == "approved" and (m.scope == "all" or project_id in m.scope)]


class ProjectMemory(_Repo):
    table, store, kinds = "project_memory", "project", PROJECT_KINDS
    _cols = "id, project_id, kind, text, source, approval, created_at, updated_at"

    def _row(self, r) -> MemoryItem:
        return MemoryItem(r[0], "project", r[2], r[3], r[1], json.loads(r[4]), r[5], r[6], r[7])

    def add(self, project_id: str, text: object, kind: object = "decision", source: Optional[dict] = None,
            approval: str = "approved") -> MemoryItem:
        text, kind = check_text(text), self._kind(kind)
        if approval not in APPROVALS:
            raise MemoryError_("bad approval state", "bad_approval")
        item_id, now = new_memory_id(), _now()
        with self._lock, self._db() as c:
            if c.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise MemoryError_("That project does not exist.", "no_such_project")
            if c.execute("SELECT COUNT(*) FROM project_memory WHERE project_id = ?", (project_id,)).fetchone()[0] >= MAX_ITEMS:
                raise MemoryError_(f"A project's memory holds at most {MAX_ITEMS} items.", "memory_full")
            c.execute(f"INSERT INTO project_memory ({self._cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (item_id, project_id, kind, text, json.dumps(source or {"type": "manual"}), approval, now, now))
        return self.get(item_id)

    def get(self, item_id: str) -> Optional[MemoryItem]:
        with self._db() as c:
            r = c.execute(f"SELECT {self._cols} FROM project_memory WHERE id = ?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def update(self, item_id: str, text: object = None, kind: object = None) -> MemoryItem:
        old = self.get(item_id)
        if old is None:
            raise MemoryError_("That memory item does not exist.", "no_such_memory")
        text = check_text(text) if text is not None else old.text
        kind = self._kind(kind) if kind is not None else old.kind
        with self._lock, self._db() as c:
            c.execute("UPDATE project_memory SET text = ?, kind = ?, updated_at = ? WHERE id = ?",
                      (text, kind, _now(), item_id))
        return self.get(item_id)

    def list(self, project_id: str) -> List[MemoryItem]:
        with self._db() as c:
            rows = c.execute(f"SELECT {self._cols} FROM project_memory WHERE project_id = ? ORDER BY created_at, id",
                             (project_id,)).fetchall()
        return [self._row(r) for r in rows]

    def for_project(self, project_id: str) -> List[MemoryItem]:
        """Approved items of this project only. There is no query across projects."""
        return [m for m in self.list(project_id) if m.approval == "approved"]


class Memory:
    """Both stores together, for the run loop and the API. Lookups by id try
    each store; nothing joins them."""

    def __init__(self, db_path: Path) -> None:
        self.personal = PersonalMemory(db_path)
        self.project = ProjectMemory(db_path)

    def get(self, item_id: str) -> Optional[MemoryItem]:
        return self.personal.get(item_id) or self.project.get(item_id)

    def delete(self, item_id: str) -> bool:
        return self.personal.delete(item_id) or self.project.delete(item_id)
