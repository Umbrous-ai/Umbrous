"""Versioned SQLite schema.

The schema version lives in SQLite's own `PRAGMA user_version`. Each entry in
MIGRATIONS moves the database up one version and runs in a single
transaction, so a failed migration leaves the previous version intact.

Rules for later milestones:
- Never edit a migration that has shipped; append a new one.
- A database newer than this code is refused rather than guessed at.

M0's schema is deliberately tiny: a key/value `meta` table and a `launches`
table that records each start and stop of the runtime. Chats arrive in M2,
receipts in M3, task state in M4.
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
