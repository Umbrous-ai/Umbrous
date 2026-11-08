"""Access checks for the local API. Every request passes through `evaluate()`.

The layers, in the order they are checked:

1. Loopback bind (in server.py): other machines cannot connect at all.
2. Host header: must name this runtime (127.0.0.1:<port> or localhost:<port>).
   This defeats DNS rebinding, where a hostile site makes its own hostname
   resolve to 127.0.0.1; the browser still sends the hostile hostname as Host.
3. Origin / Sec-Fetch-Site: a request a browser sends on behalf of another
   web page carries that page's origin. Anything that is not this runtime's
   own origin is refused. Requests with no Origin (curl, same-origin GETs)
   continue to the credential check.
4. Session credential: every /api/ call must carry X-Umbrous-Token. A new
   random value is made at each launch and exists only in memory.

How the page gets the credential: the launcher opens the page with a one-time
launch code in the URL fragment (#launch=...). The page trades it once at
/api/session/claim for the session credential and removes it from the address
bar. The code expires after LAUNCH_CODE_TTL seconds or one use, so a copy left
in browser history or a process listing is useless.

What this does not stop: any program already running as your user (it can read
browser memory, or watch the process list during the launch window), and any
browser extension allowed on the page. These are layers, not a guarantee.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

TOKEN_HEADER = "X-Umbrous-Token"
LAUNCH_HEADER = "X-Umbrous-Launch"
LAUNCH_CODE_TTL = 120.0  # seconds

PUBLIC_PATHS = frozenset({"/", "/runtime-bridge.js", "/chat.js", "/model-controls.js"})
CLAIM_PATH = "/api/session/claim"


def new_secret() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


@dataclass(frozen=True)
class Decision:
    ok: bool
    status: int = 200
    reason: str = "ok"


ALLOW = Decision(True)


class AccessPolicy:
    def __init__(self, port: int, session_token: Optional[str] = None, launch_code: Optional[str] = None,
                 launch_ttl: float = LAUNCH_CODE_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.port = port
        self.session_token = session_token or new_secret()
        self.launch_code = launch_code or new_secret()
        self._clock = clock
        self._launch_expires = clock() + launch_ttl
        self._launch_used = False
        self._lock = threading.Lock()
        self.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self.allowed_origins = frozenset(f"http://{h}" for h in self.allowed_hosts)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def check_host(self, host: Optional[str]) -> bool:
        return host is not None and host.strip().lower() in self.allowed_hosts

    def check_origin(self, origin: Optional[str], sec_fetch_site: Optional[str]) -> bool:
        if sec_fetch_site is not None and sec_fetch_site.lower() not in ("same-origin", "none"):
            return False
        if origin is None:
            return True  # not sent by a cross-origin browser request; the credential check still applies
        return origin.strip().lower() in self.allowed_origins  # "null" and every foreign origin fail

    def check_token(self, presented: Optional[str]) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(presented.encode(), self.session_token.encode())

    def claim(self, presented_code: Optional[str]) -> Optional[str]:
        """Trade the one-time launch code for the session credential, once."""
        if not presented_code:
            return None
        with self._lock:
            if self._launch_used or self._clock() > self._launch_expires:
                return None
            if not hmac.compare_digest(presented_code.encode(), self.launch_code.encode()):
                return None
            self._launch_used = True
            return self.session_token

    def reissue_launch_code(self, launch_ttl: float = LAUNCH_CODE_TTL) -> str:
        """Make a fresh one-time launch code (desktop app only, see umbrous/desktop.py).

        The desktop shell asks for one when its window has to load the page
        again (a reload, or the web view's process restarted). It grants the
        same session credential as the first code, and the old code stops
        working. Only the process that started the runtime can ask: the
        request arrives on the runtime's stdin pipe, never over HTTP.
        """
        with self._lock:
            self.launch_code = new_secret()
            self._launch_expires = self._clock() + launch_ttl
            self._launch_used = False
            return self.launch_code

    def launch_code_available(self) -> bool:
        with self._lock:
            return not self._launch_used and self._clock() <= self._launch_expires


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    # http.server's headers are case-insensitive; plain dicts in tests are not.
    if hasattr(headers, "get_all"):
        values = headers.get_all(name) or []
        if len(values) > 1:
            return "\x00duplicate"  # ambiguous: two Host or two Origin headers never pass
        return values[0] if values else None
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def evaluate(policy: AccessPolicy, method: str, path: str, headers: Mapping[str, str]) -> Decision:
    """Decide whether a request may proceed to routing. Pure apart from the policy."""
    if not policy.check_host(_header(headers, "Host")):
        return Decision(False, 403, "host_not_allowed")
    if not policy.check_origin(_header(headers, "Origin"), _header(headers, "Sec-Fetch-Site")):
        return Decision(False, 403, "origin_not_allowed")
    if method == "OPTIONS":
        # No CORS: a preflight is only ever sent by another origin, so it is always refused.
        return Decision(False, 403, "cors_not_supported")

    if path in PUBLIC_PATHS:
        return ALLOW if method in ("GET", "HEAD") else Decision(False, 405, "method_not_allowed")
    if path == CLAIM_PATH:
        return ALLOW if method == "POST" else Decision(False, 405, "method_not_allowed")
    if path.startswith("/api/"):
        if not policy.check_token(_header(headers, TOKEN_HEADER)):
            return Decision(False, 401, "missing_or_wrong_session_credential")
        return ALLOW
    return Decision(False, 404, "not_found")
