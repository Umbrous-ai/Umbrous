"""Bounded, read-only working-folder review. Never runs hooks or content filters.

Git supplies object IDs and path lists only. Working files use the same confined
file-descriptor reader as the agent tools; Git never opens working-file content.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path
import subprocess
import tempfile
import time

from umbrous.tools import fs

MAX_FILES = 60
MAX_SCAN = 1000
MAX_BYTES = 256 * 1024
MAX_PATCH = 24_000
MAX_TOTAL = 240_000
SECONDS = 8.0


class ReviewError(Exception):
    pass


def _git(root: str, args: list[str], deadline: float, limit: int = MAX_BYTES) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    command = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
               '-C', root, *args]
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(command, env=env, stdout=out, stderr=subprocess.DEVNULL)
        except OSError:
            raise ReviewError('Git is unavailable. Install Git and retry.') from None
        try:
            while p.poll() is None:
                if time.monotonic() >= deadline:
                    raise ReviewError('Review reached its time limit. Showing only files already checked.')
                if os.fstat(out.fileno()).st_size > limit:
                    raise ReviewError('Git output exceeds the review size limit.')
                time.sleep(.005)
            if p.returncode:
                raise ReviewError('Git could not read this repository.')
            if os.fstat(out.fileno()).st_size > limit:
                raise ReviewError('Git output exceeds the review size limit.')
            out.seek(0)
            return out.read(limit + 1)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()


def _working(root: str, parts: tuple[str, ...]) -> tuple[bytes, bytes]:
    with fs.open_root(root) as root_fd, fs.open_dir(root_fd, parts[:-1]) as parent:
        fd = fs._open_component(parts[-1], parent, fs._FILE_FLAGS)
        try:
            st = fs._check_regular(fd, MAX_BYTES)
            return fs._read_all(fd, MAX_BYTES), b'100755' if st.st_mode & 0o111 else b'100644'
        finally:
            os.close(fd)


def review_folder(root: str) -> dict:
    out = dict(available=True, message='Net current folder changes compared with HEAD, including edits outside this chat. '
               'Staged changes are not shown separately. This read-only snapshot may change while work runs.', files=[], truncated=False, omitted=0)
    deadline = time.monotonic() + SECONDS
    try:
        top = _git(root, ['rev-parse', '--show-toplevel'], deadline).decode().strip()
        if Path(top).resolve() != Path(root).resolve():
            return dict(out, available=False, message='Choose the repository root folder to review Git changes.')
        # ls-tree reads immutable objects; ls-files reads the index and directory
        # names. Unlike git diff/status, neither executes clean filters.
        try:
            tree = _git(root, ['ls-tree', '-rz', '--full-tree', 'HEAD'], deadline, 1024 * 1024)
        except ReviewError as exc:
            # Only an unborn branch may use an empty baseline. A damaged or
            # oversized existing tree must not masquerade as a new repository.
            branch = _git(root, ['symbolic-ref', '-q', 'HEAD'], deadline).decode().strip()
            refs = _git(root, ['for-each-ref', '--format=%(refname)'], deadline).decode().splitlines()
            if branch in refs:
                raise exc
            tree = b''
    except ReviewError as exc:
        return dict(out, available=False, message=str(exc))
    try:
        base = {}
        for entry in tree.split(b'\0'):
            if not entry:
                continue
            info, path = entry.split(b'\t', 1)
            mode, kind, oid = info.split()
            base[path] = (mode, kind, oid)
        cached = set(x for x in _git(root, ['ls-files', '-z', '--cached'], deadline, 1024 * 1024).split(b'\0') if x)
        names = set(base) | cached
        names.update(x for x in _git(root, ['ls-files', '-z', '--others', '--exclude-standard'], deadline, 1024 * 1024).split(b'\0') if x)
        scanned, total = 0, 0
        for raw in sorted(names):
            if scanned >= MAX_SCAN or len(out['files']) >= MAX_FILES or total >= MAX_TOTAL or time.monotonic() >= deadline:
                out['truncated'] = True
                out['message'] += ' Review limit reached; remaining files were not checked.'
                break
            scanned += 1
            try:
                path = raw.decode('utf-8')
            except UnicodeDecodeError:
                out['omitted'] += 1
                continue
            parts = tuple(path.split('/'))
            if any(p in ('', '.', '..') for p in parts) or fs.is_excluded(parts):
                out['omitted'] += 1
                continue
            previous = base.get(raw)
            if previous and (previous[0] not in (b'100644', b'100755') or previous[1] != b'blob'):
                out['omitted'] += 1
                continue
            item = dict(path=path, status='modified' if previous else ('added' if raw in cached else 'untracked'), patch='', binary=False, truncated=False)
            try:
                try:
                    current, current_mode = _working(root, parts)
                except fs.FsError as exc:
                    if exc.code == 'not_found' and previous:
                        current, current_mode = b'', b''
                        item['status'] = 'deleted'
                    else:
                        raise
                if previous:
                    oid = previous[2].decode()
                    if int(_git(root, ['cat-file', '-s', oid], deadline, 64)) > MAX_BYTES:
                        raise ReviewError('The committed file exceeds the review size limit.')
                    old = _git(root, ['cat-file', 'blob', oid], deadline)
                else:
                    old = b''
                if previous and current == old and current_mode == previous[0] and item['status'] != 'deleted':
                    continue
                old_text, new_text = fs._decode(old), fs._decode(current)
                lines = difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                            fromfile='a/' + path if previous else '/dev/null',
                            tofile='/dev/null' if item['status'] == 'deleted' else 'b/' + path)
                patch = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
                if previous and current_mode and previous[0] != current_mode:
                    patch = f'old mode {previous[0].decode()}\nnew mode {current_mode.decode()}\n' + patch
                budget = min(MAX_PATCH, MAX_TOTAL - total)
                item['patch'] = patch[:budget]
                item['truncated'] = len(patch) > budget
                total += len(item['patch'])
                if item['truncated']:
                    out['truncated'] = True
            except fs.FsError as exc:
                item['binary'] = exc.code == 'binary'
                item['error'] = exc.message
            except ReviewError as exc:
                item['error'] = str(exc)
                item['truncated'] = True
                out['truncated'] = True
            out['files'].append(item)
        if out['omitted']:
            out['message'] += ' Excluded paths and unsupported Git entries are omitted.'
    except ReviewError as exc:
        out.update(available=False, message=str(exc))
    return out
