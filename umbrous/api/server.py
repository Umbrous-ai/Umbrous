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
from urllib.parse import parse_qs

from umbrous import MILESTONE, __version__
from umbrous.api.security import CLAIM_PATH, LAUNCH_HEADER, AccessPolicy, evaluate
from umbrous.config import Config, ensure_loopback
from umbrous.logging_setup import log_event
from umbrous.model.base import Deadlines
from umbrous.model.exposure import ExposureReport, check_exposure
from umbrous.model.ollama import OllamaAdapter
from umbrous.model.router import ModelRouter
from umbrous.receipts.redaction import default_redactor
from umbrous.gate.access import AccessError, AccessStore
from umbrous.gate.gate import Gate, GateError
from umbrous.history.store import HistoryError, HistoryStore
from umbrous.memory.review import Review
from umbrous.memory.store import Memory, MemoryError_
from umbrous.receipts.store import ReceiptStore
from umbrous.reset import Reset, ResetError
from umbrous.runloop.chat import ChatLocks, RunRegistry, run_chat
from umbrous.tools.run import CommandRegistry
from umbrous.tasks.recover import recover, resume_info
from umbrous.tasks.store import TaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = REPO_ROOT / "prototypes" / "umbrous-harness.html"
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
STATIC_FILES = {"/runtime-bridge.js": WEB_DIR / "runtime-bridge.js", "/chat.js": WEB_DIR / "chat.js"}
MAX_BODY = 256 * 1024  # a chat message is capped at 32,000 characters (up to ~6 bytes each as JSON)

log = logging.getLogger("umbrous.api")

CHAT_PATH = re.compile(r"^/api/chats/(chat_[A-Za-z0-9_-]{8,64})(/archive|/unarchive)?$")
PROJECT_ID_RE = re.compile(r"^prj_[A-Za-z0-9_-]{4,64}$")
ACCESS_PATH = re.compile(r"^/api/projects/(prj_[A-Za-z0-9_-]{4,64})/"
                         r"(access|workspace|workspace/detach|profile|grants/(grt_[0-9a-f]{16})/revoke)$")
APPROVAL_PATH = re.compile(r"^/api/approvals/(apr_[0-9a-f]{16})$")
TASK_ID_RE = re.compile(r"^tsk_[0-9a-f]{28}$")
ACCESS_STATUS = {"no_such_project": 404, "no_workspace": 409, "no_such_grant": 404}
MEMORY_PATH = re.compile(r"^/api/memory/(mem_[0-9a-f]{16})(/delete)?$")
MEMORY_STATUS = {"no_such_memory": 404, "no_such_project": 404, "memory_full": 409,
                 "no_such_candidate": 404, "already_decided": 409, "duplicate": 409}
CANDIDATE_PATH = re.compile(r"^/api/memory/candidates/(cand_[0-9a-f]{16})(/accept|/reject)?$")
RESET_STATUS = {"no_such_chat": 404, "no_such_memory": 404, "no_such_project": 404, "preview_changed": 409,
                "chat_busy": 409}
HISTORY_STATUS = {"no_such_project": 404, "no_such_chat": 404, "project_exists": 409, "bad_project_name": 400}

_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


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
        self.router = router or ModelRouter(
            OllamaAdapter(config.ollama_url, Deadlines(first_token=config.first_token_timeout_s,
                                                       idle=config.idle_timeout_s, total=config.total_timeout_s)),
            config.db_path)
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


class Handler(BaseHTTPRequestHandler):
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

    def _tasks(self, path: str) -> Tuple[int, str]:
        """Task state, for the report and the trace (M4). Read-only: tasks are
        removed only by `bin/umbrous purge tasks`."""
        rt = self.runtime
        if self.command != "GET":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        if path == "/api/tasks":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            chat_id = (q.get("chat_id") or [""])[0]
            if not (chat_id and CHAT_PATH.match("/api/chats/" + chat_id)):
                self._send_json(400, {"error": "give chat_id"})
                return 400, "bad_query"
            listed = rt.tasks.for_chat(chat_id)
            for t in listed:     # M5: say whether an interrupted task can be resumed
                if t["status"] == "unknown":
                    t["resume"] = resume_info(rt.tasks, rt.history, t["task_id"])
            self._send_json(200, {"tasks": listed})
            return 200, "tasks"
        task_id = path[len("/api/tasks/"):]
        task = rt.tasks.get(task_id) if TASK_ID_RE.match(task_id) else None
        if task is None:
            self._send_json(404, {"error": "no_such_task"})
            return 404, "no_such_task"
        task["resume"] = resume_info(rt.tasks, rt.history, task_id, task)
        self._send_json(200, task)
        return 200, "task"

    def _memory(self, path: str) -> Tuple[int, str]:
        """The two memory stores (M6): inspect, add, edit, delete. Nothing here
        reads or changes chats; deleting an item removes only that item."""
        rt, cmd = self.runtime, self.command
        mem, hist = rt.memory, rt.history

        def exists(project_id: str) -> bool:
            try:
                hist.get_project(project_id)
                return True
            except HistoryError:
                return False

        if path == "/api/memory" and cmd == "GET":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (q.get("project_id") or [""])[0]
            if project_id and not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            personal = []
            for m in mem.personal.list():
                d = m.as_dict()
                d["applies"] = bool(project_id) and (m.scope == "all" or project_id in m.scope)
                personal.append(d)
            out = {"personal": personal, "project_id": project_id or None, "project": [],
                   "limits": {"max_items": rt.config.memory_max_items, "max_tokens": rt.config.memory_tokens}}
            if project_id:
                hist.get_project(project_id)
                out["project"] = [m.as_dict() for m in mem.project.list(project_id)]
                out["candidates"] = [c.as_dict() for c in rt.review.list(project_id)]   # M7: waiting for you
            else:
                out["candidates"] = []
            self._send_json(200, out)
            return 200, "memory"
        if path == "/api/memory/candidates" and cmd == "GET":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (q.get("project_id") or [""])[0] or None
            status = (q.get("status") or ["pending"])[0]
            if project_id and not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            if status not in ("pending", "accepted", "rejected", "all"):
                self._send_json(400, {"error": "bad_status"})
                return 400, "bad_status"
            items = rt.review.list(project_id, None if status == "all" else status)
            self._send_json(200, {"candidates": [c.as_dict() for c in items]})
            return 200, "candidates"
        if cmd != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if path == "/api/memory":
            store = body.get("store")
            if store == "personal":
                item = mem.personal.add(body.get("text"), body.get("kind") or "fact", body.get("scope", "all"),
                                        project_exists=exists)
            elif store == "project":
                project_id = str(body.get("project_id") or "")
                if not PROJECT_ID_RE.match(project_id):
                    self._send_json(400, {"error": "bad_project_id"})
                    return 400, "bad_project_id"
                item = mem.project.add(project_id, body.get("text"), body.get("kind") or "decision")
            else:
                self._send_json(400, {"error": "bad_store", "message": 'store is "personal" or "project"'})
                return 400, "bad_store"
            log_event(log, "memory.added", memory_id=item.id, store=item.store, kind=item.kind)  # never the text
            self._send_json(201, {"item": item.as_dict()})
            return 201, "memory_added"
        cm = CANDIDATE_PATH.match(path)
        if cm:
            return self._candidate(cm.group(1), (cm.group(2) or "").lstrip("/"), body, exists)
        m = MEMORY_PATH.match(path)
        if not m:
            self._send_json(404, {"error": "not_found"})
            return 404, "not_found"
        item_id, delete = m.group(1), bool(m.group(2))
        item = mem.get(item_id)
        if item is None:
            raise MemoryError_("That memory item does not exist.", "no_such_memory")
        if delete:
            mem.delete(item_id)
            log_event(log, "memory.deleted", memory_id=item_id, store=item.store)
            self._send_json(200, {"deleted": item_id, "store": item.store})
            return 200, "memory_deleted"
        if item.store == "personal":
            item = mem.personal.update(item_id, body.get("text"), body.get("kind"), body.get("scope"),
                                       project_exists=exists)
        else:
            if "scope" in body or "project_id" in body:
                raise MemoryError_("A project item stays in its project. Delete it and add it to the other "
                                   "project instead.", "bad_scope")
            item = mem.project.update(item_id, body.get("text"), body.get("kind"))
        log_event(log, "memory.edited", memory_id=item_id, store=item.store)
        self._send_json(200, {"item": item.as_dict()})
        return 200, "memory_edited"

    def _reset(self, path: str) -> Tuple[int, str]:
        """M8: preview a selective reset, or confirm exactly a preview."""
        rt = self.runtime
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        try:
            if path == "/api/reset/preview":
                out = rt.reset.preview(body.get("selection"), lambda cid: rt.chat_locks.get(cid).locked())
                self._send_json(200, out)
                return 200, "reset_preview"
            out = rt.reset.confirm(body.get("selection"), str(body.get("preview_id") or ""), rt.chat_locks)
        except ResetError as exc:
            status = RESET_STATUS.get(exc.code, 400)
            payload = {"error": exc.code, "message": exc.message}
            if exc.preview is not None:
                payload["preview"] = exc.preview
            self._send_json(status, payload)
            return status, exc.code
        # ids and counts only, never titles or memory text
        log_event(log, "reset.done", **{k: v for k, v in out["deleted"].items()},
                  ids=[i["id"] for i in out["items"]][:50])
        self._send_json(200, out)
        return 200, "reset_done"

    def _candidate(self, cand_id: str, action: str, body: dict, exists) -> Tuple[int, str]:
        """M7: review one suggestion. Accept is the only way a suggestion
        reaches memory, and it is checked again first."""
        rev = self.runtime.review
        edits = {k: body.get(k) for k in ("text", "kind", "store", "scope")}
        if action == "reject":
            cand = rev.reject(cand_id)
            log_event(log, "memory.candidate_rejected", candidate_id=cand_id)   # never the text
            self._send_json(200, {"candidate": cand.as_dict()})
            return 200, "candidate_rejected"
        if action == "accept":
            cand, item = rev.accept(cand_id, project_exists=exists, **edits)
            log_event(log, "memory.candidate_accepted", candidate_id=cand_id, memory_id=item.id, store=item.store,
                      kind=item.kind, edited=cand.edited)
            self._send_json(200, {"candidate": cand.as_dict(), "item": item.as_dict()})
            return 200, "candidate_accepted"
        cand = rev.edit(cand_id, project_exists=exists, **edits)
        log_event(log, "memory.candidate_edited", candidate_id=cand_id)
        self._send_json(200, {"candidate": cand.as_dict()})
        return 200, "candidate_edited"

    def _access(self, path: str) -> Tuple[int, str]:
        """The access view, approvals and receipts (M3)."""
        rt, cmd = self.runtime, self.command
        if path == "/api/receipts":
            if cmd != "GET":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            chat_id, task_id = (q.get("chat_id") or [""])[0], (q.get("task_id") or [""])[0]
            if task_id and TASK_ID_RE.match(task_id):
                records = rt.receipts.for_task(task_id)
            elif chat_id and CHAT_PATH.match("/api/chats/" + chat_id):
                records = rt.receipts.for_chat(chat_id)
            else:
                self._send_json(400, {"error": "give chat_id or task_id"})
                return 400, "bad_query"
            self._send_json(200, {"receipts": records})
            return 200, "receipts"
        m = APPROVAL_PATH.match(path)
        if m:
            if cmd != "POST":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            body = self._read_json()
            if body is None:
                self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
                return 400, "bad_json"
            try:
                rt.gate.approvals.answer(m.group(1), str(body.get("choice", "")))
            except GateError as exc:
                self._send_json(409, {"error": "approval_closed", "message": str(exc)})
                return 409, "approval_closed"
            log_event(log, "gate.approval_answered", approval_id=m.group(1), choice=body.get("choice"))
            self._send_json(200, {"ok": True})
            return 200, "approval_answered"
        m = ACCESS_PATH.match(path)
        project_id, what = m.group(1), m.group(2)
        rt.history.get_project(project_id)  # 404 for an unknown project
        if what == "access":
            if cmd != "GET":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            self._send_json(200, rt.access.view(project_id))
            return 200, "access_view"
        if cmd != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if what == "workspace":
            rt.access.attach(project_id, body.get("root"), str(body.get("profile") or "workspace_read"))
            reason = "workspace_attached"
        elif what == "workspace/detach":
            rt.access.detach(project_id)
            reason = "workspace_detached"
        elif what == "profile":
            rt.access.set_profile(project_id, str(body.get("profile", "")))
            reason = "profile_changed"
        else:
            rt.access.revoke(project_id, m.group(3))
            reason = "grant_revoked"
        log_event(log, "access." + reason, project_id=project_id)  # never the folder path
        self._send_json(200, rt.access.view(project_id))
        return 200, reason

    def _history(self, path: str) -> Tuple[int, str]:
        """Projects and chats (M2). Archive and unarchive are the only changes
        to an existing chat; nothing here deletes."""
        hist, cmd = self.runtime.history, self.command
        if path == "/api/projects" and cmd == "GET":
            self._send_json(200, {"projects": [p.as_dict() for p in hist.list_projects()]})
            return 200, "projects"
        if path == "/api/chats" and cmd == "GET":
            query = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (query.get("project_id") or [""])[0]
            if not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            archived = (query.get("archived") or ["0"])[0] in ("1", "true")
            self._send_json(200, {"project_id": project_id, "archived": archived,
                                  "chats": [c.as_dict() for c in hist.list_chats(project_id, archived)]})
            return 200, "chats"
        match = CHAT_PATH.match(path)
        if match and cmd == "GET" and not match.group(2):
            chat = hist.get_chat(match.group(1))
            self._send_json(200, {"chat": chat.as_dict(), "messages": [m.as_dict() for m in hist.messages(chat.id)]})
            return 200, "chat"
        if not match and path not in ("/api/projects", "/api/chats"):
            self._send_json(404, {"error": "not_found"})
            return 404, "not_found"
        if cmd != "POST" or (match and not match.group(2)):  # e.g. DELETE: there is no delete
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if path == "/api/projects":
            project = hist.create_project(body.get("name"))
            log_event(log, "history.project_created", project_id=project.id)
            self._send_json(201, {"project": project.as_dict()})
            return 201, "project_created"
        if path == "/api/chats":
            project_id = str(body.get("project_id", ""))
            if not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            chat = hist.create_chat(project_id)
            log_event(log, "history.chat_created", chat_id=chat.id, project_id=project_id)
            self._send_json(201, {"chat": chat.as_dict()})
            return 201, "chat_created"
        archive = match.group(2) == "/archive"
        chat_id = hist.get_chat(match.group(1)).id  # raises for an unknown chat
        # Archiving takes the same per-chat lock as a reply. While a reply is
        # running the archive is refused, so the reply is always saved first
        # (the page stops the reply, waits for it to end, then archives).
        lock = self.runtime.chat_locks.get(chat_id)
        if not lock.acquire(blocking=False):
            self._send_json(409, {"error": "chat_busy", "message": "A reply is still running in this chat. "
                                  "Stop it or let it finish, then archive."})
            return 409, "chat_busy"
        try:
            chat = hist.set_archived(chat_id, archive)
        finally:
            lock.release()
        log_event(log, "history.chat_archived" if archive else "history.chat_unarchived", chat_id=chat.id)
        self._send_json(200, {"chat": chat.as_dict()})
        return 200, "archived" if archive else "unarchived"

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

    def _probe(self, body: dict) -> Tuple[int, str]:
        from umbrous.model.base import ModelError
        try:
            result, model = self.runtime.router.probe(str(body.get("model", "")))
        except ModelError as exc:
            self._send_json(409, {"error": exc.code, "message": exc.message})
            return 409, exc.code
        self._send_json(200, {"model": model.name, "passed": result.passed, "stage": result.stage,
                              "detail": result.detail, "ms": result.ms})
        log_event(log, "model.probe", model=model.name, passed=result.passed, stage=result.stage, ms=result.ms)
        return 200, "probe_passed" if result.passed else "probe_failed"

    def _chat(self, body: dict) -> Tuple[int, str]:
        """Stream the reply as JSON lines. No Content-Length: the response ends
        when the connection closes (HTTP/1.0), which the page treats as the end."""
        rt = self.runtime
        events = run_chat(rt.config, rt.router, rt.history, rt.chat_locks, rt.runs, str(body.get("chat_id", "")),
                          str(body.get("model", "")), body.get("message"), gate=rt.gate,
                          approvals=body.get("approvals") is True, tasks=rt.tasks,
                          resume_task=body.get("resume_task") if "resume_task" in body else None,
                          memory=rt.memory, use_memory=body.get("use_memory") is not False, review=rt.review, commands=rt.commands)
        self.send_response(200)
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        last = "ok"
        try:
            for event in events:
                if event["type"] == "error":
                    last = event["code"]
                self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                self.wfile.flush()
        except OSError:  # the page closed the connection: stop the model too
            events.close()
            return 200, "client_gone"
        return 200, "chat_" + last

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
