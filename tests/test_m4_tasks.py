"""M4: durable task state, limits, Stop, Observe + Verify, and log privacy."""

import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_ollama import FakeModel, FakeOllama, scripted, workspace_agent
from tests.helpers import RunningRuntime
from tests.test_m3_fs import make_fixture
from umbrous.persistence import db
from tests.helpers import project_fixture_db
from umbrous.tasks.store import TaskError, TaskStore
from umbrous.tools import fs

# A fixture string that must never be stored by default: it is in a file the
# task reads, in the message, and in the answer.
CANARY = "PELICAN-7741-canary"
TOKEN_NAME = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"   # a GitHub-token-shaped file name
STOP_BOUND_S = 1.0     # documented: Stop ends the reply within 1 s (docs/m4-tasks.md)


def answer_citing(*cites):
    return lambda msgs: "Answer. Sources: " + ", ".join(cites)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "u.db"
        project_fixture_db(self.path).close()
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_has_task_tables(self):
        conn = project_fixture_db(self.path)
        self.assertGreaterEqual(db.schema_version(conn), 5)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue({"tasks", "task_actions", "task_events", "task_checkpoints", "task_artifacts"} <= names)

    def test_state_transitions_are_enforced(self):
        s = self.store
        s.create("tsk_" + "0" * 28, "prj_general", None, "m", {"max_steps": 1})
        tid = "tsk_" + "0" * 28
        with self.assertRaises(TaskError):
            s.set_status(tid, "completed")             # requested -> completed skips started
        s.set_status(tid, "started")
        s.add_action(tid, "act_" + "1" * 28, 1, "read_file", "a.txt")
        with self.assertRaises(TaskError):
            s.action_status("act_" + "1" * 28, "unknown")   # unknown only after it started
        s.action_status("act_" + "1" * 28, "started")
        s.action_status("act_" + "1" * 28, "completed", "ok", "rcp_x")
        with self.assertRaises(TaskError):
            s.action_status("act_" + "1" * 28, "failed")    # ended stays ended
        s.set_status(tid, "completed")
        with self.assertRaises(TaskError):
            s.set_status(tid, "cancelled")
        kinds = [e["kind"] for e in s.get(tid)["events"]]
        self.assertEqual(kinds, ["task.requested", "task.started", "action.requested", "action.started",
                                 "action.completed", "task.completed"])
        self.assertEqual([e["seq"] for e in s.get(tid)["events"]], list(range(1, 7)))

    def test_purge_removes_everything_of_a_task(self):
        s = self.store
        tid = "tsk_" + "2" * 28
        s.create(tid, None, None, "m", {})
        s.set_status(tid, "started")
        s.add_action(tid, "act_" + "3" * 28, 1, "list_files", ".")
        s.checkpoint(tid, 1, {"step": 1})
        s.artifact(tid, "report", {"status": "completed"})
        self.assertEqual(s.purge(), 1)
        conn = sqlite3.connect(self.path)
        for table in ("tasks", "task_actions", "task_events", "task_checkpoints", "task_artifacts"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        conn.close()


class TaskRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = make_fixture(base)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("def main():\n    return 'hello'\n")
        (self.ws / "notes" / "private.md").write_text(f"The code word is {CANARY}.\n")
        (self.ws / "notes" / f"{TOKEN_NAME}.txt").write_text("nothing here\n")
        many = [("list_files", {})]
        self.fake = FakeOllama(models=[
            FakeModel("multi:3b", agent=scripted(
                ("list_files", {}), ("search_files", {"query": "Ship"}), ("read_file", {"path": "notes/plan.md"}),
                final=answer_citing("notes/plan.md:2", "src/app.py:1", "notes/plan.md:40"))),
            FakeModel("secret:3b", agent=scripted(
                ("read_file", {"path": "notes/private.md"}), ("search_files", {"query": CANARY}),
                ("read_file", {"path": f"notes/{TOKEN_NAME}.txt"}),
                final=lambda m: f"The code word is {CANARY}.")),
            FakeModel("greedy:3b", agent=lambda msgs: {"tool_calls": many}
                      if msgs[-1].get("content") != "__no_tools__" else {"content": "Done looking."}),
            FakeModel("burst:3b", agent=scripted([("list_files", {}) for _ in range(5)])),
            FakeModel("slowtail:3b", agent=scripted(("list_files", {}), final=lambda m: "slow answer " * 40),
                      chunk_delay=0.2),
            FakeModel("reader:3b", agent=scripted(("read_file", {"path": "notes/plan.md"}),
                                                  ("read_file", {"path": "readme.txt"}))),
            FakeModel("stalls:3b", agent=scripted(("list_files", {}), final=lambda m: "x" * 200),
                      behavior="stall_mid"),
        ]).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def rt(self, **kw):
        return RunningRuntime(ollama_url=self.fake.url, **kw)

    def setup_rt(self, rt, *models):
        """Attach the workspace and pass the tool check for the given models."""
        status, _, body = rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.ws)})
        self.assertEqual(status, 200, body)
        for m in models:
            self.assertEqual(rt.post_json("/api/models/probe", {"model": m})[0], 200)

    def stream(self, rt, cid, model, message, on_event=lambda e: None):
        conn = http.client.HTTPConnection("127.0.0.1", rt.port, timeout=30)
        conn.request("POST", "/api/chat", body=json.dumps({"chat_id": cid, "model": model, "message": message}).encode(),
                     headers={"Host": rt.host, "Origin": rt.origin, "Content-Type": "application/json",
                              "X-Umbrous-Token": rt.policy.session_token})
        resp, events = conn.getresponse(), []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.strip():
                ev = json.loads(line)
                ev["_t"] = time.monotonic()
                events.append(ev)
                on_event(ev)
        conn.close()
        return events

    def task(self, rt, task_id):
        status, t = rt.get_json(f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        return t

    # ------------------------------------------------------------ the trace and the report
    def test_multi_step_read_task_has_a_trace_and_a_report(self):
        with self.rt() as rt:
            self.setup_rt(rt, "multi:3b")
            cid = rt.new_chat()
            ev = rt.chat(cid, "multi:3b", "where is the plan and who owns it?")
            done = ev[-1]
            self.assertEqual(done["type"], "done")
            rep = done["report"]
            self.assertEqual((rep["status"], rep["steps"], rep["tool_calls"]), ("completed", 4, 3))
            self.assertEqual(rep["actions"], {"completed": 3})
            refs = [(e["kind"], e["ref"]) for e in rep["evidence"]]
            self.assertIn(("directory", "."), refs)
            self.assertIn(("file", "notes/plan.md"), refs)
            self.assertTrue(any(k == "search" and r.startswith(". query#") for k, r in refs))
            # Verify: src/app.py was cited but never read; plan.md:40 is outside the lines read
            self.assertIn("The answer cites src/app.py, which was not read in this task.", rep["uncertainty"])
            self.assertIn("The answer cites notes/plan.md:40, outside the lines read.", rep["uncertainty"])
            self.assertFalse(any("plan.md:2" in u for u in rep["uncertainty"]))

            t = self.task(rt, done["task_id"])
            self.assertEqual(t["task"]["status"], "completed")
            self.assertEqual(t["task"]["chat_id"], cid)
            kinds = [e["kind"] for e in t["events"]]
            self.assertEqual(kinds[:3], ["task.requested", "task.started", "step.started"])
            self.assertEqual(kinds[-2:], ["artifact", "task.completed"])
            for a in t["actions"]:      # each action: requested -> started -> completed, in order
                mine = [e["kind"] for e in t["events"] if e["action_id"] == a["action_id"]]
                self.assertEqual(mine, ["action.requested", "action.started", "action.completed"])
            self.assertEqual([c["step"] for c in t["checkpoints"]], [1, 2, 3, 4])
            self.assertEqual(t["report"], rep)
            # same ids as the receipts, and the receipts are unchanged in format
            receipts = rt.get_json(f"/api/receipts?task_id={done['task_id']}")[1]["receipts"]
            self.assertEqual({r["action_id"] for r in receipts}, {a["action_id"] for a in t["actions"]})
            self.assertEqual({a["receipt_id"] for a in t["actions"]}, {r["receipt_id"] for r in receipts})
            self.assertTrue(all(r["format_version"] == 1 for r in receipts))
            calls = [e for e in ev if e["type"] == "tool_call"]
            self.assertEqual([c["action_id"] for c in calls], [a["action_id"] for a in t["actions"]])
            # a chat's tasks, for redrawing it
            listed = rt.get_json(f"/api/tasks?chat_id={cid}")[1]["tasks"]
            self.assertEqual([x["task_id"] for x in listed], [done["task_id"]])
            self.assertEqual(listed[0]["report"]["status"], "completed")
            # task state is not chat history: the transcript is unchanged in shape
            msgs = rt.get_json(f"/api/chats/{cid}")[1]["messages"]
            self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
            self.assertEqual(msgs[1]["task_id"], done["task_id"])

    # ------------------------------------------------------------ Verify (Codex review, 2026-11-07)
    def verify_report(self, rt, model, steps, answer):
        self.fake.models.append(FakeModel(model, agent=scripted(*steps, final=lambda m: answer)))
        self.assertEqual(rt.post_json("/api/models/probe", {"model": model})[0], 200)
        return rt.chat(rt.new_chat(), model, "go")[-1]["report"]

    def test_a_search_hit_is_not_a_read(self):
        long_line = "Ship it " + "x" * 400            # longer than the 200 characters a search shows
        (self.ws / "notes" / "long.md").write_text("# Long\n" + long_line + "\n")
        with self.rt() as rt:
            self.setup_rt(rt)
            rep = self.verify_report(rt, "searchonly:3b", [("search_files", {"query": "Ship it"})],
                                     "It says so in notes/long.md:2.")
            self.assertEqual(rep["citations_checked"], 1)
            self.assertIn("The answer cites notes/long.md:2, which was only seen in search results "
                          "(lines cut to 200 characters), not read.", rep["uncertainty"])
            rep = self.verify_report(rt, "searchread:3b", [("search_files", {"query": "Ship it"}),
                                                           ("read_file", {"path": "notes/long.md"})],
                                     "It says so in notes/long.md:2.")
            # read after the search: verified (the search itself skipped the fixture's links and binaries)
            self.assertFalse([u for u in rep["uncertainty"] if "long.md" in u], rep["uncertainty"])

    def test_extensionless_citations_are_checked(self):
        (self.ws / "README").write_text("Read me.\n")
        (self.ws / "Makefile").write_text("all:\n\ttrue\n")
        with self.rt() as rt:
            self.setup_rt(rt)
            rep = self.verify_report(rt, "noext:3b", [("list_files", {})],
                                     "See README:1 and Makefile:3, and src/Dockerfile line 2. Served on localhost:8417 at 10:30.")
            self.assertEqual(rep["citations_checked"], 3)
            for name in ("README", "Makefile", "src/Dockerfile"):
                self.assertIn(f"The answer cites {name}, which was not read in this task.", rep["uncertainty"])
            self.assertEqual(len(rep["uncertainty"]), 3)  # host:port and a time are not citations
            rep = self.verify_report(rt, "noextread:3b", [[("read_file", {"path": "README"}),
                                                            ("read_file", {"path": "Makefile"})]],
                                     "See README:1 and Makefile:1-2 (Makefile:2:3 too).")
            self.assertEqual((rep["citations_checked"], rep["uncertainty"]), (3, []))

    def test_digit_leading_names_are_checked_and_times_are_not(self):
        from umbrous.tasks.run import citations
        self.assertEqual(citations("migrations/001_init.sql:3, 2fa.md:1, 001_init.sql line 4, at 10:30, "
                                   "12:00:05, a 3:1 ratio, version 1.2:4, ./8ball.py:2"),
                         [("migrations/001_init.sql", 3, 3), ("2fa.md", 1, 1), ("001_init.sql", 4, 4),
                          ("8ball.py", 2, 2)])
        # Codex's reproduction: a read notes/plan.md:2 next to an unread migrations/001_init.sql:3
        with self.rt() as rt:
            self.setup_rt(rt)
            rep = self.verify_report(rt, "digits:3b", [("read_file", {"path": "notes/plan.md"})],
                                     "Plan: notes/plan.md:2. Schema: migrations/001_init.sql:3.")
            self.assertEqual(rep["citations_checked"], 2)
            self.assertEqual(rep["uncertainty"],
                             ["The answer cites migrations/001_init.sql, which was not read in this task."])

    def test_tasks_api_rejects_bad_ids_and_methods(self):
        with self.rt() as rt:
            self.assertEqual(rt.get_json("/api/tasks/tsk_nope")[0], 404)
            self.assertEqual(rt.get_json("/api/tasks/tsk_" + "0" * 28)[0], 404)
            self.assertEqual(rt.get_json("/api/tasks")[0], 400)
            self.assertEqual(rt.post_json("/api/tasks/tsk_" + "0" * 28, {})[0], 405)
            self.assertEqual(rt.request("GET", "/api/tasks?chat_id=chat_aaaaaaaa")[0], 401)

    def test_plain_chat_is_not_a_task(self):
        with self.rt() as rt:
            ev = rt.chat(rt.new_chat(), "reader:3b", "hello")      # no workspace, no probe
            self.assertIsNone(ev[-1].get("report"))
            self.assertEqual(TaskStore(rt.config.db_path).count(), 0)

    # ------------------------------------------------------------ limits
    def test_step_limit_ends_with_a_clear_report(self):
        with self.rt(task_max_steps=2) as rt:
            self.setup_rt(rt, "greedy:3b")
            ev = rt.chat(rt.new_chat(), "greedy:3b", "look around")
            rep = ev[-1]["report"]
            self.assertEqual(ev[-1]["type"], "done")
            self.assertEqual(sum(e["type"] == "tool_result" for e in ev), 2)
            self.assertEqual(rep["limits_hit"], ["steps"])
            self.assertIn("The step limit (2) was reached", rep["uncertainty"][-1])
            self.assertEqual(rep["status"], "completed")

    def test_total_tool_call_limit(self):
        with self.rt(task_max_tool_calls=3) as rt:
            self.setup_rt(rt, "burst:3b")
            ev = rt.chat(rt.new_chat(), "burst:3b", "list a lot")
            codes = [e["code"] for e in ev if e["type"] == "tool_result"]
            self.assertEqual(codes, ["ok"] * 3 + ["tool_call_limit"] * 2)
            rep = ev[-1]["report"]
            self.assertEqual(rep["actions"], {"completed": 3, "denied": 2})
            self.assertIn("tool_calls", rep["limits_hit"])
            self.assertEqual([f["code"] for f in rep["failures"]], ["tool_call_limit"] * 2)

    def test_hung_tool_is_abandoned_and_reported_unknown(self):
        real = fs.read_file
        release = threading.Event()

        def hang(root, parts, *a, **k):
            if parts == ("notes", "plan.md"):
                release.wait(10)
            return real(root, parts, *a, **k)
        with self.rt(tool_timeout_s=1) as rt, mock.patch.object(fs, "read_file", hang):
            self.setup_rt(rt, "reader:3b")
            t0 = time.monotonic()
            ev = rt.chat(rt.new_chat(), "reader:3b", "read two files")
            self.assertLess(time.monotonic() - t0, 4)
            results = [(e["decision"], e["code"]) for e in ev if e["type"] == "tool_result"]
            self.assertEqual(results, [("uncertain", "tool_timeout"), ("allowed", "ok")])
            rep = ev[-1]["report"]
            self.assertEqual(rep["status"], "completed")        # the task went on without it
            self.assertEqual(rep["actions"], {"unknown": 1, "completed": 1})
            self.assertIn("read_file notes/plan.md did not report back in time; its outcome is unknown.",
                          rep["uncertainty"])
            release.set()                                       # the stuck tool finishes later
            tid = ev[-1]["task_id"]
            for _ in range(50):
                late = [e for e in self.task(rt, tid)["events"] if e["kind"] == "action.late_result"]
                if late:
                    break
                time.sleep(0.05)
            self.assertEqual(late[0]["data"]["decision"], "allowed")      # logged, never given to the model
            actions = self.task(rt, tid)["actions"]
            self.assertEqual(actions[0]["status"], "unknown")

    def test_hung_model_ends_with_a_report(self):
        self.fake.stall_seconds = 4
        with self.rt(idle_timeout_s=1) as rt:
            self.setup_rt(rt, "stalls:3b")
            ev = rt.chat(rt.new_chat(), "stalls:3b", "go")
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "timeout"))
            rep = ev[-1]["report"]
            self.assertEqual((rep["status"], rep["code"]), ("failed", "timeout"))
            self.assertEqual(rep["actions"], {"completed": 1})
            self.assertEqual(self.task(rt, rep["task_id"])["task"]["status"], "failed")

    def test_task_time_limit(self):
        with self.rt(task_timeout_s=1) as rt:
            self.setup_rt(rt, "slowtail:3b")
            t0 = time.monotonic()
            ev = rt.chat(rt.new_chat(), "slowtail:3b", "go")
            self.assertLess(time.monotonic() - t0, 1 + STOP_BOUND_S + 0.5)
            self.assertEqual((ev[-1]["type"], ev[-1]["code"]), ("error", "task_time_limit"))
            rep = ev[-1]["report"]
            self.assertEqual((rep["status"], rep["code"], rep["limits_hit"]), ("failed", "task_time_limit", ["task_time"]))
            self.assertTrue(ev[-1]["partial"])

    # ------------------------------------------------------------ Stop
    def test_stop_during_streaming_is_fast_and_prevents_further_steps(self):
        with self.rt() as rt:
            self.setup_rt(rt, "slowtail:3b")
            cid, box = rt.new_chat(), {}

            def on(ev):
                if ev["type"] == "start":
                    box["run"] = ev["run_id"]
                if ev["type"] == "delta" and "stop_at" not in box:
                    box["stop_at"] = time.monotonic()
                    rt.post_json("/api/chat/cancel", {"run_id": box["run"]})
            ev = self.stream(rt, cid, "slowtail:3b", "go", on)
            self.assertEqual(ev[-1]["code"], "cancelled")
            self.assertLess(ev[-1]["_t"] - box["stop_at"], STOP_BOUND_S)
            rep = ev[-1]["report"]
            self.assertEqual((rep["status"], rep["code"]), ("cancelled", "cancelled"))
            n_requests = len(self.fake.requests)
            time.sleep(0.5)
            self.assertEqual(len(self.fake.requests), n_requests)      # no model call after Stop

    def test_stop_interrupts_a_running_tool_within_the_bound(self):
        real = fs.read_file
        entered = threading.Event()

        def hang(root, parts, *a, **k):
            entered.set()
            time.sleep(5)            # does not look at Stop at all: it must be abandoned
            return real(root, parts, *a, **k)
        with self.rt() as rt, mock.patch.object(fs, "read_file", hang):
            self.setup_rt(rt, "reader:3b")
            cid, box = rt.new_chat(), {}

            def on(ev):
                if ev["type"] == "start":
                    box["run"] = ev["run_id"]

                    def stop():
                        entered.wait(5)
                        box["stop_at"] = time.monotonic()
                        rt.post_json("/api/chat/cancel", {"run_id": box["run"]})
                    threading.Thread(target=stop, daemon=True).start()
            ev = self.stream(rt, cid, "reader:3b", "read two files", on)
            self.assertEqual(ev[-1]["code"], "cancelled")
            self.assertLess(ev[-1]["_t"] - box["stop_at"], STOP_BOUND_S)
            results = [(e["decision"], e["code"]) for e in ev if e["type"] == "tool_result"]
            self.assertEqual(results, [("failed", "cancelled")])      # the second read never ran
            rep = ev[-1]["report"]
            self.assertEqual(rep["actions"], {"cancelled": 1})
            self.assertEqual(rep["status"], "cancelled")

    def test_stop_during_a_search_stops_the_search(self):
        for i in range(400):
            (self.ws / f"f{i:03}.txt").write_text("nothing\n")
        real_open = fs.os.open
        box = {}

        def slow_open(*a, **k):
            if box.get("searching"):
                time.sleep(0.01)
            return real_open(*a, **k)
        self.fake.models.append(FakeModel("searcher:3b", agent=scripted(("search_files", {"query": "zebra"}))))
        with self.rt() as rt, mock.patch.object(fs.os, "open", slow_open):
            self.setup_rt(rt, "searcher:3b")
            cid = rt.new_chat()

            def on(ev):
                if ev["type"] == "start":
                    box["run"] = ev["run_id"]
                if ev["type"] == "tool_call":
                    box["searching"] = True

                    def stop():
                        time.sleep(0.3)
                        box["stop_at"] = time.monotonic()
                        rt.post_json("/api/chat/cancel", {"run_id": box["run"]})
                    threading.Thread(target=stop, daemon=True).start()
            ev = self.stream(rt, cid, "searcher:3b", "find zebra", on)
            self.assertLess(ev[-1]["_t"] - box["stop_at"], STOP_BOUND_S)
            result = next(e for e in ev if e["type"] == "tool_result")
            self.assertEqual((result["decision"], result["code"]), ("failed", "cancelled"))
            self.assertIn("Stopped during the search", result["message"])
            self.assertIsNotNone(result["receipt_id"])            # the search noticed Stop itself

    # ------------------------------------------------------------ privacy
    def test_sensitive_fixture_strings_stay_out_of_task_state_and_logs(self):
        with self.rt() as rt:
            self.setup_rt(rt, "secret:3b")
            cid = rt.new_chat()
            ev = rt.chat(cid, "secret:3b", f"what is the code word? I think it is {CANARY}")
            self.assertIn(CANARY, "".join(e.get("text", "") for e in ev))      # the answer has it...
            self.assertEqual(ev[-1]["report"]["actions"], {"completed": 3})
            conn = sqlite3.connect(rt.config.db_path)
            dump = "\n".join(conn.iterdump())
            conn.close()
            task_rows = "\n".join(l for l in dump.splitlines() if l.startswith('INSERT INTO "task'))
            self.assertTrue(task_rows)
            self.assertNotIn(CANARY, task_rows)         # ...task state does not (message, answer, file, query)
            self.assertNotIn(TOKEN_NAME, task_rows)     # a token-shaped path is redacted
            self.assertIn("[REDACTED", task_rows)
            self.assertNotIn(CANARY, rt.log_stream.getvalue())
            self.assertNotIn(TOKEN_NAME, rt.log_stream.getvalue())
            receipts_rows = "\n".join(l for l in dump.splitlines() if l.startswith('INSERT INTO "receipts"'))
            self.assertNotIn(CANARY, receipts_rows)

    def test_cli_tasks_and_purge(self):
        from umbrous.cli import main
        import contextlib
        import io
        with self.rt() as rt:
            self.setup_rt(rt, "multi:3b")
            tid = rt.chat(rt.new_chat(), "multi:3b", "go")[-1]["task_id"]
            home = str(rt.home)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--home", home, "tasks"]), 0)
                self.assertEqual(main(["--home", home, "tasks", "--task", tid]), 0)
            self.assertIn(tid, out.getvalue())
            self.assertIn("steps=4 actions={'completed': 3}", out.getvalue())   # the listing reads the report
            self.assertIn("action.completed", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--home", home, "purge", "tasks", "--all"]), 1)   # needs --yes
                self.assertEqual(TaskStore(rt.config.db_path).count(), 1)
                self.assertEqual(main(["--home", home, "purge", "tasks", "--all", "--yes"]), 0)
            self.assertEqual(TaskStore(rt.config.db_path).count(), 0)
            self.assertEqual(rt.get_json(f"/api/tasks/{tid}")[0], 404)
            # receipts are separate and stay until purged on their own
            self.assertEqual(len(rt.get_json(f"/api/receipts?task_id={tid}")[1]["receipts"]), 3)


if __name__ == "__main__":
    unittest.main()
