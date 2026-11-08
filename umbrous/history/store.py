"""Chat History / Session Store (M2).

Its own logical store, in the `projects`, `chats` and `messages` tables of the
runtime's SQLite file. Reset also deletes rows there; nothing here touches
other stores: memory (M6) and task records/receipts (M3, M4) live in their
own tables, and no operation in this module reaches into them.

What each word means here:
  transcript   the messages of one chat, exactly as sent and received
  archived     a chat with archived_at set: hidden from the normal list,
               read-only, every message kept. Unarchive clears archived_at.
  deleted      removed by the reset service, including its messages. Project
               deletion also removes project-owned data, never source files.
               See docs/deletion.md.

Chats may belong to an explicit project or an isolated session scope.
Session scopes reuse the access storage but are never listed as projects.

Pending turns (M5): while a task runs, its message waits in `pending_turns`,
not in the transcript. If the runtime is killed mid-task, the message is still
there after the restart, so the interrupted task can be resumed without asking
you to type it again. The row is removed when the exchange is stored, when the
reply ends with nothing to keep (the M2 rule: the message is not kept), when
you send a new message in that chat instead of resuming, and with its task
when tasks are purged. It is never sent to a model as context.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from umbrous.timeutil import utc_now as _now
from umbrous.persistence.session import session
from pathlib import Path
from typing import ContextManager, List, Optional

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



def name_key(name: str) -> str:
    """What makes two project names "the same": Unicode-normalised (NFKC) and
    fully case-folded, so "École", "école" and "E\u0301cole" all collide."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


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


CHAT_ACCESS = ("workspace_read", "ask_before_read", "workspace_code", "code_ask_commands")


def _check_access(access: Optional[str]) -> None:
    if access is not None and access not in CHAT_ACCESS:
        raise HistoryError(f"Unknown access {access!r}.", "bad_access")


@dataclass(frozen=True)
class ChatInfo:
    id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str
    archived_at: Optional[str]
    messages: int = 0
    # the chat's own access (a workspace profile), or None for the project's profile
    access: Optional[str] = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def as_dict(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "archived": self.archived, "archived_at": self.archived_at, "messages": self.messages,
                "access": self.access}


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
    task_id: Optional[str] = None   # M3: links a reply to its tool receipts
    memory_ids: Optional[List[str]] = None   # M6: ids of the memory items this reply was given

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class PendingTurn:
    task_id: str
    chat_id: str
    user_text: str
    model: str
    created_at: str


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()

    def _db(self) -> ContextManager[sqlite3.Connection]:
        return session(self.db_path)

    # ---------------------------------------------------------------- projects
    def list_projects(self, include_sessions: bool = False) -> List[Project]:
        with self._db() as c:
            rows = c.execute("""
                SELECT p.id, p.name, p.created_at,
                       COALESCE(SUM(ch.id IS NOT NULL AND ch.archived_at IS NULL), 0),
                       COALESCE(SUM(ch.archived_at IS NOT NULL), 0)
                FROM projects p LEFT JOIN chats ch ON ch.project_id = p.id
                WHERE (? OR p.is_session = 0)
                GROUP BY p.id ORDER BY p.id = ? DESC, p.name_key""", (include_sessions, DEFAULT_PROJECT_ID,)).fetchall()
        return [Project(*r) for r in rows]

    def get_project(self, project_id: str) -> Project:
        for p in self.list_projects(include_sessions=True):
            if p.id == project_id:
                return p
        raise HistoryError("That project does not exist.", "no_such_project")

    def create_project(self, name: str) -> Project:
        if not isinstance(name, str):
            raise HistoryError("A project needs a name.", "bad_project_name")
        name = unicodedata.normalize("NFC", " ".join(name.split()))
        if not name or len(name) > MAX_PROJECT_NAME:
            raise HistoryError(f"Project names are 1 to {MAX_PROJECT_NAME} characters.", "bad_project_name")
        pid = "prj_" + secrets.token_hex(8)
        with self._write_lock, self._db() as c:
            try:
                c.execute("INSERT INTO projects (id, name, name_key, created_at) VALUES (?, ?, ?, ?)",
                          (pid, name, name_key(name), _now()))
            except sqlite3.IntegrityError:
                raise HistoryError(f"A project called {name!r} already exists.", "project_exists") from None
        return self.get_project(pid)

    # ---------------------------------------------------------------- chats
    def create_chat(self, project_id: Optional[str] = None, title: str = "New chat", access: Optional[str] = None) -> ChatInfo:
        if project_id is not None:
            self.get_project(project_id)  # explicit missing projects remain an error
            with self._db() as c:
                if c.execute("SELECT is_session FROM projects WHERE id=?", (project_id,)).fetchone()[0]:
                    raise HistoryError("A standalone session cannot contain another chat.", "bad_project_id")
        _check_access(access)
        cid = "chat_" + secrets.token_hex(12)
        now = _now()
        with self._write_lock, self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            if project_id is None:
                project_id = "prj_session_" + secrets.token_hex(12)
                c.execute("INSERT INTO projects (id, name, name_key, created_at, is_session) VALUES (?, ?, ?, ?, 1)",
                          (project_id, "Standalone chat", project_id, now))
            c.execute("INSERT INTO chats (id, project_id, title, created_at, updated_at, access) VALUES (?, ?, ?, ?, ?, ?)",
                      (cid, project_id, title[:MAX_TITLE], now, now, access))
            c.execute("COMMIT")
        return self.get_chat(cid)

    def set_access(self, chat_id: str, access: Optional[str]) -> ChatInfo:
        """The chat's own access (a workspace profile), or None for the project's.
        The gate reads it at every call, so a change applies from the next action."""
        _check_access(access)
        self.get_chat(chat_id)
        with self._write_lock, self._db() as c:
            c.execute("UPDATE chats SET access = ? WHERE id = ?", (access, chat_id))
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: str) -> ChatInfo:
        with self._db() as c:
            row = c.execute("""
                SELECT ch.id, ch.project_id, ch.title, ch.created_at, ch.updated_at, ch.archived_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id), ch.access
                FROM chats ch WHERE ch.id = ?""", (chat_id,)).fetchone()
        if row is None:
            raise HistoryError("That chat does not exist.", "no_such_chat")
        return ChatInfo(*row)

    def list_chats(self, project_id: Optional[str] = None, archived: bool = False) -> List[ChatInfo]:
        """Chats of one explicit project, or standalone chats when omitted."""
        if project_id is not None:
            self.get_project(project_id)
        with self._db() as c:
            rows = c.execute(f"""
                SELECT ch.id, ch.project_id, ch.title, ch.created_at, ch.updated_at, ch.archived_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id), ch.access
                FROM chats ch
                WHERE (ch.project_id = ? OR (? IS NULL AND ch.project_id IN (SELECT id FROM projects WHERE is_session = 1))) AND ch.archived_at IS {'NOT ' if archived else ''}NULL
                ORDER BY ch.updated_at DESC, ch.id""", (project_id, project_id)).fetchall()
        return [ChatInfo(*r) for r in rows]

    def messages(self, chat_id: str) -> List[Message]:
        self.get_chat(chat_id)
        with self._db() as c:
            rows = c.execute("""SELECT seq, role, content, model, status, prompt_tokens, reply_tokens, created_at,
                                       task_id, memory_ids FROM messages WHERE chat_id = ? ORDER BY seq""",
                             (chat_id,)).fetchall()
        return [Message(*r[:9], memory_ids=json.loads(r[9]) if r[9] else None) for r in rows]

    def turns(self, chat_id: str) -> List[Turn]:
        """The transcript as context turns, oldest first (M1's context assembly)."""
        return [Turn(m.role, m.content) for m in self.messages(chat_id)]

    def append_exchange(self, chat_id: str, user_text: str, reply_text: str, model: str, status: str,
                        prompt_tokens: Optional[int] = None, reply_tokens: Optional[int] = None,
                        task_id: Optional[str] = None, memory_ids: Optional[List[str]] = None) -> None:
        """Store one message and its reply together, in one transaction.
        `memory_ids` (M6) is a reference only: the memory items stay in their
        own store, and nothing here reads or changes them."""
        if status not in STATUSES:
            raise HistoryError(f"unknown status {status!r}")
        if not reply_text.strip():  # an empty reply is an error, never a saved answer
            raise HistoryError("A reply with no text is not saved.", "empty_reply")
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
                c.execute("INSERT INTO messages (chat_id, seq, role, content, model, status, created_at, task_id) "
                          "VALUES (?, ?, 'user', ?, NULL, 'complete', ?, ?)", (chat_id, last + 1, user_text, now, task_id))
                c.execute("INSERT INTO messages (chat_id, seq, role, content, model, status, prompt_tokens, "
                          "reply_tokens, created_at, task_id, memory_ids) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
                          (chat_id, last + 2, reply_text, model, status, prompt_tokens, reply_tokens, now, task_id,
                           json.dumps(memory_ids) if memory_ids else None))
                new_title = make_title(user_text) if last == 0 else title
                c.execute("UPDATE chats SET updated_at = ?, title = ? WHERE id = ?", (now, new_title, chat_id))
                if task_id:   # M5: the message is in the transcript now, so it is no longer pending
                    c.execute("DELETE FROM pending_turns WHERE task_id = ?", (task_id,))
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

    # ---------------------------------------------------------------- pending turns (M5)
    def begin_pending(self, task_id: str, chat_id: str, user_text: str, model: str) -> None:
        """Keep a running task's message until its exchange is stored or dropped."""
        with self._write_lock, self._db() as c:
            c.execute("INSERT INTO pending_turns (task_id, chat_id, user_text, model, created_at) VALUES (?, ?, ?, ?, ?)",
                      (task_id, chat_id, user_text, model, _now()))

    def pending(self, task_id: str) -> Optional[PendingTurn]:
        with self._db() as c:
            row = c.execute("SELECT task_id, chat_id, user_text, model, created_at FROM pending_turns WHERE task_id = ?",
                            (task_id,)).fetchone()
        return PendingTurn(*row) if row else None

    def pending_for_chat(self, chat_id: str) -> List[PendingTurn]:
        with self._db() as c:
            rows = c.execute("SELECT task_id, chat_id, user_text, model, created_at FROM pending_turns "
                             "WHERE chat_id = ? ORDER BY created_at", (chat_id,)).fetchall()
        return [PendingTurn(*r) for r in rows]

    def drop_pending(self, task_id: str) -> bool:
        """Remove a pending message (its reply ended with nothing to keep, or it was superseded)."""
        with self._write_lock, self._db() as c:
            c.execute("PRAGMA secure_delete = ON")
            return c.execute("DELETE FROM pending_turns WHERE task_id = ?", (task_id,)).rowcount > 0

    def move_pending(self, old_task_id: str, new_task_id: str) -> bool:
        """A resumed task takes over the interrupted task's message."""
        with self._write_lock, self._db() as c:
            return c.execute("UPDATE pending_turns SET task_id = ? WHERE task_id = ?",
                             (new_task_id, old_task_id)).rowcount > 0

    def has_reply(self, task_id: str) -> bool:
        """Was a reply stored for this task? (A crash can come after the reply was saved.)"""
        with self._db() as c:
            return c.execute("SELECT 1 FROM messages WHERE task_id = ? AND role = 'assistant' LIMIT 1",
                             (task_id,)).fetchone() is not None
