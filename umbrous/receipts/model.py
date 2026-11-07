"""Receipt format (version 1) and task/action identifiers.

A receipt answers "what did Umbrous try to do, under what scope, and what was
decided", without holding the thing itself. It records references (a path, a
hash, a size), never file contents, prompts, model output, or secrets.

M0 defines and tests the format. Nothing stores receipts until M3; M3 must
build them with this module and persist only `Receipt.to_record()` output,
which is redacted on the way out. The full spec is docs/receipts.md.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple

from umbrous.receipts.redaction import Redactor, default_redactor

FORMAT_VERSION = 1
DECISIONS = ("allowed", "denied", "failed", "uncertain")
EVIDENCE_KINDS = ("file", "directory", "search", "url", "tool_result", "other")

# Small limits on every free-text field. They are there to make it awkward to
# smuggle content (a file body, a prompt) into a receipt by accident.
MAX_ACTION = 64
MAX_SCOPE = 256
MAX_REASON = 280
MAX_REF = 512
MAX_EVIDENCE = 50

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
_ID_RE = {p: re.compile(rf"^{p}_[0-9a-f]{{12}}[0-9a-f]{{16}}$") for p in ("tsk", "act", "rcp")}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReceiptError(ValueError):
    pass


def new_id(prefix: str) -> str:
    """Time-ordered id: prefix, 12 hex digits of milliseconds, 16 random hex digits.

    Sorting ids as strings sorts them by creation time, which keeps logs and
    receipts easy to read in order.
    """
    if prefix not in _ID_RE:
        raise ReceiptError(f"unknown id prefix {prefix!r}")
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(8)}"


def new_task_id() -> str:
    return new_id("tsk")


def new_action_id() -> str:
    return new_id("act")


def _check_id(value: str, prefix: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_RE[prefix].match(value):
        raise ReceiptError(f"{name} must be a {prefix}_ id")


def _check_text(value: Optional[str], limit: int, name: str, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or (required and not value):
        raise ReceiptError(f"{name} is required")
    if len(value) > limit:
        raise ReceiptError(f"{name} is longer than {limit} characters; store a reference, not content")
    if "\n" in value:
        raise ReceiptError(f"{name} must be a single line")


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ReceiptError("timestamps must be timezone-aware")
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer to evidence, never the evidence itself."""

    kind: str
    ref: str                      # e.g. a workspace-relative path or a search id
    sha256: Optional[str] = None  # hash of the content at the time, if known
    size_bytes: Optional[int] = None
    lines: Optional[Tuple[int, int]] = None

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ReceiptError(f"evidence kind must be one of {EVIDENCE_KINDS}")
        _check_text(self.ref, MAX_REF, "evidence ref")
        if self.sha256 is not None and not _SHA256_RE.match(self.sha256):
            raise ReceiptError("sha256 must be 64 lowercase hex characters")
        if self.size_bytes is not None and (not isinstance(self.size_bytes, int) or self.size_bytes < 0):
            raise ReceiptError("size_bytes must be a non-negative integer")
        if self.lines is not None:
            a, b = self.lines
            if not (isinstance(a, int) and isinstance(b, int) and 1 <= a <= b):
                raise ReceiptError("lines must be (first, last) with 1 <= first <= last")


@dataclass(frozen=True)
class Receipt:
    task_id: str
    action_id: str
    action: str        # tool or operation name, e.g. "fs.read"
    scope: str         # what it was allowed to touch, e.g. "project:demo resource:docs/a.md"
    decision: str      # allowed | denied | failed | uncertain
    started_at: datetime
    ended_at: datetime
    reason: Optional[str] = None
    evidence: Tuple[EvidenceRef, ...] = ()
    receipt_id: str = field(default_factory=lambda: new_id("rcp"))

    def __post_init__(self) -> None:
        _check_id(self.receipt_id, "rcp", "receipt_id")
        _check_id(self.task_id, "tsk", "task_id")
        _check_id(self.action_id, "act", "action_id")
        _check_text(self.action, MAX_ACTION, "action")
        if not _ACTION_RE.match(self.action):
            raise ReceiptError("action must look like 'fs.read' (lowercase, dots, underscores)")
        _check_text(self.scope, MAX_SCOPE, "scope")
        if self.decision not in DECISIONS:
            raise ReceiptError(f"decision must be one of {DECISIONS}")
        _check_text(self.reason, MAX_REASON, "reason", required=False)
        if self.ended_at < self.started_at:
            raise ReceiptError("ended_at is before started_at")
        if len(self.evidence) > MAX_EVIDENCE:
            raise ReceiptError(f"at most {MAX_EVIDENCE} evidence references")
        if not all(isinstance(e, EvidenceRef) for e in self.evidence):
            raise ReceiptError("evidence must be EvidenceRef values")

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    def to_record(self, redactor: Redactor = default_redactor) -> dict:
        """The only form a receipt may be persisted in: plain JSON values, redacted."""
        record = {
            "format_version": FORMAT_VERSION,
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "action": self.action,
            "scope": self.scope,
            "decision": self.decision,
            "reason": self.reason,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_ms": self.duration_ms,
            "evidence": [
                {
                    "kind": e.kind,
                    "ref": e.ref,
                    "sha256": e.sha256,
                    "size_bytes": e.size_bytes,
                    "lines": list(e.lines) if e.lines else None,
                }
                for e in self.evidence
            ],
        }
        return redactor.redact_obj(record)
