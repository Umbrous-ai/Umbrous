"""Find and change files in the workspace (M9), confined like reads (M3).

Every function here is called by the gate only, with a workspace-relative
path the gate has already checked as text (no "..", nothing absolute,
nothing excluded). Confinement on disk works as in tools/fs.py: the root is
opened once, every component is opened relative to its parent's descriptor
with O_NOFOLLOW, and the file itself is checked on its open descriptor (a
regular file with one link).

How a change is made
--------------------
1. The current file is opened (or found absent) through descriptors and
   hashed. That hash must be the one the caller expects: the hash of the file
   as this task last read or wrote it. If it differs, someone else changed it
   since, and the change is refused as a conflict instead of overwriting their
   work. A new file must not exist yet.
2. The new content is computed in memory and hashed.
3. `intent(before, after, tmp)` is called and must return before anything is
   written: the run loop records it durably (a `write.intent` event), so a
   crash from here on leaves enough to check against (tasks/recover.py).
4. The content goes to a temporary file in the same folder (O_CREAT|O_EXCL,
   the old file's permission bits), is fsynced, and then:
     replace  the current file is hashed once more, and only if it is still
              the expected one is the temporary renamed over it (atomic: the
              file is old or new, never half written);
     create   the temporary is hard-linked to the new name, which fails if a
              file of that name appeared meanwhile (no clobbering), then the
              temporary name is removed.
   The folder is fsynced so the rename survives a crash.

So after a crash the file's hash is the old one (it did not happen), the new
one (it happened), or something else (another change: a conflict, never
retried blindly). The temporary name is recorded with the intent, so recovery
can remove a leftover one.

This is not an OS sandbox: it depends on this code being correct.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Callable, List, Optional, Tuple

from umbrous.tools import fs
from umbrous.tools.fs import FsError, _check_regular, _decode, _open_component, _read_all, fold, is_excluded, \
    open_dir, open_root, component_flags

MAX_WRITE_BYTES = fs.MAX_READ_BYTES      # what is written must be readable again
MAX_FIND_RESULTS = 200
MAX_DIFF_CHARS = 4_000
TMP_PREFIX = ".umbrous-tmp-"
_DIR_FLAGS = fs._DIR_FLAGS
_FILE_FLAGS = fs._FILE_FLAGS


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FindResult:
    path: str
    pattern: str
    matches: List[Tuple[str, str, Optional[int]]] = field(default_factory=list)   # (path, kind, size)
    scanned: int = 0
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False


@dataclass
class ChangeResult:
    path: str
    op: str                    # created | replaced | edited | unchanged | already_applied
    before: Optional[str]      # sha256 before (None: the file did not exist)
    after: str                 # sha256 after
    size: int
    added: int = 0
    removed: int = 0
    diff: str = ""             # a bounded unified diff, for the model and the page (never stored)


class Conflict(FsError):
    """The file is not what the change expected: changed by someone else, or
    already exists. Reported, never overwritten."""


# ---------------------------------------------------------------- find
def find(root: str, parts: Tuple[str, ...], pattern: str, deadline_s: float = fs.SEARCH_SECONDS,
         cancelled: Callable[[], bool] = lambda: False) -> FindResult:
    """Files and folders whose name (or workspace path) matches a glob such as
    `*.py`, `test_*`, `src/**/main.rs` or a plain word (a part of the name),
    compared case-insensitively. Excluded names are skipped, symlinks are not
    followed."""
    raw = pattern.strip()
    pat = fold(raw)
    plain = not any(ch in raw for ch in "*?[")
    want_path = "/" in raw
    out = FindResult(path="/".join(parts) or ".", pattern=raw)
    started = time.monotonic()
    with open_root(root) as root_fd, open_dir(root_fd, parts) as top:
        for dirpath, dirnames, filenames, _dfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
            rel = tuple(p for p in dirpath.split("/") if p not in ("", "."))
            if len(rel) >= fs.MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = sorted(d for d in dirnames if not is_excluded(parts + rel + (d,)))
            if cancelled():
                out.cancelled = out.truncated = True
                return out
            if time.monotonic() - started > deadline_s:
                out.timed_out = out.truncated = True
                return out
            for name, kind in [(d, "folder") for d in dirnames] + [(f, "file") for f in sorted(filenames)]:
                if kind == "file" and is_excluded(parts + rel + (name,)):
                    continue
                out.scanned += 1
                full = "/".join(parts + rel + (name,))
                target = fold(full if want_path else name)
                hit = (pat in target) if plain else (fnmatchcase(target, pat) or
                                                      (want_path and fnmatchcase(target, pat.replace("**/", ""))))
                if not hit:
                    continue
                size = None
                if kind == "file":
                    try:
                        st = os.stat(name, dir_fd=_dfd, follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode):
                            kind = "symlink (not followed)"
                        else:
                            size = st.st_size
                    except OSError:
                        kind = "unreadable"
                if len(out.matches) >= MAX_FIND_RESULTS:
                    out.truncated = True
                    return out
                out.matches.append((full, kind, size))
    return out


# ---------------------------------------------------------------- changing
def _current(parent: int, name: str) -> Tuple[Optional[bytes], Optional[int]]:
    """The file's bytes and permission bits, or (None, None) if it does not exist."""
    try:
        fd = _open_component(name, parent, _FILE_FLAGS)
    except FsError as exc:
        if exc.code == "not_found":
            return None, None
        raise
    try:
        st = _check_regular(fd, MAX_WRITE_BYTES)
        return _read_all(fd, MAX_WRITE_BYTES), stat.S_IMODE(st.st_mode)
    finally:
        os.close(fd)


def _ensure_dirs(root_fd: int, parts: Tuple[str, ...]) -> List[str]:
    """Create missing parent folders, one component at a time, never through a symlink."""
    made, fds, current = [], [], root_fd
    try:
        for name in parts:
            try:
                os.mkdir(name, 0o755, dir_fd=current)
                made.append(name)
            except FileExistsError:
                pass
            except OSError as exc:
                raise FsError("open_failed", f"Could not create the folder {name} ({exc.strerror}).") from None
            current = _open_component(name, current, _DIR_FLAGS)
            fds.append(current)
    finally:
        for fd in reversed(fds):
            os.close(fd)
    return made


def _diff(old: str, new: str, path: str) -> Tuple[str, int, int]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    text = "\n".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n[… diff cut at {MAX_DIFF_CHARS:,} characters]"
    return text, added, removed


def change(root: str, parts: Tuple[str, ...], compute: Callable[[Optional[str]], Tuple[str, str]],
           expect: Optional[str], create_only: bool, intent: Callable[[dict], None]) -> ChangeResult:
    """Apply one change. `compute(old_text or None)` returns (new_text, op).
    `expect`: the sha256 the file must have now (None: no expectation beyond
    what `compute` checks). `create_only`: the file must not exist."""
    if not parts:
        raise FsError("is_directory", "That is the workspace folder itself.")
    path = "/".join(parts)
    with open_root(root) as root_fd:
        if create_only:
            _ensure_dirs(root_fd, parts[:-1])
        with open_dir(root_fd, parts[:-1]) as parent:
            old_bytes, mode = _current(parent, parts[-1])
            if old_bytes is None and expect is not None:
                raise Conflict("conflict", f"{path} no longer exists; it was removed or renamed after it was read.")
            if old_bytes is not None and create_only:
                raise Conflict("exists", f"{path} already exists. Read it, then edit it or replace it with its "
                                         "current hash, instead of creating it.")
            before = sha(old_bytes) if old_bytes is not None else None
            if expect is not None and before != expect:
                raise Conflict("conflict", f"{path} changed since this task last read or wrote it. Read it again "
                                           "and redo the change on what is there now; nothing was written.")
            old_text = _decode(old_bytes) if old_bytes is not None else None
            new_text, op = compute(old_text)
            data = new_text.encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise FsError("too_large", f"The new content is {len(data):,} bytes; the limit is {MAX_WRITE_BYTES:,}.")
            after = sha(data)
            diff, added, removed = _diff(old_text or "", new_text, path)
            if before == after:
                return ChangeResult(path, "already_applied" if op == "edited_already" else "unchanged",
                                    before, after, len(data))
            tmp = f"{TMP_PREFIX}{secrets.token_hex(6)}"
            # The intent is recorded before anything is written (see the module docstring).
            intent({"path": path, "before": before, "after": after, "tmp": "/".join(parts[:-1] + (tmp,)),
                    "op": "create" if before is None else "replace"})
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         mode if mode is not None else 0o644, dir_fd=parent)
            try:
                try:
                    view = memoryview(data)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    if mode is not None:
                        os.fchmod(fd, mode)      # the umask must not narrow the old file's bits
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if before is None:
                    try:
                        os.link(tmp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    except FileExistsError:
                        raise Conflict("exists", f"{path} was created by something else just now; nothing was "
                                                 "overwritten.") from None
                    os.unlink(tmp, dir_fd=parent)
                else:
                    now_bytes, _ = _current(parent, parts[-1])     # last check, just before the rename
                    if now_bytes is None or sha(now_bytes) != before:
                        raise Conflict("conflict", f"{path} changed while the change was being written; "
                                                   "nothing was overwritten.")
                    os.rename(tmp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                try:
                    os.fsync(parent)
                except OSError:
                    pass
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except OSError:
                    pass
                raise
    return ChangeResult(path, "created" if before is None else op, before, after, len(data), added, removed, diff)


def edit_compute(old: str, new: str, path: str) -> Callable[[Optional[str]], Tuple[str, str]]:
    """Replace one exact occurrence of `old` with `new`."""
    def compute(text: Optional[str]) -> Tuple[str, str]:
        if text is None:
            raise FsError("not_found", f"{path} does not exist; use write_file to create it.")
        n = text.count(old)
        if n == 1:
            return text.replace(old, new, 1), "edited"
        if n == 0:
            if new and text.count(new) == 1:
                return text, "edited_already"     # resumed or repeated: the edit is already there
            raise FsError("no_match", f"old_text was not found in {path}. Read the file again; it may differ "
                                      "from what you expect (spaces, line breaks).")
        raise FsError("ambiguous", f"old_text occurs {n} times in {path}. Include more surrounding lines so it "
                                   "matches exactly once.")
    return compute


def remove_tmp(root: str, rel: str) -> bool:
    """Recovery: remove a temporary file a change left behind. Only names with
    the temporary prefix, only through descriptors."""
    parts = tuple(p for p in rel.split("/") if p)
    if not parts or not parts[-1].startswith(TMP_PREFIX) or ".." in parts:
        return False
    try:
        with open_root(root) as root_fd, open_dir(root_fd, parts[:-1]) as parent:
            st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                return False
            os.unlink(parts[-1], dir_fd=parent)
            return True
    except (FsError, OSError):
        return False


def canonical_path(root: str, rel: str) -> str:
    """The path as the disk itself spells it. A task remembers the version of
    a file it read or wrote under this name: on a case-insensitive disk (the
    macOS default) `calc.py` and `CALC.py` are one file, and so are the two
    Unicode forms of an accented name, so a change through either spelling must
    start from the version the task saw. Each component that exists is
    replaced by the folder entry that is the same file (same device and inode)
    and differs only by case or Unicode form; a part that does not exist yet
    is kept as given."""
    parts = tuple(p for p in rel.split("/") if p)
    out: List[str] = []
    try:
        with open_root(root) as root_fd:
            fds: List[int] = []
            cur = root_fd
            try:
                for i, name in enumerate(parts):
                    try:
                        st = os.stat(name, dir_fd=cur, follow_symlinks=False)
                        entries = os.listdir(cur)
                    except OSError:
                        return "/".join(out + list(parts[i:]))
                    actual = name
                    if name not in entries:
                        want = fold(name)
                        for e in entries:
                            if fold(e) == want:
                                try:
                                    est = os.stat(e, dir_fd=cur, follow_symlinks=False)
                                except OSError:
                                    continue
                                if (est.st_dev, est.st_ino) == (st.st_dev, st.st_ino):
                                    actual = e
                                    break
                    out.append(actual)
                    if i < len(parts) - 1:
                        if not stat.S_ISDIR(st.st_mode):
                            return "/".join(out + list(parts[i + 1:]))
                        cur = _open_component(actual, cur, fs._DIR_FLAGS)
                        fds.append(cur)
            finally:
                for fd in reversed(fds):
                    os.close(fd)
    except (FsError, OSError):
        return "/".join(parts)
    return "/".join(out)


def current_hash(root: str, rel: str) -> Tuple[str, Optional[str]]:
    """Recovery and resume: ("present", sha) | ("absent", None) | ("unreadable", None)."""
    parts = tuple(p for p in rel.split("/") if p)
    try:
        with open_root(root) as root_fd, open_dir(root_fd, parts[:-1]) as parent:
            data, _ = _current(parent, parts[-1])
    except FsError as exc:
        return ("absent", None) if exc.code == "not_found" else ("unreadable", None)
    except OSError:
        return "unreadable", None
    return ("absent", None) if data is None else ("present", sha(data))


__all__ = ["find", "change", "canonical_path", "edit_compute", "remove_tmp", "current_hash", "Conflict", "ChangeResult", "FindResult",
           "MAX_WRITE_BYTES", "TMP_PREFIX", "component_flags", "errno"]
