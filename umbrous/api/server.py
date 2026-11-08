"""The loopback HTTP server: how the browser page reaches Python.

    browser page ──HTTP──▶ 127.0.0.1:<port> ──▶ Handler ──▶ evaluate() ──▶ route
                                                             (security.py)

Python's standard-library `http.server` is enough: a few static files and
small JSON endpoints. Nothing here is reachable from another machine because the
socket is bound to 127.0.0.1 only.

Routes (everything under /api/ needs the session credential, see security.py):
    GET  /                    the prototype page (prototypes/umbrous-harness.html)
    GET  /runtime-bridge.js   connects the page to this API (M0)
    GET  /chat.js             real chat in the page (M1)
    POST /api/session/claim   trade the one-time launch code for the session credential
    GET  /api/health          runtime status, shown in the Details panel
    GET  /api/models          Ollama status, installed models, probe results, exposure check (M1)
    POST /api/models/probe    run the tool-call probe on one model (M1)
    POST /api/chat            send a message; the reply streams back as JSON lines (M1).
                              {"approvals": true} only from a client that shows approval cards (M3)
    POST /api/chat/cancel     stop a reply that is streaming (M1)
    GET  /api/projects                       projects with chat counts (M2)
    POST /api/projects        {name}         add a project (M2)
    GET  /api/chats?project_id=…&archived=1  one project's chats, archived only when asked (M2)
    POST /api/chats           {project_id}   start a chat in a project (M2)
    GET  /api/chats/<id>                     one chat and its transcript (M2)
    POST /api/chats/<id>/archive             hide it, read-only; deletes nothing (M2)
    POST /api/chats/<id>/unarchive           bring it back (M2)
    POST /api/chats/<id>/access {access}     the chat's own access (a workspace profile), null = the project's
There is deliberately no route that deletes a chat or project (docs/m2-history.md); chats
and memory go only through the reset below, which shows the exact items first (M8).
    GET  /api/projects/<id>/access                  the access view: workspace, profile, grants (M3)
    POST /api/projects/<id>/workspace {root, profile}  attach a workspace folder (M3)
    POST /api/projects/<id>/workspace/detach        forget the folder and revoke its grants (M3)
    POST /api/projects/<id>/profile {profile}       change the access profile (M3)
    POST /api/projects/<id>/grants/<grant>/revoke   revoke a project grant (M3)
    POST /api/approvals/<id> {choice}               answer an approval card: once | grant | deny (M3)
    GET  /api/receipts?chat_id=… | task_id=…        receipts, for inspection (M3)
    GET  /api/tasks?chat_id=…                       a chat's tasks with their reports (M4)
    GET  /api/tasks/<tsk_id>                        one task: actions, event log, checkpoints, report (M4)
                                                    and whether it can be resumed (M5: "resume")
    POST /api/chat {chat_id, model, resume_task}    resume an interrupted task under the current settings (M5)
    GET  /api/memory?project_id=…                   personal memory, and that project's memory (M6)
    POST /api/memory {store, text, kind, project_id | scope}   add an item (M6)
    POST /api/memory/<mem_id> {text, kind, scope}   edit an item (M6)
    POST /api/memory/<mem_id>/delete                delete an item; transcripts are not touched (M6)
    POST /api/chat {…, use_memory: false}           a reply without memory, to compare (M6)
    GET  /api/memory/candidates?project_id=…&status=…   suggestions waiting for review (M7; status pending,
                                                    accepted, rejected or all)
    POST /api/memory/candidates/<cand_id> {text, kind, store, scope}   edit a waiting suggestion (M7)
    POST /api/memory/candidates/<cand_id>/accept {…}  check it again and write it to its store (M7)
    POST /api/memory/candidates/<cand_id>/reject    reject it; its text is not kept (M7)
    POST /api/reset/preview {selection}             the exact chats and memory items a reset would delete,
                                                    what stays, and a preview_id; stores nothing (M8)
    POST /api/reset {selection, preview_id}         delete exactly that preview, or nothing (409
                                                    preview_changed with the new preview) (M8)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from umbrous import MILESTONE, __version__
from umbrous.api.history_routes import HistoryRoutes
from umbrous.api.access_routes import AccessRoutes
from umbrous.api.memory_routes import MemoryRoutes
from umbrous.api.task_routes import TaskRoutes
from umbrous.api.model_routes import ModelRoutes
from umbrous.api.contracts import (ACCESS_PATH, APPROVAL_PATH, ACCESS_STATUS,
                                   MEMORY_STATUS, HISTORY_STATUS, BASE_HEADERS)
from umbrous.api.security import CLAIM_PATH, LAUNCH_HEADER, AccessPolicy, evaluate
from umbrous.config import Config, ensure_loopback
from umbrous.logging_setup import log_event
from umbrous.model.exposure import ExposureReport, check_exposure
from umbrous.model.router import ModelRouter
from umbrous.model.factory import create_router
from umbrous.receipts.redaction import default_redactor
from umbrous.gate.access import AccessError, AccessStore
from umbrous.gate.gate import Gate
from umbrous.history.store import HistoryError, HistoryStore
from umbrous.memory.review import Review
from umbrous.memory.store import Memory, MemoryError_
from umbrous.receipts.store import ReceiptStore
from umbrous.reset import Reset
from umbrous.runloop.coordination import ChatLocks, RunRegistry
from umbrous.tools.run import CommandRegistry
from umbrous.tasks.recover import recover
from umbrous.tasks.store import TaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = REPO_ROOT / "prototypes" / "umbrous-harness.html"
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
STATIC_FILES = {"/runtime-bridge.js": WEB_DIR / "runtime-bridge.js",
                "/chat.js": WEB_DIR / "chat.js",
                "/model-controls.js": WEB_DIR / "model-controls.js"}
MAX_BODY = 256 * 1024  # a chat message is capped at 32,000 characters (up to ~6 bytes each as JSON)

log = logging.getLogger("umbrous.api")


_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def content_security_policy(html: str) -> str:
    """Allow only this page's own inline script (by hash) and same-origin files.

    The prototype keeps its inline <script>, so instead of 'unsafe-inline' the
    policy pins that exact script by its SHA-256. Edit the script and the hash
    is recomputed at the next launch.
    """
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode() + "'"
        for m in _INLINE_SCRIPT.finditer(html)
    ]
    return "; ".join([
        "default-src 'none'",
        "script-src 'self' " + " ".join(hashes),
        "style-src 'self' 'unsafe-inline'",  # the prototype uses inline styles; scripts are what matter here
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


class Runtime:
    """State shared by request handlers."""

    def __init__(self, config: Config, policy: AccessPolicy, schema_version: int, launches: int,
                 router: Optional[ModelRouter] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.policy = policy
        self.schema_version = schema_version
        self.launches = launches
        self._clock = clock
        self._started_mono = clock()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.page = PAGE_PATH.read_bytes()
        self.page_csp = content_security_policy(self.page.decode("utf-8"))
        self.static = {path: f.read_bytes() for path, f in STATIC_FILES.items()}
        self.router = router or create_router(config)
        self.history = HistoryStore(config.db_path)
        self.access = AccessStore(config.db_path, config.home)
        self.receipts = ReceiptStore(config.db_path)
        self.gate = Gate(self.access, self.receipts)
        self.tasks = TaskStore(config.db_path)
        self.memory = Memory(config.db_path)
        self.review = Review(config.db_path, self.memory)
        self.reset = Reset(config.db_path)
        self.chat_locks = ChatLocks()
        self.runs = RunRegistry()
        self.commands = CommandRegistry()    # M9: commands tasks are running
        self.exposure: Optional[ExposureReport] = None
        self.recovered: list = []      # M5: tasks closed at startup (set by make_server)
        self._ollama_cache = (0.0, None)

    def refresh_exposure(self) -> ExposureReport:
        self.exposure = check_exposure(self.router.adapter.port)
        return self.exposure

    def ollama_summary(self) -> dict:
        """Cheap Ollama status for the 5-second health poll (cached 4 s)."""
        at, cached = self._ollama_cache
        if cached is not None and self._clock() - at < 4:
            return cached
        try:
            summary = {"reachable": True, "version": self.router.adapter.version()}
        except Exception as exc:
            summary = {"reachable": False, "error": getattr(exc, "message", str(exc))}
        self._ollama_cache = (self._clock(), summary)
        return summary

    def health(self) -> dict:
        # Exercise from docs/m0-runtime.md: add a harmless field here and watch it appear.
        return {
            "status": "ok",
            "version": __version__,
            "milestone": MILESTONE,
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "uptime_s": int(self._clock() - self._started_mono),
            "bind": f"{self.config.host}:{self.policy.port}",
            "launches": self.launches,
            "ollama": self.ollama_summary(),
            "ollama_exposure": self.exposure.as_dict() if self.exposure else None,
        }


class Handler(HistoryRoutes, AccessRoutes, MemoryRoutes, TaskRoutes, ModelRoutes, BaseHTTPRequestHandler):
    server_version = "Umbrous"
    sys_version = ""
    timeout = 15  # seconds; a stalled client cannot hold a thread forever
    runtime: Runtime  # set on the subclass made in make_server()

    # All methods go through one dispatcher so no method can skip the checks.
    def do_GET(self) -> None: self._dispatch()
    def do_HEAD(self) -> None: self._dispatch()
    def do_POST(self) -> None: self._dispatch()
    def do_PUT(self) -> None: self._dispatch()
    def do_PATCH(self) -> None: self._dispatch()
    def do_DELETE(self) -> None: self._dispatch()
    def do_OPTIONS(self) -> None: self._dispatch()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # replaced by the structured line in _dispatch, which never logs headers

    def _dispatch(self) -> None:
        start = time.monotonic()
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        status, reason = 500, "error"
        try:
            length = self.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                status, reason = 413, "body_too_large"
                self._send_json(status, {"error": reason})
                return
            decision = evaluate(self.runtime.policy, self.command, path, self.headers)
            if not decision.ok:
                status, reason = decision.status, decision.reason
                self._send_json(status, {"error": reason})
                return
            status, reason = self._route(path)
        except Exception:  # never leak a traceback to the client
            log.exception("http.unhandled_error")
            status, reason = 500, "internal_error"
            try:
                self._send_json(500, {"error": reason})
            except Exception:
                pass
        finally:
            # Redact first, then truncate: truncating first could cut a secret in half,
            # and a half secret no longer matches the exact-value redaction rule.
            safe_path = default_redactor.redact(path)
            if len(safe_path) > 200:
                safe_path = safe_path[:200] + "…"
            log_event(log, "http.request", method=self.command, path=safe_path,
                      status=status, reason=reason, ms=int((time.monotonic() - start) * 1000))

    def _route(self, path: str) -> Tuple[int, str]:
        rt = self.runtime
        if path == "/":
            self._send(200, rt.page, "text/html; charset=utf-8", {"Content-Security-Policy": rt.page_csp})
            return 200, "page"
        if path in rt.static:
            self._send(200, rt.static[path], "text/javascript; charset=utf-8")
            return 200, "static"
        if path == CLAIM_PATH:
            token = rt.policy.claim(self.headers.get(LAUNCH_HEADER))
            if token is None:
                self._send_json(401, {"error": "launch_code_invalid_used_or_expired"})
                return 401, "launch_code_rejected"
            self._send_json(200, {"token": token})
            return 200, "session_claimed"
        if path == "/api/health" and self.command in ("GET", "HEAD"):
            self._send_json(200, rt.health())
            return 200, "health"
        if path == "/api/models" and self.command == "GET":
            status = rt.router.status()
            status["exposure"] = rt.refresh_exposure().as_dict()
            self._send_json(200, status)
            return 200, "models"
        if path in ("/api/providers/codex", "/api/providers/codex/login"):
            return self._provider(path)
        if self.command == "POST" and path in ("/api/models/probe", "/api/chat", "/api/chat/cancel"):
            body = self._read_json()
            if body is None:
                self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
                return 400, "bad_json"
            if path == "/api/models/probe":
                return self._probe(body)
            if path == "/api/chat":
                return self._chat(body)
            ok = rt.runs.cancel(str(body.get("run_id", "")))
            self._send_json(200 if ok else 404, {"cancelled": ok})
            return (200, "cancelled") if ok else (404, "no_such_run")
        if ACCESS_PATH.match(path) or APPROVAL_PATH.match(path) or path == "/api/receipts":
            try:
                return self._access(path)
            except (AccessError, HistoryError) as exc:
                status = ACCESS_STATUS.get(exc.code, HISTORY_STATUS.get(exc.code, 400))
                self._send_json(status, {"error": exc.code, "message": exc.message})
                return status, exc.code
        if path == "/api/tasks" or path.startswith("/api/tasks/"):
            return self._tasks(path)
        if path == "/api/memory" or path.startswith("/api/memory/"):
            try:
                return self._memory(path)
            except (MemoryError_, HistoryError) as exc:
                status = MEMORY_STATUS.get(exc.code, HISTORY_STATUS.get(exc.code, 400))
                self._send_json(status, {"error": exc.code, "message": exc.message})
                return status, exc.code
        if path in ("/api/reset", "/api/reset/preview"):
            return self._reset(path)
        if path == "/api/projects" or path == "/api/chats" or path.startswith("/api/chats/"):
            try:
                return self._history(path)
            except HistoryError as exc:
                status = HISTORY_STATUS.get(exc.code, 400)
                self._send_json(status, {"error": exc.code, "message": exc.message})
                return status, exc.code
        self._send_json(404, {"error": "not_found"})
        return 404, "not_found"


    def _read_json(self) -> Optional[dict]:
        """Only JSON bodies are accepted. Requiring the JSON content type also
        means a plain HTML form on another site could never send one."""
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return None
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None


    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        for k, v in {**BASE_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")


class LoopbackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True
    # True lets a restart reuse the port while old connections sit in TIME_WAIT. It does not
    # let a second program listen on the same address and port; that still fails clearly.
    allow_reuse_address = True

    def server_close(self) -> None:
        runtime = getattr(self.RequestHandlerClass, "runtime", None)
        if runtime is not None:
            runtime.runs.cancel_all()
            runtime.router.close()
        super().server_close()


def make_server(config: Config, schema_version: int, launches: int,
                policy_factory: Callable[[int], AccessPolicy] = AccessPolicy,
                router: Optional[ModelRouter] = None) -> Tuple[LoopbackServer, Runtime]:
    """Bind to 127.0.0.1 (never anything else) and build the request handler."""
    host = ensure_loopback(config.host)
    server = LoopbackServer((host, config.port), Handler, bind_and_activate=True)
    bound_host, bound_port = server.server_address[:2]
    if bound_host != "127.0.0.1":  # belt and braces: refuse to serve on anything else
        server.server_close()
        raise RuntimeError(f"refusing to serve on non-loopback address {bound_host}")
    policy = policy_factory(bound_port)
    runtime = Runtime(config, policy, schema_version, launches, router=router)
    # M5: close tasks a previous run stopped in the middle of, before serving.
    def workspace_of(project_id: str):
        ws = runtime.access.workspace(project_id)
        return ws.root if ws else None
    runtime.recovered = recover(runtime.tasks, runtime.receipts, runtime.history, workspace_of)
    server.RequestHandlerClass = type("BoundHandler", (Handler,), {"runtime": runtime})
    return server, runtime
