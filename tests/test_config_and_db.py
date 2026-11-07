"""Configuration refuses non-loopback binds; the schema is versioned and migrations are safe."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umbrous.config import ConfigError, load_config
from umbrous.persistence import db


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UMBROUS_PORT", None)
            c = load_config(self.home)
        self.assertEqual((c.host, c.port), ("127.0.0.1", 8417))

    def test_non_loopback_hosts_are_refused(self):
        for host in ("0.0.0.0", "192.168.1.20", "::", "::1", "localhost", "example.com", "127.0.0.2"):
            with self.subTest(host=host), self.assertRaises(ConfigError):
                load_config(self.home, {"host": host})

    def test_non_loopback_host_in_config_file_is_refused(self):
        (self.home / "config.json").write_text(json.dumps({"host": "0.0.0.0"}))
        with self.assertRaises(ConfigError):
            load_config(self.home)

    def test_unknown_keys_and_bad_ports_are_refused(self):
        for bad in ({"bind_all": True}, {"port": 70000}, {"port": "80"}, {"port": True}, {"log_level": "LOUD"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_config(self.home, bad)

    def test_file_then_env_then_flags(self):
        (self.home / "config.json").write_text(json.dumps({"port": 9001, "log_level": "debug"}))
        with mock.patch.dict(os.environ, {"UMBROUS_PORT": "9002"}):
            self.assertEqual(load_config(self.home).port, 9002)
            self.assertEqual(load_config(self.home, {"port": 9003}).port, 9003)
            self.assertEqual(load_config(self.home).log_level, "DEBUG")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "umbrous.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_database_is_migrated_and_private(self):
        conn = db.connect(self.path)
        self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
        self.assertIsNotNone(db.get_meta(conn, "created_at"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        conn.close()

    def test_reopening_is_idempotent_and_keeps_data(self):
        conn = db.connect(self.path)
        lid = db.record_launch(conn, "x")
        db.record_stop(conn, lid)
        conn.close()
        conn = db.connect(self.path)
        self.assertEqual(db.launch_count(conn), 1)
        conn.close()

    def test_newer_database_is_refused(self):
        conn = sqlite3.connect(str(self.path))
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(db.SchemaError):
            db.connect(self.path)

    def test_failed_migration_rolls_back(self):
        broken = ("CREATE TABLE a (x); CREATE TABLE a (x);",)  # second statement fails
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        with mock.patch.object(db, "MIGRATIONS", broken), mock.patch.object(db, "SCHEMA_VERSION", 1):
            with self.assertRaises(sqlite3.Error):
                db.migrate(conn)
        self.assertEqual(db.schema_version(conn), 0)
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone())
        conn.close()


if __name__ == "__main__":
    unittest.main()
