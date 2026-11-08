"""Versioned SQLite schema.

The schema version lives in SQLite's own `PRAGMA user_version`. Each entry in
MIGRATIONS moves the database up one version and runs in a single
transaction, so a failed migration leaves the previous version intact.

Rules for later milestones:
- Never edit a migration that has shipped; append a new one.
- A database newer than this code is refused rather than guessed at.

Versions so far:
  1 (M0)  `meta` key/value table and `launches`, one row per start and stop.
  2 (M1)  `model_probes`, the result of the tool-call probe per model digest.
  3 (M2)  `projects`, `chats`, `messages`: the chat history store.
  4 (M3)  `workspaces`, `access_policy`, `grants` (the access profile store),
          `receipts` (one per tool attempt), and `messages.task_id`.
  5 (M4)  Durable task state: `tasks`, `task_actions`, `task_events`,
          `task_checkpoints`, `task_artifacts`. Separate from chats and memory.
  6 (M5)  `pending_turns`: the message of a task that is still running, so a
          crash or restart can offer to resume it. One row per running task,
          removed when the exchange is stored or dropped, and with its task
          when tasks are purged.
  7 (M6)  `personal_memory` and `project_memory`, two memory stores with no
          link to chats; `messages.memory_ids` records which items a reply
          was given.
  8 (M7)  `memory_candidates`: notes a finished task suggests for memory,
          waiting for your review. A separate queue, never read by retrieval;
          an accepted candidate is written to its store, and once decided the
          candidate keeps no text (only a hash, to spot duplicates).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MIGRATIONS = (
    # version 1 (M0)
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE launches (
        id         INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        stopped_at TEXT,
        version    TEXT NOT NULL
    );
    INSERT INTO meta (key, value) VALUES ('created_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
    """,
    # version 2 (M1): results of the tool-call probe, one row per model digest.
    # A re-pulled model has a new digest, so it must pass the probe again.
    """
    CREATE TABLE model_probes (
        model      TEXT NOT NULL,
        digest     TEXT NOT NULL,
        passed     INTEGER NOT NULL CHECK (passed IN (0, 1)),
        stage      TEXT NOT NULL,
        detail     TEXT NOT NULL,
        ms         INTEGER NOT NULL,
        probed_at  TEXT NOT NULL,
        PRIMARY KEY (model, digest)
    );
    """,
    # version 3 (M2): the chat history store. Projects own chats, chats own
    # messages. Archiving sets archived_at and deletes nothing. There is no
    # delete path yet (docs/m2-history.md: the deletion contract is undecided).
    """
    CREATE TABLE projects (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        -- uniqueness key made in Python (history/store.py name_key): Unicode NFKC
        -- plus full case folding. SQLite's NOCASE only folds ASCII A-Z, so it
        -- would let "École" and "école" both exist.
        name_key    TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE chats (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id),
        title       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE INDEX chats_by_project ON chats (project_id, archived_at, updated_at);
    CREATE TABLE messages (
        id            INTEGER PRIMARY KEY,
        chat_id       TEXT NOT NULL REFERENCES chats(id),
        seq           INTEGER NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content       TEXT NOT NULL,
        model         TEXT,
        status        TEXT NOT NULL DEFAULT 'complete'
                      CHECK (status IN ('complete', 'stopped', 'interrupted', 'timeout', 'error')),
        prompt_tokens INTEGER,
        reply_tokens  INTEGER,
        created_at    TEXT NOT NULL,
        UNIQUE (chat_id, seq)
    );
    INSERT INTO projects (id, name, name_key, created_at)
        VALUES ('prj_general', 'General', 'general', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
    """,
    # version 4 (M3): workspaces, access profiles, grants and receipts. Each
    # project has at most one workspace folder and one profile. policy_version
    # goes up whenever the workspace or profile changes, which makes every
    # approval given under the old policy stale. Receipts are their own store:
    # never memory, never shown to the model.
    """
    CREATE TABLE workspaces (
        project_id  TEXT PRIMARY KEY REFERENCES projects(id),
        root        TEXT NOT NULL,
        profile     TEXT NOT NULL CHECK (profile IN ('workspace_read', 'ask_before_read')),
        attached_at TEXT NOT NULL
    );
    CREATE TABLE access_policy (
        project_id  TEXT PRIMARY KEY REFERENCES projects(id),
        version     INTEGER NOT NULL
    );
    CREATE TABLE grants (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id),
        capability  TEXT NOT NULL,
        policy_version INTEGER NOT NULL,
        granted_at  TEXT NOT NULL,
        revoked_at  TEXT
    );
    CREATE INDEX grants_active ON grants (project_id, capability, revoked_at);
    CREATE TABLE receipts (
        receipt_id  TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL,
        action_id   TEXT NOT NULL,
        project_id  TEXT,
        chat_id     TEXT,
        action      TEXT NOT NULL,
        decision    TEXT NOT NULL CHECK (decision IN ('allowed', 'denied', 'failed', 'uncertain')),
        started_at  TEXT NOT NULL,
        record      TEXT NOT NULL
    );
    CREATE INDEX receipts_by_task ON receipts (task_id);
    CREATE INDEX receipts_by_chat ON receipts (chat_id, started_at);
    ALTER TABLE messages ADD COLUMN task_id TEXT;
    """,
    # version 5 (M4): durable task state. Its own store, like receipts: the
    # chat history never reads it and it never holds message or file text.
    # Every row is metadata and evidence references, redacted before writing.
    """
    CREATE TABLE tasks (
        task_id     TEXT PRIMARY KEY,
        project_id  TEXT,
        chat_id     TEXT,
        model       TEXT,
        status      TEXT NOT NULL CHECK (status IN ('requested', 'started', 'completed', 'failed', 'cancelled', 'unknown')),
        code        TEXT,
        created_at  TEXT NOT NULL,
        started_at  TEXT,
        ended_at    TEXT,
        limits      TEXT NOT NULL
    );
    CREATE INDEX tasks_by_chat ON tasks (chat_id, created_at);
    CREATE TABLE task_actions (
        action_id   TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        step        INTEGER NOT NULL,
        tool        TEXT NOT NULL,
        resource    TEXT,
        status      TEXT NOT NULL CHECK (status IN ('requested', 'started', 'completed', 'denied', 'failed', 'cancelled', 'unknown')),
        code        TEXT,
        receipt_id  TEXT,
        requested_at TEXT NOT NULL,
        ended_at    TEXT
    );
    CREATE INDEX task_actions_by_task ON task_actions (task_id, requested_at);
    CREATE TABLE task_events (
        task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        at          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        action_id   TEXT,
        data        TEXT NOT NULL,
        PRIMARY KEY (task_id, seq)
    );
    CREATE TABLE task_checkpoints (
        task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        step        INTEGER NOT NULL,
        at          TEXT NOT NULL,
        state       TEXT NOT NULL,
        PRIMARY KEY (task_id, step)
    );
    CREATE TABLE task_artifacts (
        artifact_id TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        sha256      TEXT NOT NULL,
        body        TEXT NOT NULL
    );
    CREATE INDEX task_artifacts_by_task ON task_artifacts (task_id);
    """,
    # version 6 (M5): the message of a running task, kept only until its
    # exchange is stored (or dropped), so an interrupted task can be resumed
    # after a crash or restart. It is chat content, written by the history
    # store, and it goes with its task when tasks are purged.
    """
    CREATE TABLE pending_turns (
        task_id     TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
        chat_id     TEXT NOT NULL REFERENCES chats(id),
        user_text   TEXT NOT NULL,
        model       TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX pending_turns_by_chat ON pending_turns (chat_id);
    """,
    # version 7 (M6): two memory stores, separate from chats and tasks. No
    # column here points at a chat or a message, so nothing that happens to a
    # transcript reaches memory. A reply records the ids of the memory items
    # it was given (messages.memory_ids) as a plain reference: deleting an
    # item later leaves that reference, and the old reply, as they were.
    """
    CREATE TABLE personal_memory (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('fact', 'preference')),
        text        TEXT NOT NULL,
        scope       TEXT NOT NULL,
        source      TEXT NOT NULL,
        approval    TEXT NOT NULL CHECK (approval IN ('approved', 'proposed', 'rejected')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE project_memory (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id),
        kind        TEXT NOT NULL CHECK (kind IN ('decision', 'state', 'lesson')),
        text        TEXT NOT NULL,
        source      TEXT NOT NULL,
        approval    TEXT NOT NULL CHECK (approval IN ('approved', 'proposed', 'rejected')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX project_memory_by_project ON project_memory (project_id, created_at);
    ALTER TABLE messages ADD COLUMN memory_ids TEXT;
    """,
    # version 8 (M7)
    """
    CREATE TABLE memory_candidates (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id),
        chat_id     TEXT,
        task_id     TEXT,
        store       TEXT NOT NULL CHECK (store IN ('personal', 'project')),
        kind        TEXT NOT NULL,
        text        TEXT NOT NULL,
        quote       TEXT NOT NULL,
        text_hash   TEXT NOT NULL,
        scope       TEXT NOT NULL,
        basis       TEXT NOT NULL CHECK (basis IN ('message', 'reply')),
        evidence    TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
        edited      INTEGER NOT NULL DEFAULT 0,
        memory_id   TEXT,
        created_at  TEXT NOT NULL,
        decided_at  TEXT
    );
    CREATE INDEX memory_candidates_by_project ON memory_candidates (project_id, status, created_at);
    """,
    # version 9 (M9): the folder a task worked in, so resuming it can check the
    # project still uses that folder before anything runs again; and the two
    # coding profiles. SQLite cannot change a CHECK in place, so the small
    # workspaces table is rebuilt with its rows copied unchanged.
    """
    ALTER TABLE tasks ADD COLUMN workspace TEXT;
    CREATE TABLE workspaces_m9 (
        project_id  TEXT PRIMARY KEY REFERENCES projects(id),
        root        TEXT NOT NULL,
        profile     TEXT NOT NULL CHECK (profile IN ('workspace_read', 'ask_before_read',
                                                     'workspace_code', 'code_ask_commands')),
        attached_at TEXT NOT NULL
    );
    INSERT INTO workspaces_m9 (project_id, root, profile, attached_at)
        SELECT project_id, root, profile, attached_at FROM workspaces;
    DROP TABLE workspaces;
    ALTER TABLE workspaces_m9 RENAME TO workspaces;
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed, mode 0600) and migrate the database."""
    path = Path(path)
    if not path.exists():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(str(path), isolation_level=None)  # we manage transactions explicitly
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema is version {current}, but this runtime only knows up to {SCHEMA_VERSION}; "
            "refusing to open it with older code"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        # executescript runs its statements outside Python's transaction handling,
        # so the BEGIN/COMMIT here is what makes each migration all-or-nothing.
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def record_launch(conn: sqlite3.Connection, version: str) -> int:
    cur = conn.execute("INSERT INTO launches (started_at, version) VALUES (?, ?)", (_now(), version))
    return int(cur.lastrowid)


def record_stop(conn: sqlite3.Connection, launch_id: int) -> None:
    conn.execute("UPDATE launches SET stopped_at = ? WHERE id = ?", (_now(), launch_id))


def launch_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0]


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def save_probe(conn: sqlite3.Connection, model: str, digest: str, passed: bool, stage: str, detail: str, ms: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO model_probes (model, digest, passed, stage, detail, ms, probed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (model, digest, 1 if passed else 0, stage, detail[:500], ms, _now()),
    )


def load_probes(conn: sqlite3.Connection) -> dict:
    """{(model, digest): {...}} for every stored probe result."""
    rows = conn.execute("SELECT model, digest, passed, stage, detail, ms, probed_at FROM model_probes").fetchall()
    return {(r[0], r[1]): {"passed": bool(r[2]), "stage": r[3], "detail": r[4], "ms": r[5], "probed_at": r[6]}
            for r in rows}
