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

A command never outlives its task: when the task ends, every command it
started whose process group still has a process in it is stopped
(`end_task`). That includes a command whose shell has exited but left a
process behind (`sleep 60 &`): the shell exiting ends the command's result,
not its group.

Who owns a group. A command's process group is named by its leader's process
id, and an id is free for reuse only once that process has been reaped. So
the leader is not the shell but a small supervisor (`SUPERVISOR`): it starts
the shell in the group, reports its exit status on a pipe, and then stays
until the runtime lets it go. The runtime reaps it only after it has finished
signalling the group, and never signals a group after that (`released`). For
as long as Umbrous may signal a group, the group's id cannot belong to
anything else. If the runtime dies, the supervisor sees its pipe close and
stops the group itself.

Each command's leader is recorded with its identity: the boot it ran in and
its start time (`identity`). After a restart, recovery signals a group only
when its leader is still the very process that was started; a process that
merely reuses the id is left alone (`same_process`).

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
import sys
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
    identity: Optional[dict] = None
    ctl: int = -1                          # the supervisor stays while this is open
    code: Optional[int] = None             # the shell's exit status (None: killed with the supervisor)
    exited: threading.Event = field(default_factory=threading.Event)
    released: bool = False                 # the supervisor was reaped: never signal this group again
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def pid(self) -> int:
        """The supervisor's id, which is the process group's id."""
        return self.proc.pid

    def done(self) -> bool:
        """The shell has exited."""
        return self.exited.is_set()

    def lingering(self) -> bool:
        """The shell has exited but left processes running in its group."""
        if not self.done() or self.released:
            return False
        left = _running_members(self.pid, besides=self.pid)
        return left is None or bool(left)

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


# The group's leader. argv: command, status fd, control fd. It runs the shell
# as its child, writes the shell's exit status to the status pipe, then waits
# on the control pipe: "x" from the runtime means let go (exit now); the pipe
# closing without it means the runtime died, so it stops the group first.
SUPERVISOR = r"""
import os, select, signal, sys, time
cmd, st, ctl = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
pid = os.fork()
if pid == 0:
    try:
        os.close(st); os.close(ctl)
        for s in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGINT):
            signal.signal(s, signal.SIG_DFL)
        os.execv("/bin/sh", ["/bin/sh", "-c", cmd])
    finally:
        os._exit(127)
null = os.open(os.devnull, os.O_RDWR)
os.dup2(null, 0); os.dup2(null, 1); os.dup2(null, 2)
wake_r, wake_w = os.pipe()
os.set_blocking(wake_w, False)
signal.set_wakeup_fd(wake_w)
signal.signal(signal.SIGCHLD, lambda *a: None)
code, got = None, b""
while True:
    if code is None:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done, status = pid, 0
        if done == pid:
            code = -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)
            os.write(st, b"%d\n" % code)
            os.close(st)
    ready, _, _ = select.select([ctl, wake_r], [], [], 1.0)
    if wake_r in ready:
        os.read(wake_r, 512)
    if ctl in ready:
        chunk = os.read(ctl, 16)
        if not chunk:
            break
        got += chunk
if b"x" not in got:
    # the runtime went away without letting go: leave nothing running behind it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.killpg(0, signal.SIGTERM)
        time.sleep(0.3)
        os.killpg(0, signal.SIGKILL)
    except OSError:
        pass
"""


def _watch_status(fd: int, c: "Command") -> None:
    data = b""
    try:
        while True:
            chunk = os.read(fd, 64)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    finally:
        os.close(fd)
    try:
        c.code = int(data.strip())
    except ValueError:
        c.code = None                      # the supervisor was killed with the group
    c.exited.set()


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


def group_alive(pgid: int) -> bool:
    """Is there still a running process in this group that we may signal?
    While the group has a member, its id cannot be given to another process,
    so the group is still the command's. A member that has exited but not yet
    been reaped (a zombie) does not count."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return _running_members(pgid) != []


def _running_members(pgid: int, besides: int = 0) -> Optional[List[int]]:
    """The group's members that are running (not zombies), other than `besides`;
    None if they cannot be listed."""
    out: List[int] = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if len(fields) > 2 and fields[2] == str(pgid) and not fields[0].startswith("Z") and int(name) != besides:
                out.append(int(name))
        return out
    try:
        lines = subprocess.run(["/bin/ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=5,
                               env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    for f in (ln.split() for ln in lines):
        if len(f) >= 3 and f[1] == str(pgid) and not f[2].startswith("Z") and f[0].isdigit() and int(f[0]) != besides:
            out.append(int(f[0]))
    return out


# ---------------------------------------------------------------- identity
def _boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except OSError:
        pass
    try:   # macOS: "{ sec = 1762560000, usec = 123456 } Sat Nov  8 …"
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                             timeout=5).stdout
        return out.split("}")[0].strip(" {") or None
    except (OSError, subprocess.SubprocessError):
        return None


_BOOT: List[Optional[str]] = []


def boot_id() -> Optional[str]:
    if not _BOOT:
        _BOOT.append(_boot_id())
    return _BOOT[0]


def start_time(pid: int) -> Optional[str]:
    """When the process started, as the system reports it (None if it is gone)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return "jiffies:" + f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    if os.path.exists("/proc/self/stat"):
        return None                 # Linux, and the process is gone
    try:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout.strip()
        return ("lstart:" + " ".join(out.split())) if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def identity(pid: int) -> Optional[dict]:
    """What tells this process apart from a later one that reuses its id."""
    boot, start = boot_id(), start_time(pid)
    return {"boot": boot, "start": start} if boot and start else None


def same_process(pid: int, ident) -> bool:
    """Recovery: is `pid` still the process recorded as `ident`, and still the
    leader of its own group? False whenever that cannot be shown."""
    if not isinstance(ident, dict) or not ident.get("boot") or not ident.get("start"):
        return False
    if not process_alive(pid):
        return False
    return identity(pid) == {"boot": ident["boot"], "start": ident["start"]}


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

    def start(self, task_id: str, command: str, cwd: str,
              started: Callable[[int, Optional[dict]], None] = lambda pid, ident: None) -> Command:
        env = dict(os.environ)
        env.pop("UMBROUS_SESSION_TOKEN", None)
        env["PATH"] = command_path(env.get("PATH", ""))
        env["UMBROUS_COMMAND"] = "1"          # a test can see it runs under Umbrous
        st_r, st_w = os.pipe()
        ctl_r, ctl_w = os.pipe()
        for fd in (st_r, ctl_w):
            os.set_inheritable(fd, False)
        try:
            proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", SUPERVISOR, command, str(st_w), str(ctl_r)],
                                    cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, env=env, close_fds=True,
                                    pass_fds=(st_w, ctl_r))
        except BaseException:
            for fd in (st_r, st_w, ctl_r, ctl_w):
                os.close(fd)
            raise
        os.close(st_w)
        os.close(ctl_r)
        c = Command("cmd_" + secrets.token_hex(6), task_id, command, proc, time.monotonic(), ctl=ctl_w)
        c.identity = identity(proc.pid)
        threading.Thread(target=_watch_status, args=(st_r, c), daemon=True).start()
        for stream, cap in ((proc.stdout, c.out), (proc.stderr, c.err)):
            t = threading.Thread(target=_pump, args=(stream, cap), daemon=True)
            t.start()
            c.readers.append(t)
        with self._lock:
            self._cmds[c.cmd_id] = c
        try:
            started(proc.pid, c.identity)
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
        """Stop the command's whole process group (its shell if it is still
        running, and whatever it started), then let the group go."""
        if not c.done():
            c.stopped_by = c.stopped_by or by
        self._kill_owned(c)
        c.exited.wait(2)
        for t in c.readers:
            t.join(1.0)
        self.release(c)

    @staticmethod
    def _kill_owned(c: Command) -> None:
        """SIGTERM, then SIGKILL, to a group this registry still owns: its
        supervisor is not reaped, so the id is still this command's."""
        with c.lock:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if c.released:
                    return
                try:
                    os.killpg(c.pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
                deadline = time.monotonic() + KILL_GRACE_S
                while time.monotonic() < deadline:
                    if _running_members(c.pid) == []:
                        return
                    time.sleep(0.03)

    @staticmethod
    def release(c: Command) -> None:
        """Let the supervisor go and reap it. After this the group's id may be
        reused, so the group is never signalled again."""
        with c.lock:
            if c.released:
                return
            try:
                os.write(c.ctl, b"x")
            except OSError:
                pass
            try:
                os.close(c.ctl)
            except OSError:
                pass
            try:
                c.proc.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    c.proc.kill()      # its own pid: still ours until reaped
                except OSError:
                    pass
                c.proc.wait()
            c.released = True

    def result(self, c: Command, status: str) -> CommandResult:
        return CommandResult(c.cmd_id, c.command, status, c.code if status == "exited" else None, c.ms(),
                             c.out.text(), c.err.text(), c.out.total, c.err.total, c.pid)

    def forget(self, c: Command) -> None:
        """Done with its result. A command that left processes behind stays
        registered (and its group owned), so `end_task` stops them."""
        if c.lingering():
            return
        self.release(c)
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
            if not c.done() or c.lingering():
                self.stop(c, "task_end")
                n += 1
            else:
                self.release(c)
        return n

    def running(self, task_id: str) -> int:
        with self._lock:
            return sum(1 for c in self._cmds.values() if c.task_id == task_id and (not c.done() or c.lingering()))

    def live(self) -> List[Command]:
        """Every command still running, of any task (for status and tests)."""
        with self._lock:
            return [c for c in self._cmds.values() if not c.done() or c.lingering()]


def process_alive(pid: int) -> bool:
    """Recovery: is a process group with this id still there (a command left
    running by a runtime that died)? Its leader was started in its own
    session, so its group id is its pid."""
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False
