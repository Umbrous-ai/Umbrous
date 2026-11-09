"""M10: a quick check of a file Umbrous just wrote, run in-process.

After write_file or edit_file succeeds, the new text is parsed (never run) when
its kind is one Python can parse by itself: Python source, JSON and TOML. A
parse error goes back to the model with the edit's result, so a broken edit is
caught in the same step instead of after the next test run.

Nothing here executes project code or starts a process, so it needs no
permission beyond the write that was already allowed. The parser is the
runtime's own: Python source is parsed with the Python Umbrous runs on, so a
file meant for an older or newer Python may parse differently; the message
says which version parsed it.
"""

from __future__ import annotations

import ast
import json
import sys
import warnings
from typing import Optional

MAX_CHECK_CHARS = 512 * 1024


def _python(text: str, path: str) -> Optional[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")          # e.g. invalid escape sequences: not errors
            compile(text, path, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" + (f", column {exc.offset}" if exc.offset else "")
        src = (exc.text or "").rstrip("\n")
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return (f"Python {ver} cannot parse {path}: {exc.msg} ({where})."
                + (f"\n    {src.strip()[:200]}" if src.strip() else ""))
    except (ValueError, MemoryError, RecursionError) as exc:
        return f"Python cannot parse {path}: {str(exc)[:200]}."
    return None


def _json(text: str, path: str) -> Optional[str]:
    try:
        json.loads(text)
    except ValueError as exc:
        return f"{path} is not valid JSON: {str(exc)[:200]}."
    return None


def _toml(text: str, path: str) -> Optional[str]:
    try:
        import tomllib                                   # Python 3.11+
    except ImportError:
        return None
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return f"{path} is not valid TOML: {str(exc)[:200]}."
    return None


CHECKS = {".py": ("python", _python), ".pyi": ("python", _python), ".json": ("json", _json),
          ".toml": ("toml", _toml)}


def syntax(path: str, text: str) -> Optional[dict]:
    """{"kind", "ok", "message"} for a file kind that can be checked, else None."""
    name = path.rsplit("/", 1)[-1].lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in CHECKS or len(text) > MAX_CHECK_CHARS:
        return None
    kind, fn = CHECKS[ext]
    message = fn(text, path)
    return {"kind": kind, "ok": message is None, "message": message}
