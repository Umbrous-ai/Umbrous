"""Shared test helpers: a real runtime on a random loopback port in a temp home."""

from __future__ import annotations

import http.client
import io
import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from umbrous.api.server import make_server
from umbrous.config import load_config, make_private_dir
from umbrous.logging_setup import setup_logging
from umbrous.persistence import db
from umbrous.receipts.redaction import default_redactor


def project_fixture_db(path):
    """Explicit General project for legacy project-scoped acceptance tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = db.connect(path)
    if fresh:
        conn.execute("INSERT OR IGNORE INTO projects (id, name, name_key, created_at) VALUES ('prj_general', 'General', 'general', '2026-01-01T00:00:00Z')")
    return conn


class RunningRuntime:
    """Context manager: starts the runtime exactly as `serve` does, minus the browser."""

    def __init__(self, home: Optional[Path] = None, **overrides) -> None:
        """Pass `home` to start again on the same data (a restart); it is then not deleted."""
        self.overrides = overrides
        self._given_home = home
        self._aliases: Dict[str, str] = {}

    def __enter__(self) -> "RunningRuntime":
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._given_home or Path(self._tmp.name) / "home"
        make_private_dir(self.home)
        self.config = load_config(self.home, {"port": 0, "codex_enabled": False, "claude_enabled": False, "grok_enabled": False, **self.overrides})
        self.log_stream = io.StringIO()
        setup_logging(self.config, stream=self.log_stream)
        self.conn = project_fixture_db(self.config.db_path)
        db.record_launch(self.conn, "test")
        self.server, self.runtime = make_server(self.config, db.schema_version(self.conn), db.launch_count(self.conn))
        self.policy = self.runtime.policy
        default_redactor.register(self.policy.session_token)
        default_redactor.register(self.policy.launch_code)
        self.port = self.policy.port
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.conn.close()
        self._tmp.cleanup()

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None, host: Optional[str] = None,
                token: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
        """Send a request with full control over headers (including Host)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if host is not None else self.host)
            if token:
                conn.putheader("X-Umbrous-Token", token)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def post_json(self, path: str, obj: dict, token: Optional[str] = None,
                  headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        h = {"Content-Type": "application/json", "Origin": self.origin, **(headers or {})}
        return self.request("POST", path, headers=h, body=json.dumps(obj).encode(),
                            token=token or self.policy.session_token)

    def new_chat(self, project_id: str = "prj_general") -> str:
        status, _, body = self.post_json("/api/chats", {"project_id": project_id})
        assert status == 201, (status, body)
        cid = json.loads(body)["chat"]["id"]
        return cid

    def get_json(self, path: str) -> Tuple[int, dict]:
        return self.json("GET", path, token=self.policy.session_token)

    def chat(self, chat_id: str, model: str, message: str, create: bool = True) -> list:
        """Send a chat message and return the streamed events.

        Since M2 chats are created by the runtime. A name that is not a real
        chat id yet (tests use readable ones like "chat_testtest01") gets a new
        chat in General on first use, and the same chat after that, unless
        create=False (to test an unknown id)."""
        if create and chat_id not in self._aliases and not re.fullmatch(r"chat_[0-9a-f]{24}", chat_id):
            self._aliases[chat_id] = self.new_chat()
        chat_id = self._aliases.get(chat_id, chat_id)
        status, _, body = self.post_json("/api/chat", {"chat_id": chat_id, "model": model, "message": message})
        assert status == 200, status
        return [json.loads(line) for line in body.decode().splitlines() if line.strip()]

    def json(self, *args, **kwargs) -> Tuple[int, dict]:
        status, _, body = self.request(*args, **kwargs)
        return status, json.loads(body)
