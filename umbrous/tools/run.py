"""Run commands in the workspace folder (M9).

A command runs through `/bin/sh -c` with the workspace folder as its working
directory, its own process group (so Stop and time limits reach every process
it started), no input, and your environment. Its output is captured into
bounded buffers: the first and last parts of stdout and stderr are kept, with
the total size, so a chatty build cannot fill memory or the model's context.

    run      wait up to `timeout_s` for it to exit. If it is still running
             then: with background, it keeps running and an id is returned
             to check later; without, it is stopped (SIGTERM, then SIGKILL)
             and reported as timed out.
    check    wait up to `wait_s` for a background command, then report its
             status and output so far; `stop` ends it.
    Stop     the run loop's cancel token is polled every 0.1 s; the process
             group is stopped at once.

A command never outlives its task: when the task ends, every background
command it started that is still running is stopped (`end_task`).

What this is not: an OS sandbox. A command runs as you and can do anything
you can, inside the workspace or outside it. The working directory is the
workspace; that is a starting point, not a boundary. This is why the Code
profile is something you choose (docs/m9-coding.md).
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

HEAD_BYTES = 2_000          # kept from the start of each stream
TAIL_BYTES = 6_000          # kept from the end of each stream
KILL_GRACE_S = 0.3          # SIGTERM, then SIGKILL after this
POLL_S = 0.05


class Capture:
    """The head and tail of one output stream, and its total size."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            room = HEAD_BYTES - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > TAIL_BYTES:
                    del self.tail[:len(self.tail) - TAIL_BYTES]

    def text(self) -> str:
        with self.lock:
            head, tail, total = bytes(self.head), bytes(self.tail), self.total
        kept = len(head) + len(tail)
        body = head.decode("utf-8", "replace")
        if total > kept:
            body += f"\n[… {total - kept:,} bytes left out …]\n"
        return body + tail.decode("utf-8", "replace")


@dataclass
class Command:
    cmd_id: str
    task_id: str
    command: str
    proc: subprocess.Popen
    started: float
    out: Capture = field(default_factory=Capture)
    err: Capture = field(default_factory=Capture)
    stopped_by: Optional[str] = None       # you | time_limit | task_end | stop
    readers: list = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def done(self) -> bool:
        return self.proc.poll() is not None

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


@dataclass
class CommandResult:
    cmd_id: str
    command: str
    status: str            # exited | running | timed_out | cancelled | stopped
    exit_code: Optional[int]
    ms: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    pid: int


def _pump(stream, cap: Capture) -> None:
    try:
        for chunk in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536), b""):
            cap.add(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def kill_group(pid: int) -> None:
    """Stop a process group: SIGTERM, then SIGKILL if it is still there."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.02)


# Umbrous.app starts its runtime with a minimal PATH (/usr/bin:/bin:...), and a
# command does not load your shell profile. These usual tool folders are added
# at the end when they exist, so `cargo`, `brew`-installed tools and the like
# are found; the system's own come first.
EXTRA_PATH = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "~/.cargo/bin", "~/.local/bin")


def command_path(path: str) -> str:
    parts = [p for p in path.split(os.pathsep) if p]
    for d in EXTRA_PATH:
        d = os.path.expanduser(d)
        if d not in parts and os.path.isdir(d):
            parts.append(d)
    return os.pathsep.join(parts) or "/usr/bin:/bin"


class CommandRegistry:
    """The commands the runtime started that may still be running, by id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cmds: Dict[str, Command] = {}

    def start(self, task_id: str, command: str, cwd: str, started: Callable[[int], None] = lambda pid: None) -> Command:
        env = dict(os.environ)
        env.pop("UMBROUS_SESSION_TOKEN", None)
        env["PATH"] = command_path(env.get("PATH", ""))
        env["UMBROUS_COMMAND"] = "1"          # a test can see it runs under Umbrous
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                env=env, close_fds=True)
        c = Command("cmd_" + secrets.token_hex(6), task_id, command, proc, time.monotonic())
        for stream, cap in ((proc.stdout, c.out), (proc.stderr, c.err)):
            t = threading.Thread(target=_pump, args=(stream, cap), daemon=True)
            t.start()
            c.readers.append(t)
        with self._lock:
            self._cmds[c.cmd_id] = c
        try:
            started(proc.pid)
        except Exception:
            pass
        return c

    def get(self, cmd_id: str, task_id: str) -> Optional[Command]:
        with self._lock:
            c = self._cmds.get(cmd_id)
        return c if c is not None and c.task_id == task_id else None

    def wait(self, c: Command, seconds: float, cancelled: Callable[[], bool]) -> str:
        """exited | running | cancelled"""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if c.done():
                for t in c.readers:
                    t.join(1.0)
                return "exited"
            if cancelled():
                self.stop(c, "you")
                return "cancelled"
            if time.monotonic() >= deadline:
                return "running"
            time.sleep(POLL_S)

    def stop(self, c: Command, by: str) -> None:
        if not c.done():
            c.stopped_by = c.stopped_by or by
            kill_group(c.pid)
            try:
                c.proc.wait(2)
            except subprocess.TimeoutExpired:
                pass
        for t in c.readers:
            t.join(1.0)

    def result(self, c: Command, status: str) -> CommandResult:
        code = c.proc.poll()
        return CommandResult(c.cmd_id, c.command, status, code if status == "exited" else None, c.ms(),
                             c.out.text(), c.err.text(), c.out.total, c.err.total, c.pid)

    def forget(self, c: Command) -> None:
        with self._lock:
            self._cmds.pop(c.cmd_id, None)

    def end_task(self, task_id: str) -> int:
        """Stop every command of a task that is still running. Returns how many."""
        with self._lock:
            mine = [c for c in self._cmds.values() if c.task_id == task_id]
            for c in mine:
                self._cmds.pop(c.cmd_id, None)
        n = 0
        for c in mine:
            if not c.done():
                self.stop(c, "task_end")
                n += 1
        return n

    def running(self, task_id: str) -> int:
        with self._lock:
            return sum(1 for c in self._cmds.values() if c.task_id == task_id and not c.done())

    def live(self) -> List[Command]:
        """Every command still running, of any task (for status and tests)."""
        with self._lock:
            return [c for c in self._cmds.values() if not c.done()]


def process_alive(pid: int) -> bool:
    """Recovery: is a process group with this id still there (a command left
    running by a runtime that died)? Its leader was started in its own
    session, so its group id is its pid."""
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False
