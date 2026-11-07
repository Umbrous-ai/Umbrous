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
Task state arrives in M4.
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
