"""Connection cleanup must not accidentally commit partial application writes."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umbrous.persistence import db
from umbrous.persistence.session import session


class SessionTests(unittest.TestCase):
    def test_failed_transaction_is_rolled_back_and_connection_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.db"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with session(path, secure_delete=True) as conn:
                    self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)
                    conn.execute("CREATE TABLE sample (value TEXT)")
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("INSERT INTO sample VALUES ('partial')")
                    raise RuntimeError("interrupted")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
            with session(path) as reopened:
                self.assertEqual(reopened.execute("SELECT * FROM sample").fetchall(), [])

    def test_failed_migration_closes_initialization_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.db"
            conn = sqlite3.connect(str(path), isolation_level=None)
            with mock.patch.object(db, "open_connection", return_value=conn), \
                    mock.patch.object(db, "migrate", side_effect=db.SchemaError("future schema")):
                with self.assertRaises(db.SchemaError):
                    db.connect(path)
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
