"""Chat History / Session Store (M2).

Its own logical store, in the `projects`, `chats` and `messages` tables of the
runtime's SQLite file. Nothing else writes there, and nothing here touches
other stores: memory (M6) and task records/receipts (M3, M4) will live in their
own tables, and no operation in this module reaches into them.

What each word means here:
  transcript   the messages of one chat, exactly as sent and received
  archived     a chat with archived_at set: hidden from the normal list,
               read-only, every message kept. Unarchive clears archived_at.
  deleted      not possible yet. The deletion contract (recovery, backups,
               derived copies) is an open decision, so there is no delete
               function, endpoint or button. See docs/m2-history.md.

Every chat belongs to exactly one project. A fresh database has one project,
"General".
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from umbrous.context.assemble import Turn

DEFAULT_PROJECT_ID = "prj_general"
MAX_PROJECT_NAME = 60
MAX_TITLE = 80
STATUSES = ("complete", "stopped", "interrupted", "timeout", "error")


class HistoryError(ValueError):
    code = "history_error"

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_title(first_message: str) -> str:
    """A chat's title is the start of its first message, made locally.
    (No model is asked to summarise it: that would be another copy of the text.)"""
    line = " ".join(first_message.split())
    return line if len(line) <= MAX_TITLE else line[: MAX_TITLE - 1].rstrip() + "…"


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    created_at: str
    chats: int = 0
    archived_chats: int = 0

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "created_at": self.created_at,
                "chats": self.chats, "archived_chats": self.archived_chats}


@dataclass(frozen=True)
class ChatInfo:
    id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str
    archived_at: Optional[str]
    messages: int = 0

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def as_dict(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "archived": self.archived, "archived_at": self.archived_at, "messages": self.messages}


@dataclass(frozen=True)
class Message:
    seq: int
    role: str
    content: str
    model: Optional[str]
    status: str
    prompt_tokens: Optional[int]
    reply_tokens: Optional[int]
    created_at: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- projects
    def list_projects(self) -> List[Project]:
        with self._db() as c:
            rows = c.execute("""
                SELECT p.id, p.name, p.created_at,
                       COALESCE(SUM(ch.id IS NOT NULL AND ch.archived_at IS NULL), 0),
                       COALESCE(SUM(ch.archived_at IS NOT NULL), 0)
                FROM projects p LEFT JOIN chats ch ON ch.project_id = p.id
                GROUP BY p.id ORDER BY p.id = ? DESC, p.name COLLATE NOCASE""", (DEFAULT_PROJECT_ID,)).fetchall()
        return [Project(*r) for r in rows]

    def get_project(self, project_id: str) -> Project:
        for p in self.list_projects():
            if p.id == project_id:
                return p
        raise HistoryError("That project does not exist.", "no_such_project")

    def create_project(self, name: str) -> Project:
        if not isinstance(name, str):
            raise HistoryError("A project needs a name.", "bad_project_name")
        name = " ".join(name.split())
        if not name or len(name) > MAX_PROJECT_NAME:
            raise HistoryError(f"Project names are 1 to {MAX_PROJECT_NAME} characters.", "bad_project_name")
        pid = "prj_" + secrets.token_hex(8)
        with self._write_lock, self._db() as c:
            try:
                c.execute("INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)", (pid, name, _now()))
            except sqlite3.IntegrityError:
                raise HistoryError(f"A project called {name!r} already exists.", "project_exists") from None
        return self.get_project(pid)

    # ---------------------------------------------------------------- chats
    def create_chat(self, project_id: str, title: str = "New chat") -> ChatInfo:
        self.get_project(project_id)  # raises if it does not exist
        cid = "chat_" + secrets.token_hex(12)
        now = _now()
        with self._write_lock, self._db() as c:
            c.execute("INSERT INTO chats (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                      (cid, project_id, title[:MAX_TITLE], now, now))
        return self.get_chat(cid)

    def get_chat(self, chat_id: str) -> ChatInfo:
        with self._db() as c:
            row = c.execute("""
                SELECT ch.id, ch.project_id, ch.title, ch.created_at, ch.updated_at, ch.archived_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id)
                FROM chats ch WHERE ch.id = ?""", (chat_id,)).fetchone()
        if row is None:
            raise HistoryError("That chat does not exist.", "no_such_chat")
        return ChatInfo(*row)

    def list_chats(self, project_id: str, archived: bool = False) -> List[ChatInfo]:
        """Chats of ONE project, newest activity first. Archived ones only when asked."""
        self.get_project(project_id)
        with self._db() as c:
            rows = c.execute(f"""
                SELECT ch.id, ch.project_id, ch.title, ch.created_at, ch.updated_at, ch.archived_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id)
                FROM chats ch
                WHERE ch.project_id = ? AND ch.archived_at IS {'NOT ' if archived else ''}NULL
                ORDER BY ch.updated_at DESC, ch.id""", (project_id,)).fetchall()
        return [ChatInfo(*r) for r in rows]

    def messages(self, chat_id: str) -> List[Message]:
        self.get_chat(chat_id)
        with self._db() as c:
            rows = c.execute("""SELECT seq, role, content, model, status, prompt_tokens, reply_tokens, created_at
                                FROM messages WHERE chat_id = ? ORDER BY seq""", (chat_id,)).fetchall()
        return [Message(*r) for r in rows]

    def turns(self, chat_id: str) -> List[Turn]:
        """The transcript as context turns, oldest first (M1's context assembly)."""
        return [Turn(m.role, m.content) for m in self.messages(chat_id)]

    def append_exchange(self, chat_id: str, user_text: str, reply_text: str, model: str, status: str,
                        prompt_tokens: Optional[int] = None, reply_tokens: Optional[int] = None) -> None:
        """Store one message and its reply together, in one transaction."""
        if status not in STATUSES:
            raise HistoryError(f"unknown status {status!r}")
        now = _now()
        with self._write_lock, self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT archived_at, title, (SELECT COALESCE(MAX(seq), 0) FROM messages WHERE chat_id = ?) "
                                "FROM chats WHERE id = ?", (chat_id, chat_id)).fetchone()
                if row is None:
                    raise HistoryError("That chat does not exist.", "no_such_chat")
                archived_at, title, last = row
                if archived_at is not None:
                    raise HistoryError("This chat is archived. Restore it to continue.", "chat_archived")
                c.execute("INSERT INTO messages (chat_id, seq, role, content, model, status, created_at) "
                          "VALUES (?, ?, 'user', ?, NULL, 'complete', ?)", (chat_id, last + 1, user_text, now))
                c.execute("INSERT INTO messages (chat_id, seq, role, content, model, status, prompt_tokens, "
                          "reply_tokens, created_at) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?)",
                          (chat_id, last + 2, reply_text, model, status, prompt_tokens, reply_tokens, now))
                new_title = make_title(user_text) if last == 0 else title
                c.execute("UPDATE chats SET updated_at = ?, title = ? WHERE id = ?", (now, new_title, chat_id))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def set_archived(self, chat_id: str, archived: bool) -> ChatInfo:
        """Archive or unarchive. Changes one column; never removes a message."""
        self.get_chat(chat_id)
        with self._write_lock, self._db() as c:
            c.execute("UPDATE chats SET archived_at = ? WHERE id = ?", (_now() if archived else None, chat_id))
        return self.get_chat(chat_id)
