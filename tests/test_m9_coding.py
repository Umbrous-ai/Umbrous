"""M9: coding in a project folder, and continuing after an interruption.

  ToolTests       the edit and command tools through the gate: create, edit,
                  replace, conflicts, workspace boundaries, atomic writes,
                  commands that pass, fail, time out, run in the background,
                  are checked, stopped, cancelled, and end with their task.
  ProfileTests    which profile offers and allows what.
  LoopTests       a whole coding task in the running runtime: the fake coder
                  edits calc.py, runs the tests, fixes the failure and
                  finishes; "work on /path"; Stop during a command; the
                  self-source note; the report.
  RecoveryTests   the record a crash leaves around a change or a command,
                  written directly, then a restart: before the write, after
                  it, a conflict, an interrupted command.
  CrashTests      the real runtime killed with SIGKILL while the tests run,
                  then resumed: the edit is kept, not made twice, and the
                  task finishes.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_ollama import CODER_TEST, FakeModel, FakeOllama, coder, scripted
from tests.helpers import RunningRuntime
from tests.test_m3_fs import make_fixture
from tests.test_m5_resume import Child
from umbrous.gate.access import AccessStore
from umbrous.gate.gate import ALLOW, ASK, BLOCK, Call, Gate, ToolContext, offered
from umbrous.persistence import db
from tests.helpers import project_fixture_db
from umbrous.receipts.model import new_action_id, new_task_id
from umbrous.receipts.store import ReceiptStore
from umbrous.tools import edit
from umbrous.tools.run import SUPERVISOR, CommandRegistry, _running_members, identity, process_alive

CALC = "def add(a, b):\n    return a + b\n\n\n# TODO: multiply\n"
TESTS = ("import unittest\nfrom calc import add, multiply\n\n\nclass T(unittest.TestCase):\n"
         "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
         "    def test_multiply(self):\n        self.assertEqual(multiply(3, 4), 12)\n\n\n"
         "if __name__ == '__main__':\n    unittest.main()\n")
FIXED = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"


def sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def make_project(base: Path) -> Path:
    p = base / "proj"
    p.mkdir()
    (p / "calc.py").write_text(CALC)
    (p / "test_calc.py").write_text(TESTS)
    (p / ".env").write_text("TOKEN=never\n")
    (p / ".git").mkdir()
    (p / ".git" / "config").write_text("[core]\n")
    return p


def _exists(pid: int) -> bool:
    """A process with this id exists (and is not a zombie we could still reap)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return True
    return bool(st) and not st.startswith("Z")


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "Umbrous-Case-Probe"
    probe.write_text("")
    try:
        return (folder / "umbrous-case-probe").exists()
    finally:
        probe.unlink()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = make_fixture(base)
        (self.ws / "calc.py").write_text(CALC)
        self.home = base / "home"
        self.home.mkdir()
        project_fixture_db(self.home / "u.db").close()
        self.access = AccessStore(self.home / "u.db", self.home)
        self.receipts = ReceiptStore(self.home / "u.db")
        self.gate = Gate(self.access, self.receipts)
        self.access.attach("prj_general", str(self.ws), "workspace_code")
        self.task = new_task_id()
        self.intents = []
        self.commands = CommandRegistry()
        self.ctx = ToolContext(intent=self.intents.append, commands=self.commands, command_timeout_s=10)

    def tearDown(self):
        self.commands.end_task(self.task)
        self.tmp.cleanup()

    def call(self, tool, cancelled=lambda: False, **args):
        return self.gate.run(self.task, "prj_general", None, Call(tool, args), cancelled=cancelled, ctx=self.ctx)

    def leftovers(self):
        return [n for n in os.listdir(self.ws) if n.startswith(".umbrous-tmp")]

    # ---------------------------------------------------------------- files
    def test_create_a_file_in_a_new_folder(self):
        out = self.call("write_file", path="src/new/hello.py", content="print('hi')\n")
        self.assertEqual((out.decision, out.observed["op"]), ("allowed", "created"))
        self.assertEqual((self.ws / "src/new/hello.py").read_text(), "print('hi')\n")
        self.assertEqual(self.intents[0]["op"], "create")
        self.assertIsNone(self.intents[0]["before"])
        self.assertEqual(self.intents[0]["after"], sha("print('hi')\n"))
        self.assertEqual(out.receipt["evidence"][0]["kind"], "change")
        self.assertIn("+print('hi')", out.detail)

    def test_create_refuses_to_overwrite_an_existing_file(self):
        out = self.call("write_file", path="calc.py", content="x = 1\n")
        self.assertEqual((out.decision, out.code), ("failed", "exists"))
        self.assertEqual((self.ws / "calc.py").read_text(), CALC)
        self.assertEqual(self.intents, [])           # nothing was about to be written

    def test_targeted_edit_after_reading(self):
        self.call("read_file", path="calc.py")
        out = self.call("edit_file", path="calc.py", old_text="# TODO: multiply\n",
                        new_text="def multiply(a, b):\n    return a * b\n")
        self.assertEqual((out.decision, out.observed["op"]), ("allowed", "edited"), out.message)
        self.assertEqual((self.ws / "calc.py").read_text(), FIXED)
        self.assertEqual((out.observed["added"], out.observed["removed"]), (2, 1))
        self.assertEqual(self.intents[0]["before"], sha(CALC))
        self.assertEqual(self.leftovers(), [])

    def test_edit_without_a_read_still_checks_the_current_version(self):
        out = self.call("edit_file", path="calc.py", old_text="# TODO: multiply\n", new_text="# later\n")
        self.assertEqual(out.decision, "allowed")     # no expectation: the exact old_text is the check
        out = self.call("edit_file", path="calc.py", old_text="return a + b", new_text="return b + a",
                        expected_sha256="0" * 64)
        self.assertEqual((out.decision, out.code), ("failed", "conflict"))

    def test_edit_results_no_match_ambiguous_and_already_applied(self):
        out = self.call("edit_file", path="calc.py", old_text="not there", new_text="x")
        self.assertEqual((out.decision, out.code), ("failed", "no_match"))
        (self.ws / "two.py").write_text("x = 1\nx = 1\n")
        out = self.call("edit_file", path="two.py", old_text="x = 1", new_text="x = 2")
        self.assertEqual((out.decision, out.code), ("failed", "ambiguous"))
        self.call("edit_file", path="calc.py", old_text="# TODO: multiply\n", new_text="# done\n")
        again = self.call("edit_file", path="calc.py", old_text="# TODO: multiply\n", new_text="# done\n")
        self.assertEqual((again.decision, again.observed["op"]), ("allowed", "already_applied"))
        self.assertEqual((self.ws / "calc.py").read_text().count("# done"), 1)   # not made twice

    def test_replace_a_whole_file_needs_the_version_it_replaces(self):
        r = self.call("read_file", path="calc.py")
        self.assertNotIn(sha(CALC), r.model_text)        # not shown: the task's own read is the expected version
        out = self.call("write_file", path="calc.py", content=FIXED)
        self.assertEqual((out.decision, out.observed["op"]), ("allowed", "replaced"))
        self.assertEqual((self.ws / "calc.py").read_text(), FIXED)
        same = self.call("write_file", path="calc.py", content=FIXED)
        self.assertEqual(same.observed["op"], "unchanged")

    def test_a_newer_edit_is_reported_not_overwritten(self):
        self.call("read_file", path="calc.py")
        (self.ws / "calc.py").write_text(CALC + "# edited by Freddy meanwhile\n")
        for tool, args in (("write_file", {"content": FIXED}),
                           ("edit_file", {"old_text": "# TODO: multiply\n", "new_text": "pass\n"})):
            out = self.call(tool, path="calc.py", **args)
            self.assertEqual((out.decision, out.code), ("failed", "conflict"), tool)
            self.assertIn("changed since", out.message)
        self.assertIn("edited by Freddy", (self.ws / "calc.py").read_text())
        self.assertEqual(self.intents, [])

    def test_conflict_found_at_the_last_moment_leaves_the_file_alone(self):
        self.call("read_file", path="calc.py")
        real = self.ctx.intent

        def meanwhile(data):           # someone writes between our check and our rename
            real(data)
            (self.ws / "calc.py").write_text("# theirs\n")
        self.ctx.intent = meanwhile
        out = self.call("edit_file", path="calc.py", old_text="# TODO: multiply\n", new_text="pass\n")
        self.assertEqual((out.decision, out.code), ("failed", "conflict"))
        self.assertEqual((self.ws / "calc.py").read_text(), "# theirs\n")
        self.assertEqual(self.leftovers(), [])

    def test_the_write_is_atomic_and_keeps_the_mode(self):
        os.chmod(self.ws / "calc.py", 0o751)
        self.call("read_file", path="calc.py")
        seen = {}

        def look(data):                # at intent time, the file is still the old one
            seen["during"] = (self.ws / "calc.py").read_text()
        self.ctx.intent = look
        self.call("write_file", path="calc.py", content=FIXED)
        self.assertEqual(seen["during"], CALC)
        self.assertEqual(os.stat(self.ws / "calc.py").st_mode & 0o777, 0o751)
        self.assertEqual(self.leftovers(), [])

    def test_no_intent_means_no_write(self):
        def broken(data):
            raise OSError("disk full")
        self.ctx.intent = broken
        out = self.call("write_file", path="new.txt", content="x")
        self.assertEqual((out.decision, out.code), ("uncertain", "tool_crashed"))
        self.assertFalse((self.ws / "new.txt").exists())
        self.assertEqual(self.leftovers(), [])

    def test_boundaries(self):
        cases = [("../outside/x.txt", "outside_workspace"), ("/etc/passwd", "outside_workspace"),
                 (".env", "excluded"), (".git/config", "excluded"), ("notes/.ENV", "excluded"),
                 ("deploy.PEM", "excluded")]
        for path, code in cases:
            for tool, args in (("write_file", {"content": "x"}), ("edit_file", {"old_text": "a", "new_text": "b"})):
                out = self.call(tool, path=path, **args)
                self.assertEqual((out.decision, out.code), ("denied", code), (tool, path))
        for path, code in (("link.txt", "symlink"), ("linkdir/x.txt", "symlink"), ("hard.txt", "hard_link")):
            out = self.call("write_file", path=path, content="x", expected_sha256="0" * 64)
            self.assertEqual(out.decision, "denied", (path, out.code, out.message))
        self.assertNotIn("x", (self.ws.parent / "outside" / "secret.txt").read_text()[-2:])

    def test_find_files(self):
        out = self.call("find_files", pattern="*.md")
        self.assertIn("notes/plan.md", out.model_text)
        out = self.call("find_files", pattern="env")
        self.assertNotIn(".env", out.model_text.split("<<<BEGIN WORKSPACE DATA>>>")[1])
        self.assertIn("0 match(es)", self.call("find_files", pattern="config").model_text)

    # ---------------------------------------------------------------- commands
    def test_command_success_and_failure(self):
        ok = self.call("run_command", command="echo hello; echo oops >&2")
        self.assertEqual((ok.decision, ok.observed["status"], ok.observed["exit"]), ("allowed", "exited", 0))
        self.assertIn("hello", ok.model_text)
        self.assertIn("oops", ok.model_text)
        bad = self.call("run_command", command="exit 3")
        self.assertEqual((bad.decision, bad.observed["exit"]), ("allowed", 3))
        self.assertIn("exited with status 3", bad.model_text)
        self.assertIn("(no output)", bad.model_text)                   # empty streams are not shown
        quiet = self.call("run_command", command="echo hello")
        self.assertIn("--- stdout (6 bytes) ---\nhello", quiet.model_text)
        self.assertNotIn("stderr", quiet.model_text)
        self.assertEqual(self.intents[0]["op"], "command")

    def test_command_runs_in_the_workspace_without_the_session_token(self):
        os.environ["UMBROUS_SESSION_TOKEN"] = "tok-secret"
        try:
            out = self.call("run_command", command="pwd; echo T=${UMBROUS_SESSION_TOKEN:-none}")
        finally:
            del os.environ["UMBROUS_SESSION_TOKEN"]
        self.assertIn(os.path.realpath(self.ws), out.model_text)
        self.assertIn("T=none", out.model_text)

    def test_command_path_adds_the_usual_tool_folders_last(self):
        from umbrous.tools.run import command_path
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".cargo", "bin"))
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                p = command_path("/usr/bin:/bin").split(":")
            finally:
                os.environ["HOME"] = old
            self.assertEqual(p[:2], ["/usr/bin", "/bin"])
            self.assertIn(os.path.join(home, ".cargo", "bin"), p)
            self.assertEqual(len(p), len(set(p)))

    def test_timeout_stops_the_command(self):
        t0 = time.monotonic()
        out = self.call("run_command", command="sleep 30", timeout_s=1)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(out.observed["status"], "timed_out")
        self.assertIn("did not finish within its time limit", out.model_text)
        self.assertEqual(self.commands.live(), [])

    def test_background_command_can_be_checked_and_stopped(self):
        out = self.call("run_command", command="echo started; sleep 30", timeout_s=1, background=True)
        self.assertEqual(out.observed["status"], "running")
        cid = out.observed["cmd_id"]
        chk = self.call("check_command", id=cid, wait_s=0)
        self.assertEqual(chk.observed["status"], "running")
        self.assertIn("started", chk.model_text)
        stop = self.call("check_command", id=cid, stop=True)
        self.assertEqual(stop.observed["status"], "stopped")
        self.assertEqual(self.call("check_command", id=cid).code, "no_such_command")

    def test_background_command_finishes_and_is_checked(self):
        out = self.call("run_command", command="sleep 2; echo done", timeout_s=1, background=True)
        chk = self.call("check_command", id=out.observed["cmd_id"], wait_s=10)
        self.assertEqual((chk.observed["status"], chk.observed["exit"]), ("exited", 0))
        self.assertIn("done", chk.model_text)

    def test_cancel_stops_a_command_quickly(self):
        flag = {"stop": False}
        threading.Timer(0.5, lambda: flag.update(stop=True)).start()
        t0 = time.monotonic()
        out = self.call("run_command", command="sleep 30", cancelled=lambda: flag["stop"])
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual((out.decision, out.code), ("failed", "cancelled"))

    def test_ending_the_task_stops_its_background_commands(self):
        out = self.call("run_command", command="sleep 30", timeout_s=1, background=True)
        pid = self.commands.get(out.observed["cmd_id"], self.task).pid
        self.assertTrue(process_alive(pid))
        self.assertEqual(self.commands.end_task(self.task), 1)
        time.sleep(0.5)
        self.assertFalse(process_alive(pid))

    def test_ending_the_task_stops_what_a_finished_command_left_running(self):
        # the shell exits at once; the sleep it started stays in its process group
        out = self.call("run_command", command="sleep 60 & echo $!")
        self.assertEqual((out.observed["status"], out.observed["exit"]), ("exited", 0), out.model_text)
        child = int(out.detail.strip().splitlines()[-1])
        self.assertTrue(process_alive(child) or _exists(child))
        self.assertIn("still running", out.model_text)          # the model is told
        self.assertEqual(self.commands.end_task(self.task), 1)
        deadline = time.monotonic() + 3
        while _exists(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_exists(child), "the child outlived its task")

    def test_a_group_is_owned_until_it_is_released_and_never_signalled_after(self):
        # the shell exits, then what it left exits too: the group is empty, but its id
        # stays taken (by the supervisor) until the task ends, so it cannot be reused
        out = self.call("run_command", command="sleep 1.5 >/dev/null 2>&1 & echo started")
        c = self.commands.get(out.observed["cmd_id"], self.task)
        self.assertIsNotNone(c, "a command that left a process behind stays registered")
        self.assertTrue(c.lingering())
        deadline = time.monotonic() + 5
        while c.lingering() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(c.lingering())
        self.assertEqual(os.getpgid(c.pid), c.pid)        # the id is still the command's group
        self.assertFalse(c.released)
        self.commands.end_task(self.task)
        self.assertTrue(c.released)
        self.assertFalse(process_alive(c.pid))
        # once released (reaped), the id may belong to anything: it is never signalled again
        sent = []
        real = os.killpg
        os.killpg = lambda pgid, sig: sent.append((pgid, sig))
        try:
            self.commands.stop(c, "stop")
            self.commands._kill_owned(c)
        finally:
            os.killpg = real
        self.assertEqual(sent, [])

    def test_the_group_is_stopped_if_the_runtime_goes_away(self):
        out = self.call("run_command", command="sleep 60", timeout_s=1, background=True)
        c = self.commands.get(out.observed["cmd_id"], self.task)
        os.close(c.ctl)                                   # as when the runtime dies: no "let go"
        c.ctl = os.open(os.devnull, os.O_WRONLY)          # (so cleanup has something to close)
        deadline = time.monotonic() + 3
        while _running_members(c.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(_running_members(c.pid), [], "the command outlived the runtime")

    def test_a_broken_status_pipe_still_stops_the_group(self):
        # the runtime goes away just as the shell finishes: writing the status fails,
        # and the supervisor must still stop what the shell left running
        pidfile = self.ws / "child.pid"
        st_r, st_w = os.pipe()
        ctl_r, ctl_w = os.pipe()
        os.close(st_r)                                    # nobody will read the status
        sup = subprocess.Popen([sys.executable, "-I", "-S", "-c", SUPERVISOR,
                                f"sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}; sleep 0.3",
                                str(st_w), str(ctl_r)], pass_fds=(st_w, ctl_r), start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(st_w)
        os.close(ctl_r)
        try:
            sup.wait(5)                                   # the control pipe is still open: it left on its own
            deadline = time.monotonic() + 3
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pidfile.read_text())
            deadline = time.monotonic() + 3
            while _exists(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_exists(child), "the shell's child outlived its supervisor")
        finally:
            os.close(ctl_w)
            if sup.poll() is None:
                sup.kill()
                sup.wait()
            try:
                os.killpg(sup.pid, signal.SIGKILL)
            except OSError:
                pass

    def test_a_command_that_left_nothing_running_is_forgotten(self):
        self.call("run_command", command="true")
        self.assertEqual(self.commands.running(self.task), 0)
        self.assertEqual(self.commands.end_task(self.task), 0)

    def test_a_remembered_version_is_found_through_the_disk_spelling(self):
        self.assertEqual(edit.canonical_path(str(self.ws), "calc.py"), "calc.py")
        self.assertEqual(edit.canonical_path(str(self.ws), "src/new/x.py"), "src/new/x.py")   # not there yet
        if not _case_insensitive(self.ws):
            self.skipTest("this disk tells calc.py and CALC.py apart")
        self.assertEqual(edit.canonical_path(str(self.ws), "CALC.py"), "calc.py")
        self.call("read_file", path="calc.py")
        (self.ws / "calc.py").write_text(CALC + "# changed by someone else\n")
        out = self.call("edit_file", path="CALC.py", old_text="# TODO: multiply\n", new_text="# x\n")
        self.assertEqual((out.decision, out.code), ("failed", "conflict"), out.message)
        self.assertIn("someone else", (self.ws / "calc.py").read_text())

    def test_output_is_bounded_and_redacted(self):
        out = self.call("run_command", command="yes line | head -c 500000; echo password=hunter2")
        self.assertLess(len(out.model_text), 12000)
        self.assertIn("500,0", out.model_text)       # the full size is reported
        cmd = self.call("run_command", command="echo api_key=sk-abcdefghijklmnopqrstuvwxyz123456")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps(cmd.receipt))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = make_project(base)
        home = base / "home"
        home.mkdir()
        project_fixture_db(home / "u.db").close()
        self.access = AccessStore(home / "u.db", home)
        self.gate = Gate(self.access, ReceiptStore(home / "u.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def outcomes(self, profile):
        self.access.attach("prj_general", str(self.ws), profile)
        calls = [Call("read_file", {"path": "calc.py"}), Call("write_file", {"path": "x.py", "content": ""}),
                 Call("edit_file", {"path": "calc.py", "old_text": "a", "new_text": "b"}),
                 Call("run_command", {"command": "true"})]
        return [self.gate.evaluate("prj_general", c).outcome for c in calls]

    def test_profiles(self):
        self.assertEqual(self.outcomes("workspace_read"), [ALLOW, BLOCK, BLOCK, BLOCK])
        self.assertEqual(self.outcomes("ask_before_read"), [ASK, BLOCK, BLOCK, BLOCK])
        self.assertEqual(self.outcomes("workspace_code"), [ALLOW, ALLOW, ALLOW, ALLOW])
        self.assertEqual(self.outcomes("code_ask_commands"), [ALLOW, ALLOW, ALLOW, ASK])

    def test_read_profiles_are_offered_only_read_tools(self):
        self.assertEqual(tuple(offered("workspace_read")), ("list_files", "read_file", "search_files", "find_files"))
        self.assertIn("run_command", offered("workspace_code"))


def models():
    return [FakeModel("coder:3b", agent=coder()),
            FakeModel("slow-coder:3b", agent=coder("sleep 30; " + CODER_TEST)),
            FakeModel("quick:3b", agent=scripted(final=lambda m: "ok"))]


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proj = make_project(self.base)
        self.fake = FakeOllama(models=models()).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def send(self, rt, cid, model, message, **extra):
        status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": model, "message": message, **extra})
        self.assertEqual(status, 200)
        return [json.loads(x) for x in body.decode().splitlines() if x.strip()]

    def ready(self, rt, attach=True):
        if attach:
            self.assertEqual(rt.post_json("/api/projects/prj_general/workspace",
                                          {"root": str(self.proj), "profile": "workspace_code"})[0], 200)
        for m in ("coder:3b", "slow-coder:3b"):
            self.assertEqual(rt.post_json("/api/models/probe", {"model": m})[0], 200)
        return rt.new_chat()

    def test_a_coding_task_edits_tests_fixes_and_finishes(self):
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt)
            ev = self.send(rt, cid, "coder:3b", "Add a multiply function to calc.py and make the tests pass.")
            done = ev[-1]
            self.assertEqual(done["type"], "done", ev[-3:])
            calls = [e["tool"] for e in ev if e["type"] == "tool_call"]
            self.assertEqual(calls, ["find_files", "read_file", "edit_file", "run_command", "edit_file",
                                     "run_command"])
            runs = [e for e in ev if e["type"] == "tool_result" and (e["observed"] or {}).get("command")]
            self.assertEqual([r["observed"]["exit"] for r in runs], [1, 0])        # failed, then passed
            self.assertIn("FAIL", runs[0]["detail"])                                # the real output
            self.assertEqual((self.proj / "calc.py").read_text(), FIXED)
            rep = done["report"]
            self.assertEqual(rep["status"], "completed")
            self.assertEqual(rep["workspace"], os.path.realpath(self.proj))
            self.assertEqual([(c["path"], c["op"]) for c in rep["changes"]], [("calc.py", "edited")] * 2)
            self.assertEqual([(c["status"], c["exit"]) for c in rep["commands"]], [("exited", 1), ("exited", 0)])
            self.assertFalse(any("fail" in u.lower() for u in rep["uncertainty"]), rep["uncertainty"])
            start = ev[0]
            self.assertTrue(start["tools"]["coding"])
            self.assertEqual(start["tools"]["root"], os.path.realpath(self.proj))

    def test_the_report_says_when_the_last_check_failed_or_none_ran(self):
        stuck = scripted(("edit_file", {"path": "calc.py", "old_text": "# TODO: multiply\n", "new_text": "x\n"}),
                         final=lambda m: "Done!")
        failing = scripted(("run_command", {"command": "exit 1"}), final=lambda m: "Done!")
        self.fake.models += [FakeModel("edit-only:3b", agent=stuck), FakeModel("fails:3b", agent=failing)]
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt)
            for m in ("edit-only:3b", "fails:3b"):
                rt.post_json("/api/models/probe", {"model": m})
            rep = self.send(rt, cid, "edit-only:3b", "edit it")[-1]["report"]
            self.assertTrue(any("no command was run" in u for u in rep["uncertainty"]), rep["uncertainty"])
            rep = self.send(rt, cid, "fails:3b", "run it")[-1]["report"]
            self.assertTrue(any("exited with status 1" in u for u in rep["uncertainty"]), rep["uncertainty"])

    def test_work_on_a_named_folder_attaches_it_with_the_code_profile(self):
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt, attach=False)
            ev = self.send(rt, cid, "coder:3b", f"work on {self.proj}: add multiply to calc.py and test it")
            self.assertEqual(ev[-1]["type"], "done", ev[-3:])
            sel = ev[0]["tools"]["selected"]
            self.assertEqual((sel["root"], sel["attached"]), (os.path.realpath(self.proj), True))
            status, ws = rt.get_json("/api/projects/prj_general/access")
            self.assertEqual((ws["workspace"]["root"], ws["workspace"]["profile"]),
                             (os.path.realpath(self.proj), "workspace_code"))
            self.assertEqual((self.proj / "calc.py").read_text(), FIXED)
            # a folder already attached keeps the profile chosen for it
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.proj), "profile": "workspace_read"})
            ev = self.send(rt, cid, "coder:3b", f"work on {self.proj} and add multiply")
            self.assertFalse(ev[0]["tools"]["selected"]["attached"])
            self.assertFalse(ev[0]["tools"]["coding"])
            self.assertEqual(rt.get_json("/api/projects/prj_general/access")[1]["workspace"]["profile"],
                             "workspace_read")

    def test_a_named_folder_that_is_not_allowed_is_refused_before_the_model(self):
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt, attach=False)
            before = len(self.fake.requests)
            ev = self.send(rt, cid, "coder:3b", "work on /definitely/not/here please")
            self.assertEqual(ev[-1]["type"], "error")
            self.assertEqual(ev[-1]["code"], "bad_workspace")
            self.assertFalse([r for r in self.fake.requests[before:] if r.get("path") == "/api/chat"])

    def test_stop_during_a_command_ends_it_quickly(self):
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt)
            out = {}

            def go():
                out["ev"] = self.send(rt, cid, "slow-coder:3b", "add multiply")
            th = threading.Thread(target=go)
            th.start()
            deadline = time.monotonic() + 10
            while not rt.runtime.commands.live() and time.monotonic() < deadline:
                time.sleep(0.05)
            pid = rt.runtime.commands.live()[0].pid
            t0 = time.monotonic()
            run_id = next(iter(rt.runtime.runs._runs))       # the page has it from the start event
            self.assertEqual(rt.post_json("/api/chat/cancel", {"run_id": run_id})[0], 200)
            th.join(10)
            self.assertLess(time.monotonic() - t0, 2)
            self.assertFalse(process_alive(pid))
            self.assertEqual(out["ev"][-1]["report"]["status"], "cancelled")

    def test_its_own_source_is_flagged_as_not_the_running_app(self):
        for name, content in (("umbrous/__init__.py", ""), ("bin/umbrous", "")):
            (self.proj / name).parent.mkdir(exist_ok=True)
            (self.proj / name).write_text(content)
        (self.proj / "desktop").mkdir()
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            cid = self.ready(rt)
            ev = self.send(rt, cid, "coder:3b", "add multiply")
            self.assertTrue(ev[0]["tools"]["self_source"])
            system = next(m["content"] for r in self.fake.requests for m in (r.get("body") or {}).get("messages", [])
                          if m["role"] == "system" and "workspace" in m["content"].lower())
            self.assertIn("does not change the running app", system)
            self.assertTrue(any("rebuild" in u for u in ev[-1]["report"]["uncertainty"]))


class NamedFolderTests(unittest.TestCase):
    def test_named_folder(self):
        from umbrous.runloop.chat import named_folder
        cases = {"work on /a/b: add x": "/a/b", "Please work in ~/code/x.": "~/code/x",
                 "work on the repo `/My Projects/app` now": "/My Projects/app",
                 'working on "/tmp/with space"': "/tmp/with space", "work on it later": None,
                 "the file /a/b is wrong": None, "work on 'relative/path'": None}
        for message, want in cases.items():
            self.assertEqual(named_folder(message), want, message)


class RecoveryTests(unittest.TestCase):
    """The record a crash leaves around a change or a command; then a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proj = make_project(self.base)
        self.home = self.base / "home"
        self.fake = FakeOllama(models=models()).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def rt(self):
        return RunningRuntime(home=self.home, ollama_url=self.fake.url)

    def seed(self, tool, data, kind="write.intent", resource="calc.py"):
        with self.rt() as rt:
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.proj), "profile": "workspace_code"})
            rt.post_json("/api/models/probe", {"model": "coder:3b"})
            chat = rt.new_chat()
            r = rt.runtime
            tid, aid = new_task_id(), new_action_id()
            r.tasks.create(tid, "prj_general", chat, "coder:3b", {"max_steps": 6}, workspace=os.path.realpath(self.proj))
            r.tasks.set_status(tid, "started")
            r.history.begin_pending(tid, chat, "add multiply", "coder:3b")
            r.tasks.add_action(tid, aid, 1, tool, resource)
            r.tasks.action_status(aid, "started")
            if data is not None:
                r.tasks.event(tid, kind, aid, **data)
        return chat, tid

    def task(self, rt, tid):
        return rt.get_json(f"/api/tasks/{tid}")[1]

    def intent(self, after_text):
        return {"op": "replace", "path": "calc.py", "before": sha(CALC), "after": sha(after_text),
                "tmp": ".umbrous-tmp-0123abcd"}

    def test_crash_before_the_intent_means_nothing_was_written(self):
        _, tid = self.seed("edit_file", None)
        with self.rt() as rt:
            a = self.task(rt, tid)["actions"][0]
            self.assertEqual((a["status"], a["code"]), ("cancelled", "interrupted"))

    def test_crash_after_the_intent_before_the_write(self):
        (self.proj / ".umbrous-tmp-0123abcd").write_text(FIXED)     # the temp file it left
        _, tid = self.seed("edit_file", self.intent(FIXED))
        with self.rt() as rt:
            t = self.task(rt, tid)
            self.assertEqual(t["actions"][0]["status"], "cancelled")
            self.assertFalse((self.proj / ".umbrous-tmp-0123abcd").exists())
            self.assertTrue(any("shows it was not" in u for u in t["report"]["uncertainty"]))
            self.assertTrue(t["resume"]["possible"])

    def test_crash_after_the_write(self):
        (self.proj / "calc.py").write_text(FIXED)
        cid, tid = self.seed("edit_file", self.intent(FIXED))
        with self.rt() as rt:
            t = self.task(rt, tid)
            self.assertEqual((t["actions"][0]["status"], t["actions"][0]["code"]), ("completed", "ok"))
            rec = next(e for e in t["events"] if e["kind"] == "action.reconciled")["data"]
            self.assertEqual((rec["reconciled_from"], rec["outcome"]), ("disk", "done"))
            self.assertEqual(t["report"]["changes"][0]["path"], "calc.py")
            self.assertEqual(t["resume"]["done"], [{"tool": "edit_file", "resource": "calc.py"}])

    def test_crash_and_then_someone_else_changed_the_file(self):
        (self.proj / "calc.py").write_text("# Freddy's own version\n")
        _, tid = self.seed("edit_file", self.intent(FIXED))
        with self.rt() as rt:
            t = self.task(rt, tid)
            self.assertEqual((t["actions"][0]["status"], t["actions"][0]["code"]), ("unknown", "conflict"))
            self.assertTrue(any("neither as it was" in u for u in t["report"]["uncertainty"]))
            self.assertEqual(t["resume"]["conflicts"], ["calc.py"])
            self.assertEqual((self.proj / "calc.py").read_text(), "# Freddy's own version\n")   # left alone

    def test_an_interrupted_command_is_unknown_and_its_process_is_stopped(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            cid, tid = self.seed("run_command", {"pid": proc.pid, "identity": identity(proc.pid),
                                                 "command": "sleep 60"}, kind="command.started", resource=".")
            with self.rt() as rt:
                t = self.task(rt, tid)
                self.assertEqual((t["actions"][0]["status"], t["actions"][0]["retry"] if "retry" in t["actions"][0]
                                  else "command"), ("unknown", "command"))
                self.assertIn("command.stopped_after_restart", [e["kind"] for e in t["events"]])
                self.assertTrue(any("not run again by itself" in u for u in t["report"]["uncertainty"]))
                self.assertTrue(t["resume"]["possible"], t["resume"])
                self.assertIn("Commands are not re-run", t["resume"]["message"])
            proc.wait(5)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_a_reused_process_id_is_not_signalled(self):
        # the record names a process id that is now an unrelated process (same id, other start)
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            real = identity(proc.pid)
            self.assertIsNotNone(real)
            for ident in ({"boot": real["boot"], "start": "lstart:Thu Jan  1 00:00:00 1970"},
                          {"boot": "another-boot", "start": real["start"]}, None):
                _, tid = self.seed("run_command", {"pid": proc.pid, "identity": ident, "command": "sleep 60"},
                                   kind="command.started", resource=".")
                with self.rt() as rt:
                    t = self.task(rt, tid)
                    kinds = [e["kind"] for e in t["events"]]
                    self.assertNotIn("command.stopped_after_restart", kinds, ident)
                    self.assertIn("command.not_signalled", kinds)
                    self.assertTrue(any("left alone" in u for u in t["report"]["uncertainty"]))
                self.assertIsNone(proc.poll(), f"an unrelated process was signalled ({ident})")
        finally:
            proc.kill()
            proc.wait(5)

    def test_resume_tells_the_model_what_was_done_and_does_not_redo_it(self):
        (self.proj / "calc.py").write_text(CALC.replace("# TODO: multiply\n", "def multiply(a, b):\n    return a + b\n"))
        cid, tid = self.seed("edit_file", self.intent(CALC.replace("# TODO: multiply\n",
                                                                    "def multiply(a, b):\n    return a + b\n")))
        with self.rt() as rt:
            status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": "coder:3b", "resume_task": tid})
            ev = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
            self.assertEqual(ev[-1]["type"], "done", ev[-3:])
            brief = next(m["content"] for r in self.fake.requests for m in (r.get("body") or {}).get("messages", [])
                         if m["role"] == "system" and "is now resumed" in m["content"])
            self.assertIn("edit_file calc.py (replaced; still as written)", brief)
            calls = [(e["tool"], e["args"].get("old_text")) for e in ev if e["type"] == "tool_call"]
            self.assertNotIn(("edit_file", "# TODO: multiply\n"), calls)            # not made again
            self.assertEqual((self.proj / "calc.py").read_text(), FIXED)
            self.assertEqual((self.proj / "calc.py").read_text().count("def multiply"), 1)

    def test_resume_rechecks_the_current_permissions(self):
        (self.proj / "calc.py").write_text(FIXED)
        cid, tid = self.seed("edit_file", self.intent(FIXED))
        with self.rt() as rt:
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.proj), "profile": "workspace_read"})
            status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": "coder:3b", "resume_task": tid})
            ev = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "resume_scope_changed"), ev[-1])
            self.assertFalse([e for e in ev if e["type"] == "tool_call"])

    def test_resume_rechecks_command_permission(self):
        # a task that only ran a command, resumed after commands were turned off
        cid, tid = self.seed("run_command", {"pid": 999999, "command": "make"}, kind="command.started", resource=".")
        with self.rt() as rt:
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.proj), "profile": "workspace_read"})
            status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": "coder:3b", "resume_task": tid})
            ev = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "resume_scope_changed"), ev[-1])

    def test_resume_is_refused_when_the_workspace_changed(self):
        cid, tid = self.seed("edit_file", None)
        other = self.base / "other"
        other.mkdir()
        with self.rt() as rt:
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(other), "profile": "workspace_code"})
            status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": "coder:3b", "resume_task": tid})
            ev = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "resume_scope_changed"), ev[-1])


class CrashTests(unittest.TestCase):
    """Kill the real runtime while the tests run, start it again, resume."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = make_project(base)
        self.home = base / "home"
        self.home.mkdir(mode=0o700)
        self.fake = FakeOllama(models=models()).start()
        (self.home / "config.json").write_text(json.dumps({"ollama_url": self.fake.url}))

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def test_kill_during_the_tests_then_resume_without_duplicating_the_edit(self):
        c = Child(self.home)
        pid_seen = {}
        try:
            self.assertEqual(c.call("POST", "/api/projects/prj_general/workspace",
                                    {"root": str(self.proj), "profile": "workspace_code"})[0], 200)
            for m in ("slow-coder:3b", "coder:3b"):
                self.assertEqual(c.call("POST", "/api/models/probe", {"model": m})[0], 200)
            cid = c.call("POST", "/api/chats", {"project_id": "prj_general"})[1]["chat"]["id"]

            def when_testing(e):
                if e["type"] == "tool_call" and e["tool"] == "run_command":
                    time.sleep(0.7)     # the command (sleep 30; tests) is running now
                    return True
                return False
            events = c.stream({"chat_id": cid, "model": "slow-coder:3b", "message": "add multiply"}, when_testing)
            task_id = next(e["task_id"] for e in events if e["type"] == "start")
        finally:
            c.stop()
        after_kill = (self.proj / "calc.py").read_text()
        self.assertEqual(after_kill.count("def multiply"), 1)            # the edit had been made
        c = Child(self.home)
        try:
            t = c.call("GET", f"/api/tasks/{task_id}")[1]
            st = {a["tool"]: a["status"] for a in t["actions"]}
            self.assertEqual((st["edit_file"], st["run_command"]), ("completed", "unknown"))
            # gone with the runtime (its supervisor stopped it) or stopped by recovery
            kinds = {e["kind"] for e in t["events"]}
            self.assertTrue(kinds & {"command.gone_after_restart", "command.stopped_after_restart"}, kinds)
            self.assertTrue(t["resume"]["possible"], t["resume"])
            ev = c.stream({"chat_id": cid, "model": "coder:3b", "resume_task": task_id})
            self.assertEqual(ev[-1]["type"], "done", ev[-3:])
            self.assertEqual(ev[-1]["report"]["status"], "completed")
            calls = [(e["tool"], e["args"].get("old_text")) for e in ev if e["type"] == "tool_call"]
            self.assertNotIn(("edit_file", "# TODO: multiply\n"), calls)
            self.assertEqual((self.proj / "calc.py").read_text(), FIXED)
            self.assertEqual([n for n in os.listdir(self.proj) if n.startswith(".umbrous-tmp")], [])
        finally:
            c.stop()


if __name__ == "__main__":
    unittest.main()
