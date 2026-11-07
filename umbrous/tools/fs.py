"""Read-only workspace file access, confined by open file descriptors (M3).

Only the gate (umbrous/gate/) calls these functions. Every path here is a
workspace-relative path that the gate has already validated as text; this
module enforces confinement on the real filesystem.

Why descriptors, not path checks
--------------------------------
Checking a path ("is it inside the workspace? not a symlink?") and then
opening it leaves a gap: between the check and the open, a file or folder can
be replaced by a symlink to somewhere else. Checking a second time just moves
the gap. So nothing here opens a path string. Instead:

1. The workspace root is opened once, as a directory descriptor.
2. Each path component is opened *relative to its parent's descriptor*
   (`os.open(name, ..., dir_fd=parent)`) with O_NOFOLLOW, so a symlink at any
   step makes the open itself fail. Folders also use O_DIRECTORY.
3. A file is opened with O_NONBLOCK (a FIFO cannot hang us), then fstat() is
   called on the open descriptor: only regular files, only files with exactly
   one hard link (a hard link inside the workspace can be another name for a
   file outside it), and only up to the size limit.
4. Content is read from that same descriptor. Whatever the path points to a
   moment later does not matter: we hold the object we checked.

List and search walk the tree the same way (os.scandir on a descriptor and
os.fwalk with follow_symlinks=False), never by joining path strings.

This is not an OS sandbox. It depends on this code being correct; a bug here,
or in Python, could still read anything your user account can read.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Iterator, List, Optional, Tuple

# ---------------------------------------------------------------- limits
MAX_READ_BYTES = 256 * 1024        # largest file read_file will open
MAX_READ_CHARS = 12_000            # most text returned by one read (about 3k tokens)
MAX_LIST_ENTRIES = 300
MAX_SEARCH_FILES = 2_000           # files looked at by one search
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 40
MAX_SEARCH_CHARS = 8_000
MAX_DEPTH = 12
SEARCH_SECONDS = 5.0
SNIFF_BYTES = 8_192

_NOFOLLOW_ANY = getattr(os, "O_NOFOLLOW_ANY", 0)  # macOS: refuse a symlink in ANY component
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

# Files and folders that are never read, listed or searched, under any profile
# and whatever is approved. Compared after Unicode normalisation and case
# folding, because APFS normally treats ".ENV" and ".env" (and composed and
# decomposed accents) as the same name.
EXCLUDED_PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc",
    ".git", ".hg", ".svn",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx",
    "credentials", "credentials.*", "*credentials.json", "secrets", "secrets.*", "*.secret",
    "*.sqlite-wal", "*.sqlite-shm",
)


def fold(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFKC", name).casefold())


_FOLDED_PATTERNS = tuple(fold(p) for p in EXCLUDED_PATTERNS)


def is_excluded(parts: Tuple[str, ...]) -> bool:
    """True if any component (or the pair forming '.config/gcloud') is excluded."""
    folded = [fold(p) for p in parts]
    for i, name in enumerate(folded):
        for pat in _FOLDED_PATTERNS:
            if "/" in pat:
                if "/".join(folded[i:i + pat.count("/") + 1]) == pat:
                    return True
            elif fnmatchcase(name, pat):
                return True
    return False


class FsError(Exception):
    """A refusal or failure with a stable code and a sentence for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ReadResult:
    path: str
    text: str
    sha256: str
    size_bytes: int
    first_line: int
    last_line: int
    total_lines: int
    truncated: bool


@dataclass
class ListResult:
    path: str
    entries: List[Tuple[str, str, Optional[int]]]  # (name, kind, size)
    hidden: int
    truncated: bool


@dataclass
class SearchHit:
    path: str
    line: int
    text: str


@dataclass
class SearchResult:
    query: str
    path: str
    hits: List[SearchHit] = field(default_factory=list)
    files_scanned: int = 0
    skipped: int = 0
    truncated: bool = False
    timed_out: bool = False


# ---------------------------------------------------------------- descriptor walking
@contextmanager
def open_root(root: str) -> Iterator[int]:
    """Open the workspace root folder itself. The root must not be a symlink."""
    try:
        fd = os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        raise FsError("workspace_missing", "The workspace folder no longer exists.") from None
    except NotADirectoryError:
        raise FsError("workspace_missing", "The workspace is not a folder.") from None
    except OSError as exc:
        raise FsError("workspace_unavailable", f"The workspace folder cannot be opened ({exc.strerror}).") from None
    try:
        yield fd
    finally:
        os.close(fd)


def component_flags(flags: int, nofollow_any: int = _NOFOLLOW_ANY) -> int:
    """Flags for opening one path component. Where the system has O_NOFOLLOW_ANY
    (macOS), it REPLACES O_NOFOLLOW: macOS refuses the two together with EINVAL,
    which made every read fail in the app's bundled Python 3.13. Elsewhere
    O_NOFOLLOW stays. Either way a symlink is never followed."""
    if nofollow_any:
        return (flags & ~os.O_NOFOLLOW) | nofollow_any
    return flags


def _open_component(name: str, parent: int, flags: int) -> int:
    try:
        return os.open(name, component_flags(flags), dir_fd=parent)
    except FileNotFoundError:
        raise FsError("not_found", "No such file or folder in the workspace.") from None
    except NotADirectoryError:
        # Linux reports a symlink opened with O_DIRECTORY|O_NOFOLLOW as "not a
        # directory". Look (without following) only to give the right reason;
        # the open has already been refused either way.
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                raise FsError("symlink", "That path goes through a symbolic link, which is never followed.") from None
        except OSError:
            pass
        raise FsError("not_a_directory", "A part of that path is not a folder.") from None
    except OSError as exc:
        # ELOOP (or EMLINK on some BSDs) is what O_NOFOLLOW gives for a symlink.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise FsError("symlink", "That path goes through a symbolic link, which is never followed.") from None
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise FsError("permission", "macOS does not allow reading that file.") from None
        raise FsError("open_failed", f"Could not open it ({exc.strerror}).") from None


@contextmanager
def open_dir(root_fd: int, parts: Tuple[str, ...]) -> Iterator[int]:
    """Descend from the root, one component at a time, never following a symlink."""
    fds: List[int] = []
    current = root_fd
    try:
        for name in parts:
            current = _open_component(name, current, _DIR_FLAGS)
            fds.append(current)
        yield current
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _check_regular(fd: int, limit: int) -> os.stat_result:
    st = os.fstat(fd)
    if stat.S_ISDIR(st.st_mode):
        raise FsError("is_directory", "That is a folder; use list_files for folders.")
    if not stat.S_ISREG(st.st_mode):
        raise FsError("not_regular", "Only ordinary files can be read (not a pipe, device or socket).")
    if st.st_nlink > 1:  # 0 is fine: the file was renamed away after we opened it, and we hold it
        raise FsError("hard_link", "That file has more than one hard link, so it may be a file from outside "
                                   "the workspace under another name. It is not read.")
    if st.st_size > limit:
        raise FsError("too_large", f"The file is {st.st_size:,} bytes; the limit is {limit:,}.")
    return st


def _read_all(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            raise FsError("not_regular", "The file did not behave like an ordinary file.") from None
        if not chunk:
            break
        total += len(chunk)
        if total > limit:  # it grew after fstat
            raise FsError("too_large", f"The file is larger than the {limit:,} byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(data: bytes) -> str:
    if b"\x00" in data[:SNIFF_BYTES]:
        raise FsError("binary", "That looks like a binary file; only text files are read.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FsError("binary", "That file is not UTF-8 text; only text files are read.") from None


def precheck(root: str, parts: Tuple[str, ...], want_file: bool) -> None:
    """Confinement checks only (no reading), used before asking you to approve
    something, so that a symlink, hard link or pipe is refused outright instead
    of showing an approval card for it. Execution checks everything again."""
    with open_root(root) as root_fd:
        if want_file and parts:
            with open_dir(root_fd, parts[:-1]) as parent:
                fd = _open_component(parts[-1], parent, _FILE_FLAGS)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISDIR(st.st_mode):
                        _check_regular(fd, 1 << 62)
                finally:
                    os.close(fd)
        else:
            with open_dir(root_fd, parts):
                pass


# ---------------------------------------------------------------- the three tools
def read_file(root: str, parts: Tuple[str, ...], start_line: int = 1, max_lines: int = 400) -> ReadResult:
    if not parts:
        raise FsError("is_directory", "That is the workspace folder itself; use list_files.")
    with open_root(root) as root_fd, open_dir(root_fd, parts[:-1]) as parent:
        fd = _open_component(parts[-1], parent, _FILE_FLAGS)
        try:
            _check_regular(fd, MAX_READ_BYTES)
            data = _read_all(fd, MAX_READ_BYTES)
        finally:
            os.close(fd)
    text = _decode(data)
    lines = text.splitlines()
    total = len(lines)
    first = max(1, start_line)
    chosen = lines[first - 1:first - 1 + max_lines]
    out, used = [], 0
    for line in chosen:
        if used + len(line) + 1 > MAX_READ_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    last = first + len(out) - 1 if out else first - 1
    return ReadResult(path="/".join(parts), text="\n".join(out), sha256=hashlib.sha256(data).hexdigest(),
                      size_bytes=len(data), first_line=first, last_line=max(last, first - 1),
                      total_lines=total, truncated=last < total)


def list_dir(root: str, parts: Tuple[str, ...]) -> ListResult:
    with open_root(root) as root_fd, open_dir(root_fd, parts) as dfd:
        entries, hidden = [], 0
        with os.scandir(dfd) as it:
            for entry in it:
                if is_excluded(parts + (entry.name,)):
                    hidden += 1
                    continue
                try:
                    if entry.is_symlink():
                        kind, size = "symlink (not followed)", None
                    elif entry.is_dir(follow_symlinks=False):
                        kind, size = "folder", None
                    elif entry.is_file(follow_symlinks=False):
                        kind, size = "file", entry.stat(follow_symlinks=False).st_size
                    else:
                        kind, size = "other", None
                except OSError:
                    kind, size = "unreadable", None
                entries.append((entry.name, kind, size))
    entries.sort(key=lambda e: (e[1] != "folder", fold(e[0])))
    truncated = len(entries) > MAX_LIST_ENTRIES
    return ListResult(path="/".join(parts) or ".", entries=entries[:MAX_LIST_ENTRIES], hidden=hidden,
                      truncated=truncated)


def search(root: str, parts: Tuple[str, ...], query: str, deadline_s: float = SEARCH_SECONDS) -> SearchResult:
    """Case-insensitive plain-text search (not a regular expression)."""
    result = SearchResult(query=query, path="/".join(parts) or ".")
    needle = query.casefold()
    started = time.monotonic()
    chars = 0
    with open_root(root) as root_fd, open_dir(root_fd, parts) as top:
        # fwalk with follow_symlinks=False never descends into a symlinked
        # folder, and checks each folder it opens is the one it listed.
        for dirpath, dirnames, filenames, dfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
            rel = tuple(p for p in dirpath.split("/") if p not in ("", "."))
            if len(rel) >= MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = sorted(d for d in dirnames if not is_excluded(parts + rel + (d,)))
            for name in sorted(filenames):
                if time.monotonic() - started > deadline_s:
                    result.timed_out = result.truncated = True
                    return result
                if result.files_scanned >= MAX_SEARCH_FILES:
                    result.truncated = True
                    return result
                if is_excluded(parts + rel + (name,)):
                    continue
                try:
                    fd = os.open(name, component_flags(_FILE_FLAGS), dir_fd=dfd)
                except OSError:
                    result.skipped += 1  # symlink, vanished, no permission
                    continue
                try:
                    _check_regular(fd, MAX_SEARCH_FILE_BYTES)
                    text = _decode(_read_all(fd, MAX_SEARCH_FILE_BYTES))
                except FsError:
                    result.skipped += 1  # binary, too large, hard link, not regular
                    continue
                finally:
                    os.close(fd)
                result.files_scanned += 1
                if needle not in text.casefold():
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        snippet = line.strip()[:200]
                        if len(result.hits) >= MAX_SEARCH_RESULTS or chars + len(snippet) > MAX_SEARCH_CHARS:
                            result.truncated = True
                            return result
                        result.hits.append(SearchHit("/".join(parts + rel + (name,)), n, snippet))
                        chars += len(snippet)
    return result
