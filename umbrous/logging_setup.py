"""Structured logging: one JSON object per line, redacted before it is written.

Use `log_event(logger, "event.name", key=value, ...)` rather than formatting
values into the message. That keeps every line machine-readable, e.g.

    {"ts": "2026-11-07T01:52:10.114Z", "level": "INFO", "logger": "umbrous.api",
     "event": "http.request", "method": "GET", "path": "/api/health", "status": 200, "ms": 1}

Logs go to stderr and to <home>/logs/runtime.log (rotated, readable only by you).
Request headers, query strings and bodies are never logged.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from umbrous.config import Config, make_private_dir
from umbrous.receipts.redaction import Redactor, default_redactor

_FIELDS = "umbrous_fields"


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update(getattr(record, _FIELDS, {}) or {})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(self.redactor.redact_obj(entry), default=str, ensure_ascii=False)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={_FIELDS: fields})


def _private_file_handler(path: str) -> logging.Handler:
    # Create the file 0600 before the handler opens it, so it is never world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    return logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")


def setup_logging(config: Config, redactor: Redactor = default_redactor, stream: Optional[Any] = None) -> logging.Logger:
    root = logging.getLogger("umbrous")
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(config.log_level)
    root.propagate = False
    fmt = JsonFormatter(redactor)

    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    make_private_dir(config.log_dir)
    file_handler = _private_file_handler(str(config.log_dir / "runtime.log"))
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    return root
