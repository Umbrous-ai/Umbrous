"""M8: selective start-fresh reset.

  PreviewTests   a preview names the exact items, stores nothing, and says
                 what stays; there is no implicit delete-all.
  ConfirmTests   confirm deletes exactly the preview (mixed selection) and
                 nothing else; cancel is a no-op; a changed preview, a busy
                 chat or a stale id deletes nothing; the freed space holds no
                 trace; reset memory is never retrieved again.
  ApiTests       the two routes end to end, a fresh chat after a reset, and
                 no titles or text in the log.
  CliTests       preview, then --confirm; refused while the runtime runs.
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama, notes_agent
from tests.helpers import RunningRuntime
from umbrous.history.store import HistoryStore
from umbrous.memory.retrieve import select
from umbrous.memory.review import Review
from umbrous.memory.store import Memory
from umbrous.persistence import db
from umbrous.reset import Reset, ResetError
from umbrous.runloop.chat import ChatLocks

TABLES = ("chats", "messages", "pending_turns", "personal_memory", "project_memory", "memory_candidates",
          "tasks", "receipts", "projects")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "u.db"
        db.connect(self.path).close()
        self.history = HistoryStore(self.path)
        self.mem = Memory(self.path)
        self.rs = Reset(self.path)
        self.a = self.history.create_project("Alpha").id
        self.b = self.history.create_project("Beta").id
        self.a1 = self.chat(self.a, "Alpha one SECRETMARKA1", "reply a1")
        self.a2 = self.chat(self.a, "Alpha two", "reply a2")
        self.b1 = self.chat(self.b, "Beta one", "reply b1")
        self.p1 = self.mem.personal.add("Prefers tea MARKP1", "preference").id
        self.p2 = self.mem.personal.add("Prefers dark mode", "preference", [self.a]).id
        self.m_a = self.mem.project.add(self.a, "Ship on Friday MARKMA", "decision").id
        self.m_a2 = self.mem.project.add(self.a, "Release branch is ship", "state").id
        self.m_b = self.mem.project.add(self.b, "Beta uses Rust", "decision").id

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, project, text, reply, memory_ids=None):
        cid = self.history.create_chat(project).id
        self.history.append_exchange(cid, text, reply, "m:1b", "complete", memory_ids=memory_ids)
        return cid

    def sql(self, q, args=()):
        conn = sqlite3.connect(self.path)
        try:
            out = conn.execute(q, args).fetchall()
            conn.commit()
            return out
        finally:
            conn.close()

    def dump(self):
        return {t: sorted(map(repr, self.sql(f"SELECT * FROM {t}"))) for t in TABLES}

    def ids(self, preview, kind=None):
        return [i["id"] for i in preview["items"] if kind in (None, i["type"])]

    def add_candidate(self, cid, chat, status):
        self.sql("INSERT INTO memory_candidates (id, project_id, chat_id, task_id, store, kind, text, quote, "
                 "text_hash, scope, basis, evidence, status, created_at) VALUES (?, ?, ?, 'tsk_1', 'project', "
                 "'lesson', ?, ?, 'h', ?, 'message', '[]', ?, 'now')",
                 (cid, self.a, chat, "Lesson MARKCAND" if status == "pending" else "",
                  "quote MARKCAND" if status == "pending" else "", json.dumps(self.a), status))


class PreviewTests(Base):
    def test_preview_names_exact_items_and_stores_nothing(self):
        before = self.dump()
        pv = self.rs.preview({"chats": [self.a1], "personal": [self.p1], "project": {self.a: "all"}})
        self.assertEqual(self.dump(), before)                        # a preview changes nothing
        self.assertEqual(self.ids(pv, "chat"), [self.a1])
        self.assertEqual(self.ids(pv, "personal"), [self.p1])
        self.assertEqual(sorted(self.ids(pv, "project")), sorted([self.m_a, self.m_a2]))
        self.assertEqual(pv["counts"], {"chats": 1, "messages": 2, "personal": 1, "project": 2, "suggestions": 0})
        chat = pv["items"][0]
        self.assertEqual((chat["title"], chat["project"], chat["messages"], chat["busy"]),
                         ("Alpha one SECRETMARKA1", "Alpha", 2, False))
        self.assertTrue(pv["preview_id"].startswith("rst_"))
        self.assertIn("cannot be reversed", pv["backups"])
        self.assertIn("Time Machine", pv["backups"])
        # the same selection, the same state: the same preview id
        again = self.rs.preview({"project": {self.a: "all"}, "personal": [self.p1], "chats": [self.a1]})
        self.assertEqual(again["preview_id"], pv["preview_id"])

    def test_all_chats_of_one_project_includes_archived_and_nothing_else(self):
        self.history.set_archived(self.a2, True)
        pv = self.rs.preview({"chats_in": [self.a]})
        self.assertEqual(sorted(self.ids(pv)), sorted([self.a1, self.a2]))
        self.assertEqual([i["archived"] for i in pv["items"] if i["id"] == self.a2], [True])

    def test_no_implicit_delete_all_and_bad_selections(self):
        for sel, code in (({}, "nothing_selected"), ({"chats": []}, "nothing_selected"),
                          ("all", "bad_selection"), ({"everything": True}, "bad_selection"),
                          ({"chats": "all"}, "bad_selection"), ({"chats": ["../x"]}, "bad_selection"),
                          ({"project": "all"}, "bad_selection"), ({"project": {"all": "all"}}, "bad_selection"),
                          ({"chats": ["chat_000000000000000000000000"]}, "no_such_chat"),
                          ({"personal": ["mem_0000000000000000"]}, "no_such_memory"),
                          ({"project": {self.b: [self.m_a]}}, "no_such_memory"),     # an item of another project
                          ({"chats_in": ["prj_nope"]}, "no_such_project")):
            with self.assertRaises(ResetError) as e:
                self.rs.preview(sel)
            self.assertEqual(e.exception.code, code, sel)

    def test_kept_says_what_stays(self):
        # a task with a receipt in a1; a memory item accepted from a1; a1's pending and decided suggestions;
        # a kept chat whose reply used m_a
        self.sql("INSERT INTO tasks (task_id, project_id, chat_id, status, created_at, limits) "
                 "VALUES ('tsk_1', ?, ?, 'completed', 'now', '{}')", (self.a, self.a1))
        self.sql("INSERT INTO receipts (receipt_id, task_id, action_id, project_id, chat_id, action, decision, "
                 "started_at, record) VALUES ('rct_1', 'tsk_1', 'act_1', ?, ?, 'read', 'allowed', 'now', '{}')",
                 (self.a, self.a1))
        self.mem.project.add(self.a, "Keep backups", "lesson", source={"type": "task", "chat_id": self.a1})
        self.add_candidate("cand_aaaaaaaaaaaaaaaa", self.a1, "pending")
        self.add_candidate("cand_bbbbbbbbbbbbbbbb", self.a1, "rejected")
        self.chat(self.a, "When do we ship?", "On Friday MARKMA", memory_ids=[self.m_a])
        pv = self.rs.preview({"chats": [self.a1], "project": {self.a: [self.m_a]}})
        self.assertEqual(self.ids(pv, "suggestion"), ["cand_aaaaaaaaaaaaaaaa"])
        kept = {k["what"]: k["count"] for k in pv["kept"]}
        self.assertEqual(kept, {"task_records": 2, "memory_from_chats": 1, "review_history": 1,
                                "replies_using_memory": 1})
        self.assertIn("purge tasks", next(k["text"] for k in pv["kept"] if k["what"] == "task_records"))


class ConfirmTests(Base):
    def test_cancel_is_a_no_op(self):
        before = self.dump()
        self.rs.preview({"chats_in": [self.a, self.b], "personal": "all", "project": {self.a: "all"}})
        # cancelling is not confirming: there is nothing to call, and nothing changed
        self.assertEqual(self.dump(), before)

    def test_mixed_selection_deletes_exactly_the_preview(self):
        sel = {"chats": [self.a1], "personal": [self.p1], "project": {self.a: [self.m_a]}}
        before = self.dump()
        pv = self.rs.preview(sel)
        out = self.rs.confirm(sel, pv["preview_id"])
        self.assertEqual(out["deleted"], {"suggestions": 0, "pending_turns": 0, "messages": 2, "chats": 1,
                                          "personal": 1, "project": 1})
        after = self.dump()
        gone = {t: sorted(set(before[t]) - set(after[t])) for t in TABLES}
        self.assertEqual({t: len(v) for t, v in gone.items() if v},
                         {"chats": 1, "messages": 2, "personal_memory": 1, "project_memory": 1})
        self.assertTrue(all(self.a1 in r for r in gone["chats"] + gone["messages"]))
        self.assertIn(self.p1, gone["personal_memory"][0])
        self.assertIn(self.m_a, gone["project_memory"][0])
        self.assertTrue(all(set(after[t]) <= set(before[t]) for t in TABLES))    # nothing added or changed
        self.assertEqual([c.id for c in self.history.list_chats(self.a)], [self.a2])
        self.assertEqual(sorted(m.id for m in self.mem.personal.list()), [self.p2])

    def test_reset_memory_never_enters_a_fresh_context(self):
        sel = {"personal": [self.p1], "project": {self.a: [self.m_a]}}
        self.rs.confirm(sel, self.rs.preview(sel)["preview_id"])
        got = [i.id for i in select(self.mem, self.a, "tea ship friday dark release", max_items=50,
                                    max_tokens=8000).items]
        self.assertNotIn(self.p1, got)
        self.assertNotIn(self.m_a, got)
        self.assertEqual(sorted(got), sorted([self.p2, self.m_a2]))   # what was not selected still is

    def test_a_changed_preview_needs_revalidation(self):
        changes = {
            "new message": ({"chats": [self.a1]},
                            lambda: self.history.append_exchange(self.a1, "more", "more", "m:1b", "complete")),
            "new chat": ({"chats_in": [self.b]}, lambda: self.chat(self.b, "Beta two", "r")),
            "edited item": ({"project": {self.a: [self.m_a]}},
                            lambda: self.mem.project.update(self.m_a, "Ship on Monday", None)),
            "new item": ({"personal": "all"}, lambda: self.mem.personal.add("Likes cats", "preference")),
            "archived": ({"chats": [self.a2]}, lambda: self.history.set_archived(self.a2, True)),
        }
        for name, (sel, change) in changes.items():
            with self.subTest(name):
                pv = self.rs.preview(sel)
                change()
                before = self.dump()
                with self.assertRaises(ResetError) as e:
                    self.rs.confirm(sel, pv["preview_id"])
                self.assertEqual(e.exception.code, "preview_changed")
                self.assertEqual(self.dump(), before)                  # nothing deleted, nothing broader
                new = e.exception.preview
                self.assertNotEqual(new["preview_id"], pv["preview_id"])
                self.rs.confirm(sel, new["preview_id"])                # the new preview, confirmed, works

    def test_an_edited_waiting_suggestion_needs_revalidation(self):
        # Codex: a suggestion edited in review after the preview (text, kind,
        # store or scope) must make the old preview stale, like a memory edit
        review = Review(self.path, self.mem)
        edits = {"text": {"text": "Lesson MARKCAND edited"}, "kind": {"kind": "decision"},
                 "store": {"store": "personal", "scope": "all"}}
        for n, (name, kw) in enumerate(edits.items()):
            with self.subTest(name):
                cand = f"cand_{n:016d}"
                self.add_candidate(cand, self.a2, "pending")
                sel = {"chats": [self.a2]}
                pv = self.rs.preview(sel)
                self.assertIn(cand, self.ids(pv, "suggestion"))
                review.edit(cand, **kw)
                before = self.dump()
                with self.assertRaises(ResetError) as e:
                    self.rs.confirm(sel, pv["preview_id"])
                self.assertEqual(e.exception.code, "preview_changed")
                self.assertEqual(self.dump(), before)
                self.sql("DELETE FROM memory_candidates WHERE id = ?", (cand,))

    def test_an_item_gone_since_the_preview_is_refused(self):
        sel = {"chats": [self.a1, self.a2]}
        pv = self.rs.preview(sel)
        self.rs.confirm({"chats": [self.a1]}, self.rs.preview({"chats": [self.a1]})["preview_id"])
        before = self.dump()
        with self.assertRaises(ResetError) as e:
            self.rs.confirm(sel, pv["preview_id"])
        self.assertEqual(e.exception.code, "no_such_chat")
        self.assertEqual(self.dump(), before)

    def test_a_wrong_or_missing_preview_id_deletes_nothing(self):
        before = self.dump()
        for pid in ("", "rst_0", "x"):
            with self.assertRaises(ResetError) as e:
                self.rs.confirm({"chats": [self.a1]}, pid)
            self.assertEqual(e.exception.code, "preview_changed")
        # a preview id of another selection does not confirm this one
        other = self.rs.preview({"chats": [self.a2]})["preview_id"]
        with self.assertRaises(ResetError):
            self.rs.confirm({"chats": [self.a1, self.a2]}, other)
        self.assertEqual(self.dump(), before)

    def test_a_chat_with_a_running_reply_is_refused(self):
        locks = ChatLocks()
        sel = {"chats": [self.a1], "personal": [self.p1]}
        lock = locks.get(self.a1)
        lock.acquire()
        try:
            pv = self.rs.preview(sel, lambda cid: locks.get(cid).locked())
            self.assertEqual(pv["blocked"], [self.a1])
            before = self.dump()
            with self.assertRaises(ResetError) as e:
                self.rs.confirm(sel, pv["preview_id"], locks)
            self.assertEqual(e.exception.code, "chat_busy")
            self.assertEqual(self.dump(), before)                      # not even the memory item
        finally:
            lock.release()
        self.rs.confirm(sel, pv["preview_id"], locks)
        self.assertNotIn(self.a1, locks._locks)                         # the server forgets the chat

    def test_chat_reset_takes_its_pending_turn_and_waiting_suggestions_only(self):
        self.sql("INSERT INTO tasks (task_id, project_id, chat_id, status, created_at, limits) "
                 "VALUES ('tsk_1', ?, ?, 'unknown', 'now', '{}')", (self.a, self.a1))
        self.history.begin_pending("tsk_1", self.a1, "an interrupted message MARKPEND", "m:1b")
        PreviewTests.add_candidate(self, "cand_aaaaaaaaaaaaaaaa", self.a1, "pending")
        PreviewTests.add_candidate(self, "cand_bbbbbbbbbbbbbbbb", self.a1, "rejected")
        sel = {"chats": [self.a1]}
        out = self.rs.confirm(sel, self.rs.preview(sel)["preview_id"])
        self.assertEqual((out["deleted"]["pending_turns"], out["deleted"]["suggestions"]), (1, 1))
        self.assertEqual(self.sql("SELECT id, status FROM memory_candidates"),
                         [("cand_bbbbbbbbbbbbbbbb", "rejected")])
        self.assertEqual(self.sql("SELECT task_id, chat_id FROM tasks"), [("tsk_1", self.a1)])   # kept, by id

    def test_freed_space_holds_no_trace(self):
        PreviewTests.add_candidate(self, "cand_aaaaaaaaaaaaaaaa", self.a1, "pending")
        sel = {"chats": [self.a1], "personal": [self.p1], "project": {self.a: [self.m_a]}}
        raw = self.path.read_bytes()
        for mark in (b"SECRETMARKA1", b"MARKP1", b"MARKMA", b"MARKCAND"):
            self.assertIn(mark, raw)
        self.rs.confirm(sel, self.rs.preview(sel)["preview_id"])
        raw = self.path.read_bytes()
        for mark in (b"SECRETMARKA1", b"MARKP1", b"MARKMA", b"MARKCAND"):
            self.assertNotIn(mark, raw)
        self.assertFalse(Path(str(self.path) + "-wal").exists())

    def test_two_confirms_at_once_delete_once(self):
        sel = {"chats_in": [self.a]}
        pid = self.rs.preview(sel)["preview_id"]
        results = []

        def go():
            try:
                results.append(self.rs.confirm(sel, pid)["deleted"]["chats"])
            except ResetError as e:
                results.append(e.code)
        threads = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(map(str, results)), sorted(["2", "preview_changed"]))


# ---------------------------------------------------------------- end to end
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeOllama(models=[FakeModel("notes:3b", agent=notes_agent)]).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def start(self):
        rt = RunningRuntime(ollama_url=self.fake.url).__enter__()
        self.addCleanup(rt.__exit__, None, None, None)
        return rt

    def post(self, rt, path, body):
        status, _, out = rt.post_json(path, body)
        return status, json.loads(out)

    def test_preview_cancel_confirm_and_a_fresh_context(self):
        rt = self.start()
        keep, gone = rt.new_chat(), rt.new_chat()
        rt.chat(gone, "notes:3b", "the codename is BLUEHERON")
        rt.chat(keep, "notes:3b", "hello")
        status, item = self.post(rt, "/api/memory", {"store": "personal", "text": "Codename BLUEHERON",
                                                     "kind": "fact"})
        mem_id = item["item"]["id"]
        sel = {"chats": [gone], "personal": [mem_id], "start_fresh": True}
        status, pv = self.post(rt, "/api/reset/preview", {"selection": sel})
        self.assertEqual((status, [i["id"] for i in pv["items"]]), (200, [gone, mem_id]))
        # cancel: nothing is called; the chat and the item are still there
        self.assertEqual(rt.get_json(f"/api/chats/{gone}")[0], 200)
        self.assertEqual(len(rt.get_json("/api/memory")[1]["personal"]), 1)
        status, out = self.post(rt, "/api/reset", {"selection": sel, "preview_id": pv["preview_id"]})
        self.assertEqual((status, out["deleted"]["chats"], out["deleted"]["personal"], out["start_fresh"]),
                         (200, 1, 1, True))
        self.assertEqual(rt.get_json(f"/api/chats/{gone}")[0], 404)
        self.assertEqual(rt.get_json(f"/api/chats/{keep}")[0], 200)
        self.assertEqual(rt.get_json("/api/memory")[1]["personal"], [])
        # a fresh chat is given no reset memory
        fresh = rt.new_chat()
        rt.chat(fresh, "notes:3b", "what is the codename?")
        prompt = json.dumps(self.fake.requests[-1]["body"])
        self.assertNotIn("BLUEHERON", prompt)
        # logs: ids and counts, never the title or the memory text
        logs = rt.log_stream.getvalue()
        self.assertIn("reset.done", logs)
        self.assertNotIn("BLUEHERON", logs)

    def test_changed_preview_and_errors_over_http(self):
        rt = self.start()
        cid = rt.new_chat()
        rt.chat(cid, "notes:3b", "one")
        sel = {"chats_in": ["prj_general"]}
        status, pv = self.post(rt, "/api/reset/preview", {"selection": sel})
        rt.chat(rt.new_chat(), "notes:3b", "two")
        status, out = self.post(rt, "/api/reset", {"selection": sel, "preview_id": pv["preview_id"]})
        self.assertEqual((status, out["error"]), (409, "preview_changed"))
        self.assertEqual(len(out["preview"]["items"]), 2)
        self.assertEqual(rt.get_json(f"/api/chats/{cid}")[0], 200)
        self.assertEqual(self.post(rt, "/api/reset/preview", {"selection": {}}), (400, {
            "error": "nothing_selected", "message": "Nothing is selected. Choose chats or memory items to reset."}))
        self.assertEqual(self.post(rt, "/api/reset/preview", {"selection": {"chats": ["chat_000000000000000000000000"]}}
                                   )[0], 404)
        self.assertEqual(rt.request("GET", "/api/reset/preview", token=rt.policy.session_token)[0], 405)
        # the routes need the session token like every other
        self.assertEqual(rt.request("POST", "/api/reset", headers={"Content-Type": "application/json"},
                                    body=b"{}")[0], 401)


class CliTests(unittest.TestCase):
    def test_preview_then_confirm(self):
        from umbrous.cli import main
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "umbrous.db"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--home", home, "memory", "add", "personal", "Prefers tea"])
            mem = Memory(path).personal.list()[0].id
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--home", home, "reset", "--personal", mem]), 0)
            text = out.getvalue()
            self.assertIn("Prefers tea", text)
            self.assertIn("Nothing has been deleted", text)
            preview_id = text.rsplit("--confirm ", 1)[1].strip()
            self.assertEqual(len(Memory(path).personal.list()), 1)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--home", home, "reset", "--personal", mem, "--confirm", "rst_x"]), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--home", home, "reset", "--personal", mem, "--confirm", preview_id]), 0)
            self.assertEqual(Memory(path).personal.list(), [])

    def test_confirm_refused_while_the_runtime_runs(self):
        from umbrous import cli
        with tempfile.TemporaryDirectory() as home:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--home", home, "memory", "add", "personal", "Prefers tea"])
            run = Path(home) / "run"
            run.mkdir(exist_ok=True)
            import os
            (run / "runtime.json").write_text(json.dumps({"pid": os.getppid()}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--home", home, "reset", "--personal", "all", "--confirm", "rst_x"]), 1)
            self.assertIn("Quit it first", err.getvalue())


if __name__ == "__main__":
    unittest.main()
