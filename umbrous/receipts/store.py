"""The receipt store (M3): one receipt per tool attempt, kept apart from memory.

Only `Receipt.to_record()` output is stored (redacted, references only, no
content). Receipts are evidence for you: they are never given to the model.
Retention: kept until you purge them (`bin/umbrous purge receipts`); no
automatic expiry until the retention decision is made (docs/receipts.md).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from umbrous.receipts.model import Receipt


class ReceiptStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5)
        try:
            yield conn
        finally:
            conn.close()

    def add(self, receipt: Receipt, project_id: Optional[str], chat_id: Optional[str]) -> dict:
        record = receipt.to_record()
        with self._lock, self._db() as c:
            c.execute("INSERT INTO receipts (receipt_id, task_id, action_id, project_id, chat_id, action, decision, "
                      "started_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (record["receipt_id"], record["task_id"], record["action_id"], project_id, chat_id,
                       record["action"], record["decision"], record["started_at"], json.dumps(record)))
        return record

    def _query(self, where: str, args: tuple, limit: int) -> List[dict]:
        with self._db() as c:
            rows = c.execute(f"SELECT record FROM receipts WHERE {where} ORDER BY started_at, receipt_id LIMIT ?",
                             args + (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def for_task(self, task_id: str, limit: int = 500) -> List[dict]:
        return self._query("task_id = ?", (task_id,), limit)

    def for_chat(self, chat_id: str, limit: int = 2000) -> List[dict]:
        return self._query("chat_id = ?", (chat_id,), limit)

    def latest(self, limit: int = 20) -> List[dict]:
        with self._db() as c:
            rows = c.execute("SELECT record FROM receipts ORDER BY started_at DESC, receipt_id DESC LIMIT ?",
                             (limit,)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def count(self, before: Optional[str] = None) -> int:
        with self._db() as c:
            if before:
                return c.execute("SELECT COUNT(*) FROM receipts WHERE started_at < ?", (before,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def purge(self, before: Optional[str] = None) -> int:
        """Delete receipts (all, or those started before an ISO date). Only `bin/umbrous purge receipts` calls this."""
        with self._lock, self._db() as c:
            c.execute("PRAGMA secure_delete = ON")  # overwrite freed pages, so deleted rows do not linger in the file
            if before:
                n = c.execute("DELETE FROM receipts WHERE started_at < ?", (before,)).rowcount
            else:
                n = c.execute("DELETE FROM receipts").rowcount
        return n
