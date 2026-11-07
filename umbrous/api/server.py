"""The loopback HTTP server: how the browser page reaches Python.

    browser page ──HTTP──▶ 127.0.0.1:<port> ──▶ Handler ──▶ evaluate() ──▶ route
                                                             (security.py)

Python's standard-library `http.server` is enough for M0: two static files and
two JSON endpoints. Nothing here is reachable from another machine because the
socket is bound to 127.0.0.1 only.

Routes:
    GET  /                    the prototype page (prototypes/umbrous-harness.html)
    GET  /runtime-bridge.js   the script that connects the page to this API
    POST /api/session/claim   trade the one-time launch code for the session credential
    GET  /api/health          runtime status, shown in the Details panel
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
from umbrous.api.security import CLAIM_PATH, LAUNCH_HEADER, AccessPolicy, evaluate
from umbrous.config import Config, ensure_loopback
from umbrous.logging_setup import log_event
from umbrous.receipts.redaction import default_redactor

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = REPO_ROOT / "prototypes" / "umbrous-harness.html"
BRIDGE_PATH = Path(__file__).resolve().parents[1] / "web" / "runtime-bridge.js"
MAX_BODY = 16 * 1024

log = logging.getLogger("umbrous.api")

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
    """State shared by request handlers. Read-only after startup, so no locking."""

    def __init__(self, config: Config, policy: AccessPolicy, schema_version: int, launches: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.policy = policy
        self.schema_version = schema_version
        self.launches = launches
        self._clock = clock
        self._started_mono = clock()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.page = PAGE_PATH.read_bytes()
        self.page_csp = content_security_policy(self.page.decode("utf-8"))
        self.bridge = BRIDGE_PATH.read_bytes()

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
        if path == "/runtime-bridge.js":
            self._send(200, rt.bridge, "text/javascript; charset=utf-8")
            return 200, "bridge"
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
        self._send_json(404, {"error": "not_found"})
        return 404, "not_found"

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
                policy_factory: Callable[[int], AccessPolicy] = AccessPolicy) -> Tuple[LoopbackServer, Runtime]:
    """Bind to 127.0.0.1 (never anything else) and build the request handler."""
    host = ensure_loopback(config.host)
    server = LoopbackServer((host, config.port), Handler, bind_and_activate=True)
    bound_host, bound_port = server.server_address[:2]
    if bound_host != "127.0.0.1":  # belt and braces: refuse to serve on anything else
        server.server_close()
        raise RuntimeError(f"refusing to serve on non-loopback address {bound_host}")
    policy = policy_factory(bound_port)
    runtime = Runtime(config, policy, schema_version, launches)
    server.RequestHandlerClass = type("BoundHandler", (Handler,), {"runtime": runtime})
    return server, runtime
