"""M5: recover and resume.

Two kinds of test:
  CrashTests     the real runtime in a child process, killed with SIGKILL at
                 four points of a task, then started again on the same data.
  RecoveryTests  the record a crash can leave, written directly, then the
                 runtime started on it (each reconcile rule, Stop, a saved
                 reply), and resume under changed scope, approval, archive,
                 retention and cancellation.
"""

import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.fake_ollama import FakeModel, FakeOllama, scripted
from tests.helpers import RunningRuntime
from tests.test_m3_fs import make_fixture
from umbrous.gate import gate as gate_mod
from umbrous.persistence import db
from umbrous.receipts.model import EvidenceRef, Receipt, new_action_id, new_task_id
from umbrous.tasks.store import TaskStore

REPO = Path(__file__).resolve().parents[1]
LONG = "The owner is Freddy (notes/plan.md:3). " + "More detail follows here. " * 60

# The child process. A test-only patch makes read_file hang, so a kill can land
# while a tool is running (started, no receipt). Nothing in umbrous changes.
CHILD = """
import os, sys, time
if os.environ.get("HANG_READS"):
    from umbrous.tools import fs
    real = fs.read_file
    def hang(*a, **k):
        time.sleep(60)
        return real(*a, **k)
    fs.read_file = hang
from umbrous.cli import main
sys.exit(main())
"""


def models():
    return [
        # streams for seconds before any tool call: a kill lands mid-stream, no action yet
        FakeModel("talker:3b", chunk_delay=0.05, agent=scripted(final=lambda m: LONG)),
        # reads, then streams for seconds: a kill lands after the read's receipt was saved
        FakeModel("reader:3b", chunk_delay=0.05, agent=scripted(("read_file", {"path": "notes/plan.md"}),
                                                                 final=lambda m: LONG)),
        FakeModel("quick:3b", agent=scripted(("read_file", {"path": "notes/plan.md"}),
                                             final=lambda m: "Owner: Freddy (notes/plan.md:3).")),
    ]


class Child:
    """`serve --desktop` in a child process, with the protocol helpers the tests need."""

    def __init__(self, home: Path, hang_reads: bool = False) -> None:
        env = dict(os.environ, PYTHONPATH=str(REPO))
        env.pop("UMBROUS_PORT", None)
        if hang_reads:
            env["HANG_READS"] = "1"
        self.proc = subprocess.Popen([sys.executable, "-c", CHILD, "--home", str(home), "serve", "--desktop",
                                      "--port", "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(REPO))
        ready = json.loads(self.proc.stdout.readline())
        assert ready["event"] == "ready", ready
        self.origin = ready["origin"]
        self.port = int(self.origin.rsplit(":", 1)[1])
        status, body = self.call("POST", "/api/session/claim", headers={"X-Umbrous-Launch": ready["launch_code"]},
                                 auth=False)
        assert status == 200, body
        self.token = body["token"]

    def call(self, method, path, obj=None, headers=None, auth=True, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": f"127.0.0.1:{self.port}", "Origin": self.origin, **(headers or {})}
        if auth:
            h["X-Umbrous-Token"] = self.token
        body = json.dumps(obj).encode() if obj is not None else (b"" if method == "POST" else None)
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if data else None)

    def stream(self, obj, kill_when=None):
        """POST /api/chat and read events; SIGKILL the child when kill_when(event) is true."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", "/api/chat", body=json.dumps(obj).encode(), headers={
            "Host": f"127.0.0.1:{self.port}", "Origin": self.origin, "Content-Type": "application/json",
            "X-Umbrous-Token": self.token})
        resp, events = conn.getresponse(), []
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                if line.strip():
                    events.append(json.loads(line))
                    if kill_when and kill_when(events[-1]):
                        self.kill()
                        break
        except (OSError, http.client.HTTPException):
            pass
        conn.close()
        return events

    def kill(self):
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(10)

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.wait(10)
        self.proc.stdout.close()


class CrashTests(unittest.TestCase):
    """Kill the runtime at several points of a task, start it again, and look."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = make_fixture(base)
        self.home = base / "home"
        self.home.mkdir(mode=0o700)
        self.fake = FakeOllama(models=models()).start()
        (self.home / "config.json").write_text(json.dumps({"ollama_url": self.fake.url}))

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def first_run(self, model, kill_when, profile="workspace_read", approvals=False, hang_reads=False):
        c = Child(self.home, hang_reads=hang_reads)
        try:
            self.assertEqual(c.call("POST", "/api/projects/prj_general/workspace",
                                    {"root": str(self.ws), "profile": profile})[0], 200)
            for m in (model, "quick:3b"):
                self.assertEqual(c.call("POST", "/api/models/probe", {"model": m})[0], 200)
            cid = c.call("POST", "/api/chats", {"project_id": "prj_general"})[1]["chat"]["id"]
            events = c.stream({"chat_id": cid, "model": model, "message": "who owns the plan?",
                               "approvals": approvals}, kill_when)
            self.assertIsNotNone(c.proc.returncode, "the child was killed")
            task_id = next(e["task_id"] for e in events if e["type"] == "start")
        finally:
            c.stop()
        return cid, task_id, events

    def restarted(self, cid, task_id):
        c = Child(self.home)
        status, task = c.call("GET", f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        listed = c.call("GET", f"/api/tasks?chat_id={cid}")[1]["tasks"]
        return c, task, listed

    def resume(self, c, cid, task_id, model):
        return c.stream({"chat_id": cid, "model": model, "resume_task": task_id})

    def check_interrupted(self, task, listed):
        t = task["task"]
        self.assertEqual((t["status"], t["code"]), ("unknown", "interrupted"))
        self.assertTrue(task["report"]["recovered"])
        self.assertIn("Umbrous stopped while this task was running", task["report"]["uncertainty"][0])
        self.assertIn("task.interrupted", [e["kind"] for e in task["events"]])
        self.assertTrue(task["resume"]["possible"], task["resume"])
        self.assertEqual(listed[-1]["resume"]["possible"], True)

    def check_resumed(self, c, cid, old_id, events):
        done = events[-1]
        self.assertEqual(done["type"], "done", events[-3:])
        self.assertEqual(events[0]["resume_of"], old_id)
        rep = done["report"]
        self.assertEqual((rep["status"], rep["resume_of"]), ("completed", old_id))
        self.assertTrue(any("resumed an interrupted one" in u for u in rep["uncertainty"]))
        msgs = c.call("GET", f"/api/chats/{cid}")[1]["messages"]
        self.assertEqual([(m["role"], m["task_id"]) for m in msgs],
                         [("user", done["task_id"]), ("assistant", done["task_id"])])
        self.assertEqual(msgs[0]["content"], "who owns the plan?")      # the waiting message, not retyped
        old = c.call("GET", f"/api/tasks/{old_id}")[1]
        self.assertEqual((old["task"]["status"], old["resume"]["reason"]), ("unknown", "already_resumed"))
        again = self.resume(c, cid, old_id, "quick:3b")
        self.assertEqual((again[-1]["type"], again[-1]["code"]), ("error", "not_resumable"))   # not twice
        return rep

    def test_kill_mid_stream_before_any_action(self):
        cid, tid, _ = self.first_run("talker:3b", lambda e: e["type"] == "delta")
        c, task, listed = self.restarted(cid, tid)
        try:
            self.check_interrupted(task, listed)
            self.assertEqual(task["actions"], [])
            self.assertIn("Reply text that had arrived before the stop was not saved.", task["report"]["uncertainty"])
            self.assertEqual(c.call("GET", f"/api/chats/{cid}")[1]["messages"], [])   # nothing half-saved
            self.check_resumed(c, cid, tid, self.resume(c, cid, tid, "quick:3b"))
        finally:
            c.stop()

    def test_kill_after_a_result_was_saved(self):
        cid, tid, ev = self.first_run("reader:3b", lambda e: e["type"] == "delta")
        self.assertTrue(any(e["type"] == "tool_result" and e["decision"] == "allowed" for e in ev))
        (self.ws / "notes" / "plan.md").write_text("# Plan\nShip M5 on Monday.\nOwner: Freddy\n")  # changed meanwhile
        c, task, listed = self.restarted(cid, tid)
        try:
            self.check_interrupted(task, listed)
            self.assertEqual([(a["tool"], a["status"]) for a in task["actions"]], [("read_file", "completed")])
            self.assertEqual(task["report"]["evidence"][0]["ref"], "notes/plan.md")     # completed results kept
            self.assertEqual(task["resume"]["repeats"], [{"tool": "read_file", "resource": "notes/plan.md",
                                                          "status": "completed"}])
            rep = self.check_resumed(c, cid, tid, self.resume(c, cid, tid, "quick:3b"))
            self.assertIn("notes/plan.md changed since the interrupted attempt read it.", rep["uncertainty"])
            receipts = c.call("GET", f"/api/receipts?task_id={rep['task_id']}")[1]["receipts"]
            self.assertEqual([r["decision"] for r in receipts], ["allowed"])           # a new receipt for the re-read
        finally:
            c.stop()

    def test_kill_while_a_read_is_running(self):
        def after_it_started(e):
            if e["type"] == "tool_call":
                time.sleep(0.5)       # the read has started and hangs (test patch): no receipt yet
                return True
            return False
        cid, tid, ev = self.first_run("reader:3b", after_it_started, hang_reads=True)
        c, task, listed = self.restarted(cid, tid)
        try:
            self.check_interrupted(task, listed)
            a = task["actions"][0]
            self.assertEqual((a["status"], a["code"]), ("unknown", "interrupted"))     # not failed
            self.assertEqual(c.call("GET", f"/api/receipts?task_id={tid}")[1]["receipts"], [])
            rec = [e for e in task["events"] if e["kind"] == "action.reconciled"]
            self.assertEqual((len(rec), rec[0]["data"]["was"], rec[0]["data"]["retry"]), (1, "started", "repeat"))
            self.assertTrue(any("its outcome is unknown" in u for u in task["report"]["uncertainty"]))
            self.check_resumed(c, cid, tid, self.resume(c, cid, tid, "quick:3b"))
        finally:
            c.stop()

    def test_kill_while_waiting_for_approval(self):
        cid, tid, ev = self.first_run("reader:3b", lambda e: e["type"] == "approval_required",
                                      profile="ask_before_read", approvals=True)
        c, task, listed = self.restarted(cid, tid)
        try:
            self.check_interrupted(task, listed)
            a = task["actions"][0]
            self.assertEqual((a["status"], a["code"]), ("cancelled", "interrupted"))   # it never ran
            self.assertEqual(c.call("GET", f"/api/receipts?task_id={tid}")[1]["receipts"], [])
            # The old approval request is gone with the old process. The native app cannot
            # ask, and nothing that was already read needs re-checking, so the resume runs
            # and its read is refused at once instead of using an answer from before.
            ev2 = self.resume(c, cid, tid, "quick:3b")
            self.assertEqual(ev2[-1]["type"], "done")
            res = [e for e in ev2 if e["type"] == "tool_result"]
            self.assertEqual((res[0]["decision"], res[0]["code"]), ("denied", "approval_unavailable"))
        finally:
            c.stop()


class RecoveryTests(unittest.TestCase):
    """The record a crash can leave, written directly; then a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = make_fixture(base)
        self.home = base / "home"
        self.fake = FakeOllama(models=models()).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def rt(self):
        return RunningRuntime(home=self.home, ollama_url=self.fake.url)

    def seed(self, actions=(), stop=None, reply=False, pending=True, chat=None, profile="workspace_read"):
        """A task as a crash leaves it. actions: (tool, resource, status, receipt decision or None)."""
        with self.rt() as rt:
            if chat is None:
                self.assertEqual(rt.post_json("/api/projects/prj_general/workspace",
                                              {"root": str(self.ws), "profile": profile})[0], 200)
                self.assertEqual(rt.post_json("/api/models/probe", {"model": "quick:3b"})[0], 200)
                chat = rt.new_chat()
            r = rt.runtime
            tid = new_task_id()
            r.tasks.create(tid, "prj_general", chat, "quick:3b", {"max_steps": 6})
            r.tasks.set_status(tid, "started")
            r.tasks.event(tid, "step.started", step=1, tools_offered=True)
            if pending:
                r.history.begin_pending(tid, chat, "who owns the plan?", "quick:3b")
            ids = []
            for tool, resource, status, decision in actions:
                aid = new_action_id()
                ids.append(aid)
                r.tasks.add_action(tid, aid, 1, tool, resource)
                if status == "started":
                    r.tasks.action_status(aid, "started")
                if decision:
                    now = datetime.now(timezone.utc)
                    ev = (EvidenceRef("file", resource, "a" * 64, 10, (1, 3)),) if decision == "allowed" else ()
                    r.receipts.add(Receipt(tid, aid, gate_mod.TOOLS[tool]["action"], f"project:prj_general "
                                           f"resource:{resource}", decision, now, now + timedelta(milliseconds=5),
                                           reason="ok: done" if decision == "allowed" else f"x_{decision}: no",
                                           evidence=ev), "prj_general", chat)
            if stop:
                r.tasks.event(tid, "stop.requested", by=stop)
            if reply:
                r.history.append_exchange(chat, "who owns the plan?", "Freddy.", "quick:3b", "complete", task_id=tid)
        return chat, tid, ids

    def get(self, rt, tid):
        status, t = rt.get_json(f"/api/tasks/{tid}")
        self.assertEqual(status, 200)
        return t

    def resume(self, rt, cid, tid, approvals=False):
        status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": "quick:3b", "resume_task": tid,
                                                     "approvals": approvals})
        self.assertEqual(status, 200)
        return [json.loads(x) for x in body.decode().splitlines() if x.strip()]

    # ---------------------------------------------------------------- reconcile
    def test_each_open_action_is_reconciled_by_its_receipt_or_marked(self):
        cid, tid, ids = self.seed([
            ("read_file", "notes/plan.md", "started", "allowed"),   # result saved, task did not record it
            ("read_file", "readme.txt", "started", None),           # running, no result: unknown
            ("list_files", ".", "requested", None),                 # never ran: cancelled
            ("read_file", ".env", "requested", "denied"),           # refused before it ran
            ("search_files", ".", "started", "uncertain"),          # the tool crashed: unknown
        ])
        with self.rt() as rt:
            self.assertEqual([x["task_id"] for x in rt.runtime.recovered], [tid])
            t = self.get(rt, tid)
            got = {a["action_id"]: (a["status"], a["code"]) for a in t["actions"]}
            self.assertEqual([got[i] for i in ids], [("completed", "ok"), ("unknown", "interrupted"),
                                                    ("cancelled", "interrupted"), ("denied", "x_denied"),
                                                    ("unknown", "x_uncertain")])
            rec = [e for e in t["events"] if e["kind"] == "action.reconciled"]
            self.assertEqual(len(rec), 5)
            self.assertEqual(rec[0]["data"]["reconciled_from"], "receipt")
            self.assertEqual(rec[1]["data"]["retry"], "repeat")
            rep = t["report"]
            self.assertEqual(rep["actions"], {"completed": 1, "unknown": 2, "cancelled": 1, "denied": 1})
            self.assertEqual([e["ref"] for e in rep["evidence"]], ["notes/plan.md"])   # only the completed read
            self.assertTrue(any("readme.txt was running when Umbrous stopped" in u for u in rep["uncertainty"]))
            self.assertEqual({f["status"] for f in rep["failures"]}, {"unknown", "cancelled", "denied"})
            self.assertTrue(t["resume"]["possible"])
            self.assertEqual(t["resume"]["message"].split(".")[0], "Resuming runs the task again under the current "
                                                                   "settings")
        with self.rt() as rt:          # a second restart changes nothing
            self.assertEqual(rt.runtime.recovered, [])

    def test_unknown_is_not_failed(self):
        _, tid, ids = self.seed([("read_file", "readme.txt", "started", None)])
        with self.rt() as rt:
            a = self.get(rt, tid)["actions"][0]
            self.assertEqual(a["status"], "unknown")
            self.assertNotEqual(a["status"], "failed")

    def test_stop_before_the_crash_is_cancelled_not_interrupted(self):
        cid, tid, _ = self.seed([("read_file", "readme.txt", "started", None)], stop="you")
        with self.rt() as rt:
            t = self.get(rt, tid)
            self.assertEqual((t["task"]["status"], t["task"]["code"]), ("cancelled", "cancelled"))
            self.assertEqual(t["resume"]["reason"], "not_interrupted")
            self.assertIsNone(rt.runtime.history.pending(tid))       # stopped: the message is not kept (M2)
            ev = self.resume(rt, cid, tid)
            self.assertEqual(ev[-1]["code"], "not_resumable")
        cid, tid, _ = self.seed(stop="time_limit")
        with self.rt() as rt:
            self.assertEqual(self.get(rt, tid)["task"]["code"], "task_time_limit")

    def test_a_reply_saved_before_the_crash_is_not_resumed(self):
        cid, tid, _ = self.seed([("read_file", "notes/plan.md", "started", "allowed")], reply=True)
        with self.rt() as rt:
            t = self.get(rt, tid)
            self.assertEqual(t["task"]["status"], "unknown")
            self.assertEqual(t["resume"]["reason"], "reply_saved")
            self.assertTrue(any("The reply was saved before Umbrous stopped" in u for u in t["report"]["uncertainty"]))
            self.assertEqual(len(rt.get_json(f"/api/chats/{cid}")[1]["messages"]), 2)

    def test_an_action_that_cannot_be_repeated_blocks_resume(self):
        _, tid, _ = self.seed([("read_file", "readme.txt", "started", None)])
        patched = dict(gate_mod.TOOLS, read_file=dict(gate_mod.TOOLS["read_file"], retry="verify"))
        with mock.patch.dict(gate_mod.TOOLS, patched), self.rt() as rt:
            t = self.get(rt, tid)
            self.assertEqual(t["resume"]["reason"], "needs_verification")
            self.assertTrue(any("retry class: verify" in u for u in t["report"]["uncertainty"]))

    # ---------------------------------------------------------------- resume respects the current settings
    def test_resume_runs_under_the_current_gate_with_new_receipts(self):
        cid, tid, _ = self.seed([("read_file", "notes/plan.md", "started", "allowed")])
        with self.rt() as rt:
            ev = self.resume(rt, cid, tid)
            done = ev[-1]
            self.assertEqual(done["type"], "done")
            new = done["task_id"]
            self.assertNotEqual(new, tid)
            kinds = [e["kind"] for e in self.get(rt, new)["events"]]
            self.assertIn("task.resume_of", kinds)
            old_receipts = rt.get_json(f"/api/receipts?task_id={tid}")[1]["receipts"]
            new_receipts = rt.get_json(f"/api/receipts?task_id={new}")[1]["receipts"]
            self.assertEqual(len(old_receipts), 1)
            self.assertEqual(len(new_receipts), 1)
            self.assertNotEqual(old_receipts[0]["receipt_id"], new_receipts[0]["receipt_id"])
            self.assertIsNone(rt.runtime.history.pending(new))       # stored with its reply now
            # the hash in the seeded receipt is not the file's: it counts as changed
            self.assertIn("notes/plan.md changed since the interrupted attempt read it.", done["report"]["uncertainty"])

    def test_revoked_grant_stops_the_resume(self):
        cid, tid, _ = self.seed([("read_file", "notes/plan.md", "started", "allowed")], profile="ask_before_read")
        with self.rt() as rt:
            # the interrupted read ran under a grant; it was revoked while the task was interrupted
            access = rt.runtime.access
            self.assertEqual(access.workspace("prj_general").profile, "ask_before_read")
            ev = self.resume(rt, cid, tid, approvals=False)
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "resume_needs_approval"))
            t = self.get(rt, tid)
            self.assertEqual(t["task"]["status"], "unknown")             # still interrupted, nothing replayed
            self.assertIn("resume.refused", [e["kind"] for e in t["events"]])
            self.assertEqual(len(rt.get_json(f"/api/receipts?task_id={tid}")[1]["receipts"]), 1)
            self.assertTrue(t["resume"]["possible"])

            # with a grant again, it runs
            access.grant("prj_general", "fs.read")
            self.assertEqual(self.resume(rt, cid, tid)[-1]["type"], "done")

    def test_resume_where_approval_can_be_asked_asks_again(self):
        cid, tid, _ = self.seed([("read_file", "notes/plan.md", "started", "allowed")], profile="ask_before_read")
        with self.rt() as rt:
            seen = {}

            def deny_when_asked():
                for _ in range(100):
                    time.sleep(0.05)
                    pend = rt.runtime.gate.approvals._pending
                    if pend:
                        seen["asked"] = True
                        rt.runtime.gate.approvals.answer(next(iter(pend)), "deny")
                        return
            threading.Thread(target=deny_when_asked, daemon=True).start()
            ev = self.resume(rt, cid, tid, approvals=True)
            self.assertTrue(seen.get("asked"))                           # the old answer was not reused
            self.assertTrue(any(e["type"] == "approval_required" for e in ev))
            res = [e for e in ev if e["type"] == "tool_result"][0]
            self.assertEqual((res["decision"], res["code"]), ("denied", "denied_by_you"))

    def test_detached_workspace_stops_the_resume(self):
        cid, tid, _ = self.seed([("read_file", "notes/plan.md", "started", "allowed")])
        with self.rt() as rt:
            self.assertEqual(rt.post_json("/api/projects/prj_general/workspace/detach", {})[0], 200)
            ev = self.resume(rt, cid, tid)
            self.assertEqual(ev[-1]["code"], "resume_scope_changed")
            self.assertEqual(self.get(rt, tid)["task"]["status"], "unknown")

    def test_archived_chat_is_not_resumed(self):
        cid, tid, _ = self.seed()
        with self.rt() as rt:
            rt.post_json(f"/api/chats/{cid}/archive", {})
            self.assertEqual(self.resume(rt, cid, tid)[-1]["code"], "chat_archived")

    def test_purged_task_is_gone_with_its_message(self):
        cid, tid, _ = self.seed()
        with self.rt() as rt:
            self.assertEqual(rt.runtime.tasks.purge(), 1)
            self.assertIsNone(rt.runtime.history.pending(tid))       # retention: purge takes the message too
            self.assertEqual(rt.get_json(f"/api/tasks/{tid}")[0], 404)
            self.assertEqual(self.resume(rt, cid, tid)[-1]["code"], "no_such_task")

    def test_a_task_from_another_chat_is_refused(self):
        cid, tid, _ = self.seed()
        with self.rt() as rt:
            other = rt.new_chat()
            self.assertEqual(self.resume(rt, other, tid)[-1]["code"], "no_such_task")

    def test_a_new_message_replaces_the_interrupted_one(self):
        cid, tid, _ = self.seed()
        with self.rt() as rt:
            ev = rt.chat(cid, "quick:3b", "something else")
            self.assertEqual(ev[-1]["type"], "done")
            t = self.get(rt, tid)
            self.assertIn("task.superseded", [e["kind"] for e in t["events"]])
            self.assertEqual(t["resume"]["reason"], "message_gone")
            msgs = rt.get_json(f"/api/chats/{cid}")[1]["messages"]
            self.assertEqual([m["content"] for m in msgs if m["role"] == "user"], ["something else"])

    def test_stop_during_a_resume_cancels_it(self):
        cid, tid, _ = self.seed()
        self.fake.models.append(FakeModel("slowq:3b", chunk_delay=0.1, agent=scripted(final=lambda m: LONG)))
        with self.rt() as rt:
            self.assertEqual(rt.post_json("/api/models/probe", {"model": "slowq:3b"})[0], 200)
            box = {}
            conn = http.client.HTTPConnection("127.0.0.1", rt.port, timeout=30)
            conn.request("POST", "/api/chat", body=json.dumps({"chat_id": cid, "model": "slowq:3b",
                                                               "resume_task": tid}).encode(),
                         headers={"Host": rt.host, "Origin": rt.origin, "Content-Type": "application/json",
                                  "X-Umbrous-Token": rt.policy.session_token})
            resp, events = conn.getresponse(), []
            while True:
                line = resp.readline()
                if not line:
                    break
                e = json.loads(line)
                events.append(e)
                if e["type"] == "start":
                    box["run"] = e["run_id"]
                if e["type"] == "delta" and "stopped" not in box:
                    box["stopped"] = True
                    rt.post_json("/api/chat/cancel", {"run_id": box["run"]})
            conn.close()
            self.assertEqual(events[-1]["code"], "cancelled")
            new = events[-1]["report"]["task_id"]
            self.assertEqual(self.get(rt, new)["task"]["status"], "cancelled")
            self.assertEqual(self.get(rt, tid)["resume"]["reason"], "already_resumed")   # not offered again
            # the partial reply was kept as stopped (M2), with the waiting message
            msgs = rt.get_json(f"/api/chats/{cid}")[1]["messages"]
            self.assertEqual([(m["role"], m["status"]) for m in msgs], [("user", "complete"), ("assistant", "stopped")])


class PendingTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.fake = FakeOllama(models=models()).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def test_a_finished_task_leaves_no_pending_message_and_none_reaches_the_model(self):
        ws = make_fixture(Path(self.tmp.name))
        with RunningRuntime(home=self.home, ollama_url=self.fake.url) as rt:
            rt.post_json("/api/projects/prj_general/workspace", {"root": str(ws)})
            rt.post_json("/api/models/probe", {"model": "quick:3b"})
            cid = rt.new_chat()
            rt.chat(cid, "quick:3b", "first question")
            self.assertEqual(rt.runtime.history.pending_for_chat(cid), [])
            # a waiting message of an interrupted task is not part of the next prompt
            tid = new_task_id()
            rt.runtime.tasks.create(tid, "prj_general", cid, "quick:3b", {})
            rt.runtime.history.begin_pending(tid, cid, "WAITING-ONLY", "quick:3b")
            self.assertNotIn("WAITING-ONLY", [t.content for t in rt.runtime.history.turns(cid)])

    def test_drop_pending_never_touches_the_transcript(self):
        with RunningRuntime(home=self.home, ollama_url=self.fake.url) as rt:
            h, cid = rt.runtime.history, rt.new_chat()
            h.append_exchange(cid, "kept", "kept too", "m:1b", "complete")
            tid = new_task_id()
            rt.runtime.tasks.create(tid, "prj_general", cid, "m", {})
            h.begin_pending(tid, cid, "waiting", "m:1b")
            self.assertTrue(h.drop_pending(tid))
            self.assertEqual([m.content for m in h.messages(cid)], ["kept", "kept too"])

    def test_schema_6_upgrade_keeps_version_5_data(self):
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path, isolation_level=None)
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:5]), mock.patch.object(db, "SCHEMA_VERSION", 5):
            db.migrate(conn)
        conn.execute("INSERT INTO tasks (task_id, status, created_at, limits) VALUES ('tsk_x', 'started', 'now', '{}')")
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "started")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_turns").fetchone()[0], 0)
        conn.close()
        self.assertEqual(TaskStore(path).open_tasks(), ["tsk_x"])


if __name__ == "__main__":
    unittest.main()
