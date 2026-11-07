"""Redaction rules, applied before anything is persisted (logs now, receipts from M3).

Two kinds of rule, applied in this order:

1. Exact values. Secrets the runtime itself knows about (the session
   credential, the launch code) are registered at startup and replaced
   wherever they appear, whatever they look like.
2. Patterns. Well-known secret shapes (API keys, tokens, private keys,
   `password=...` assignments, credentials embedded in URLs).

Structured data is walked recursively, and any value stored under a
sensitive-looking key ("password", "token", "authorization", ...) is replaced
whole, whatever it contains.

Redaction is a safety net, not a guarantee: a secret with no known shape that
the runtime was not told about will pass through. That is why receipts are
also designed to hold references, never contents (see docs/receipts.md).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Pattern, Tuple

MARK = "[REDACTED:{kind}]"
MIN_EXACT_LEN = 8  # never register short strings; they would redact ordinary words

_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")),
)

# `password = hunter22`, `API_KEY: "abc..."`, `token=...`: keep the name, drop the value.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|token|access[_-]?key)[A-Za-z0-9_]*)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{6,})\3"
)

SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey", "access_key",
    "authorization", "cookie", "set-cookie", "x-umbrous-token", "x-umbrous-launch",
    "session_token", "launch_code", "private_key", "credentials",
})


def _is_sensitive_key(key: str) -> bool:
    k = key.lower().replace(" ", "_")
    return k in SENSITIVE_KEYS or k.endswith("_token") or k.endswith("_secret") or k.endswith("_password")


class Redactor:
    def __init__(self) -> None:
        self._exact: set = set()
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        """Always redact this exact value from now on (e.g. the session credential)."""
        if not isinstance(value, str) or len(value) < MIN_EXACT_LEN:
            raise ValueError("only strings of 8+ characters can be registered as secrets")
        with self._lock:
            self._exact.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            exact = sorted(self._exact, key=len, reverse=True)
        for value in exact:
            if value in text:
                text = text.replace(value, MARK.format(kind="runtime_secret"))
        for kind, pattern in _PATTERNS:
            text = pattern.sub(MARK.format(kind=kind), text)
        text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MARK.format(kind='assignment')}{m.group(3)}", text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Redact strings anywhere inside dicts, lists and tuples."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and _is_sensitive_key(k) and v not in (None, ""):
                    out[k] = MARK.format(kind="field")
                else:
                    out[k] = self.redact_obj(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    def contains_known_secret(self, text: str) -> bool:
        with self._lock:
            return any(v in text for v in self._exact)



# One process-wide redactor: logging and receipts share the same rules.
default_redactor = Redactor()
