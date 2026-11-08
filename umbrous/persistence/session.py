"""Short-lived SQLite sessions with explicit transaction ownership.

Callers issue BEGIN/COMMIT themselves. Closing a session never commits an
unfinished transaction, including when an exception escapes the operation.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


def open_connection(path: Path, *, foreign_keys: bool = True, secure_delete: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        if secure_delete:
            conn.execute("PRAGMA secure_delete = ON")
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def session(path: Path, *, foreign_keys: bool = True, secure_delete: bool = False) -> Iterator[sqlite3.Connection]:
    conn = open_connection(path, foreign_keys=foreign_keys, secure_delete=secure_delete)
    try:
        yield conn
    finally:
        conn.close()
