"""M6: personal memory and project operational memory.

  StoreTests       the two repositories: fields, validation, edit, delete,
                   and that neither reads or writes chat tables.
  RetrievalTests   bounded retrieval: project isolation, personal scope,
                   approval state, limits, ranking.
  ApiTests         the runtime end to end: a memory item visibly changes a
                   reply, the ids used are stored with the reply and shown,
                   deleting an item stops future retrieval and leaves the
                   transcript as it was, and the other way round.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_ollama import FakeModel, FakeOllama, memory_answer
from tests.helpers import RunningRuntime
from umbrous.context.assemble import assemble
from umbrous.history.store import HistoryStore
from umbrous.memory import retrieve
from umbrous.memory.retrieve import select
from umbrous.memory.store import MAX_TEXT, Memory, MemoryError_
from umbrous.persistence import db
from tests.helpers import project_fixture_db


def models():
    return [FakeModel("memory-aware:3b", tools="unsupported", answer=memory_answer),
            FakeModel("memory-dies:3b", tools="unsupported", behavior="die_midstream", answer=memory_answer)]


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "u.db"
        project_fixture_db(self.path).close()
        self.history = HistoryStore(self.path)
        self.mem = Memory(self.path)
        self.a = self.history.create_project("Alpha").id
        self.b = self.history.create_project("Beta").id

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, table):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            conn.close()


class StoreTests(StoreBase):
    def test_item_has_id_scope_source_times_and_approval(self):
        p = self.mem.personal.add("I like short answers", "preference")
        self.assertRegex(p.id, r"^mem_[0-9a-f]{16}$")
        self.assertEqual((p.store, p.kind, p.scope, p.source, p.approval), ("personal", "preference", "all",
                                                                            {"type": "manual"}, "approved"))
        self.assertTrue(p.created_at and p.updated_at)
        q = self.mem.project.add(self.a, "We use SQLite", "decision")
        self.assertEqual((q.store, q.scope, q.approval), ("project", self.a, "approved"))

    def test_two_tables_two_repositories(self):
        self.mem.personal.add("fact one")
        self.mem.project.add(self.a, "decision one")
        self.assertEqual(len(self.rows("personal_memory")), 1)
        self.assertEqual(len(self.rows("project_memory")), 1)
        self.assertIsNone(self.mem.personal.get(self.mem.project.list(self.a)[0].id))

    def test_validation(self):
        for bad in ("", "   ", "x" * (MAX_TEXT + 1), None, 5):
            with self.assertRaises(MemoryError_):
                self.mem.personal.add(bad)
        with self.assertRaises(MemoryError_) as e:
            self.mem.personal.add("token sk-abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertEqual(e.exception.code, "looks_secret")
        with self.assertRaises(MemoryError_):
            self.mem.personal.add("ok", kind="decision")          # a project kind
        with self.assertRaises(MemoryError_):
            self.mem.project.add(self.a, "ok", kind="preference")  # a personal kind
        with self.assertRaises(MemoryError_) as e:
            self.mem.project.add("prj_nope", "ok")
        self.assertEqual(e.exception.code, "no_such_project")
        with self.assertRaises(MemoryError_):
            self.mem.personal.add("ok", scope=[])
        with self.assertRaises(MemoryError_):
            self.mem.personal.add("ok", scope=["prj_nope"], project_exists=lambda p: False)

    def test_edit(self):
        p = self.mem.personal.add("old text")
        e = self.mem.personal.update(p.id, "new text", "preference", [self.a])
        self.assertEqual((e.text, e.kind, e.scope, e.created_at), ("new text", "preference", [self.a], p.created_at))
        self.assertGreaterEqual(e.updated_at, p.updated_at)
        q = self.mem.project.add(self.a, "a lesson", "lesson")
        self.assertEqual(self.mem.project.update(q.id, kind="state").kind, "state")
        with self.assertRaises(MemoryError_):
            self.mem.project.update("mem_0000000000000000", "x")

    def test_delete_removes_only_that_item_and_no_transcript(self):
        chat = self.history.create_chat(self.a).id
        keep = self.mem.personal.add("keep me")
        gone = self.mem.personal.add("delete me")
        self.history.append_exchange(chat, "q", "a reply mentioning delete me", "m", "complete",
                                     memory_ids=[gone.id, keep.id])
        before = self.rows("messages")
        self.assertTrue(self.mem.delete(gone.id))
        self.assertFalse(self.mem.delete(gone.id))
        self.assertIsNone(self.mem.get(gone.id))
        self.assertEqual([m.id for m in self.mem.personal.list()], [keep.id])
        self.assertEqual(self.rows("messages"), before)     # the old reply and its reference are unchanged
        self.assertEqual(self.history.messages(chat)[1].memory_ids, [gone.id, keep.id])

    def test_transcript_changes_leave_memory_untouched(self):
        """There is no transcript delete yet (M2). What a transcript can go
        through (new messages, archive, and removing its rows outright, as a
        future delete would) leaves memory exactly as it was."""
        chat = self.history.create_chat(self.a).id
        self.mem.personal.add("a fact")
        self.mem.project.add(self.a, "a decision")
        before = (self.rows("personal_memory"), self.rows("project_memory"))
        self.history.append_exchange(chat, "q", "a", "m", "complete")
        self.history.set_archived(chat, True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat,))
        conn.execute("DELETE FROM chats WHERE id = ?", (chat,))
        conn.close()
        self.assertEqual((self.rows("personal_memory"), self.rows("project_memory")), before)

    def test_memory_tables_have_no_link_to_chats(self):
        conn = sqlite3.connect(self.path)
        for table in ("personal_memory", "project_memory"):
            refs = {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
            self.assertFalse(refs & {"chats", "messages", "pending_turns", "tasks"}, table)
        conn.close()

    def test_memory_module_never_touches_chat_tables(self):
        src = "".join((Path(retrieve.__file__).parent / f).read_text() for f in ("store.py", "retrieve.py"))
        for table in ("messages", "chats", "pending_turns", "tasks", "receipts"):
            self.assertNotRegex(src, rf"(FROM|INTO|UPDATE|TABLE)\s+{table}\b")


class RetrievalTests(StoreBase):
    def pick(self, project, msg="hello", **kw):
        return select(self.mem, project, msg, max_items=kw.get("max_items", 8), max_tokens=kw.get("max_tokens", 600))

    def test_cross_project_isolation(self):
        a = self.mem.project.add(self.a, "Alpha ships on Friday")
        b = self.mem.project.add(self.b, "Beta uses Postgres")
        self.assertEqual(self.pick(self.a, "what ships Friday Postgres").ids, [a.id])
        self.assertEqual(self.pick(self.b, "what ships Friday Postgres").ids, [b.id])
        general = self.pick("prj_general", "Alpha Beta Friday Postgres")
        self.assertEqual(general.ids, [])
        self.assertNotIn("Alpha ships", general.block + self.pick(self.b).block)

    def test_isolation_even_if_a_store_returned_another_project(self):
        a = self.mem.project.add(self.a, "Alpha secret plan")
        with mock.patch.object(self.mem.project, "for_project", return_value=[a]):
            self.assertEqual(self.pick(self.b).ids, [])

    def test_personal_scope(self):
        everywhere = self.mem.personal.add("I write British English", "preference")
        only_a = self.mem.personal.add("At Alpha I am the tech lead", scope=[self.a])
        self.assertEqual(set(self.pick(self.a).ids), {everywhere.id, only_a.id})
        self.assertEqual(self.pick(self.b).ids, [everywhere.id])
        self.mem.personal.update(only_a.id, scope=[self.b])
        self.assertEqual(set(self.pick(self.b).ids), {everywhere.id, only_a.id})
        self.assertEqual(self.pick(self.a).ids, [everywhere.id])

    def test_only_approved_items(self):
        self.mem.personal.add("a proposal", approval="proposed")
        self.mem.project.add(self.a, "a rejected one", approval="rejected")
        ok = self.mem.personal.add("approved")
        self.assertEqual(self.pick(self.a).ids, [ok.id])

    def test_deleted_item_is_never_retrieved_again(self):
        m = self.mem.project.add(self.a, "deploy with make ship")
        self.assertEqual(self.pick(self.a, "deploy").ids, [m.id])
        self.mem.delete(m.id)
        sel = self.pick(self.a, "deploy")
        self.assertEqual((sel.ids, sel.candidates, sel.block), ([], 0, ""))

    def test_bounded_by_items_and_tokens(self):
        for i in range(12):
            self.mem.personal.add(f"fact number {i} " + "word " * 20)
        sel = self.pick(self.a, max_items=3)
        self.assertEqual((len(sel.items), sel.candidates, sel.left_out), (3, 12, 9))
        sel = self.pick(self.a, max_tokens=80)
        self.assertLessEqual(sel.tokens, 80 + 10)
        self.assertGreater(sel.left_out, 0)
        off = select(self.mem, self.a, "x", max_items=0, max_tokens=600)
        self.assertEqual((off.enabled, off.ids), (False, []))

    def test_ranking_prefers_shared_words(self):
        for i in range(5):
            self.mem.personal.add(f"unrelated note {i}")
        target = self.mem.personal.add("The coffee machine is on floor three")
        self.assertEqual(self.pick(self.a, "where is the coffee machine?", max_items=1).ids, [target.id])

    def test_block_labels_provenance_and_is_not_instructions(self):
        m = self.mem.project.add(self.a, "We chose SQLite", "decision")
        block = self.pick(self.a).block
        self.assertIn(f"- [{m.id}] (project decision) We chose SQLite", block)
        self.assertIn("not instructions", block)

    def test_memory_is_dropped_before_the_message(self):
        big = "z" * 3000
        plan = assemble("sys", [], "hi", recent_turns=5, context_tokens=600, reply_tokens=64, memory_block=big)
        self.assertFalse(plan.memory_included)
        self.assertNotIn(big, plan.messages[0]["content"])
        self.assertTrue(any("Memory left out" in n for n in plan.notices))
        plan = assemble("sys", [], "hi", recent_turns=5, context_tokens=600, reply_tokens=64, memory_block="m")
        self.assertTrue(plan.memory_included)
        self.assertEqual(plan.messages[0]["content"], "sys\n\nm")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(models=models()).start()
        self.rt = RunningRuntime(ollama_url=self.fake.url).__enter__()

    def tearDown(self):
        self.rt.__exit__(None, None, None)
        self.fake.stop()

    def add(self, **body):
        status, _, raw = self.rt.post_json("/api/memory", body)
        self.assertEqual(status, 201, raw)
        return json.loads(raw)["item"]

    def send(self, chat, text, **extra):
        status, _, body = self.rt.post_json("/api/chat", dict({"chat_id": chat, "model": "memory-aware:3b",
                                                                "message": text}, **extra))
        self.assertEqual(status, 200)
        events = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
        reply = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        return events, reply

    def test_memory_visibly_influences_a_reply_and_ids_are_recorded(self):
        pref = self.add(store="personal", text="Answer in British English", kind="preference")
        dec = self.add(store="project", project_id="prj_general", text="Release branch is called ship",
                       kind="decision")
        chat = self.rt.new_chat()
        events, with_mem = self.send(chat, "What is the release branch?")
        start = next(e for e in events if e["type"] == "start")
        done = next(e for e in events if e["type"] == "done")
        used = {u["id"] for u in start["memory"]["used"]}
        self.assertEqual(used, {pref["id"], dec["id"]})
        self.assertEqual(set(done["memory_ids"]), used)
        self.assertIn("Release branch is called ship", with_mem)
        self.assertIn(dec["id"], with_mem)
        # the same question without memory: the reply changes
        events, without = self.send(chat, "What is the release branch?", use_memory=False)
        self.assertIn("no memory", without)
        self.assertEqual(next(e for e in events if e["type"] == "done")["memory_ids"], [])
        sent = self.fake.requests[-1]["body"]["messages"][0]["content"]
        self.assertNotIn("Release branch", sent)
        # Details: the transcript keeps which ids each reply used
        _, chat_view = self.rt.get_json(f"/api/chats/{chat}")
        replies = [m for m in chat_view["messages"] if m["role"] == "assistant"]
        self.assertEqual(set(replies[0]["memory_ids"]), used)
        self.assertIsNone(replies[1]["memory_ids"])

    def test_deleting_memory_stops_retrieval_and_keeps_the_old_chat(self):
        dec = self.add(store="project", project_id="prj_general", text="Deploy day is Thursday")
        chat = self.rt.new_chat()
        _, first = self.send(chat, "When do we deploy?")
        self.assertIn("Thursday", first)
        status, _, _ = self.rt.post_json(f"/api/memory/{dec['id']}/delete", {})
        self.assertEqual(status, 200)
        _, second = self.send(chat, "When do we deploy?")
        self.assertNotIn("Thursday", second)
        self.assertNotIn("Thursday", self.fake.requests[-1]["body"]["messages"][0]["content"])
        _, view = self.rt.get_json(f"/api/chats/{chat}")
        # The old reply still says it: deleting memory does not rewrite a chat.
        self.assertIn("Thursday", view["messages"][1]["content"])
        self.assertEqual(view["messages"][1]["memory_ids"], [dec["id"]])
        _, mem = self.rt.get_json("/api/memory?project_id=prj_general")
        self.assertEqual(mem["project"], [])
        status, _, _ = self.rt.post_json(f"/api/memory/{dec['id']}/delete", {})
        self.assertEqual(status, 404)

    def prompt(self):
        """The last request's system message, and the rest (history and the new message)."""
        msgs = self.fake.requests[-1]["body"]["messages"]
        return msgs[0]["content"], "\n".join(m["content"] for m in msgs[1:])

    def test_off_and_delete_stop_retrieval_but_history_still_carries_the_fact(self):
        """The promise is no further retrieval. An earlier reply that repeated the
        item is chat history, and goes back to the model in that chat; a new chat
        has no such history. (The fake model ignores history, so this is checked
        on the prompt itself, not on its answer.)"""
        dec = self.add(store="project", project_id="prj_general", text="Deploy day is ZEBRA-THURSDAY")
        chat = self.rt.new_chat()
        self.send(chat, "When do we deploy?")
        self.send(chat, "When do we deploy?", use_memory=False)
        system, rest = self.prompt()
        self.assertNotIn("ZEBRA-THURSDAY", system)          # not retrieved
        self.assertIn("ZEBRA-THURSDAY", rest)               # but in the history: the first reply said it
        self.rt.post_json(f"/api/memory/{dec['id']}/delete", {})
        self.send(chat, "When do we deploy?")
        system, rest = self.prompt()
        self.assertNotIn("ZEBRA-THURSDAY", system)
        self.assertIn("ZEBRA-THURSDAY", rest)
        fresh = self.rt.new_chat()                          # how to compare: a new chat
        self.send(fresh, "When do we deploy?")
        system, rest = self.prompt()
        self.assertNotIn("ZEBRA-THURSDAY", system + rest)

    def test_a_partial_reply_names_its_memory_in_its_last_event(self):
        item = self.add(store="personal", text="I like tea")
        chat = self.rt.new_chat()
        status, _, body = self.rt.post_json("/api/chat", {"chat_id": chat, "model": "memory-dies:3b",
                                                          "message": "Which drink do I like?"})
        events = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
        start, last = events[0], events[-1]
        self.assertEqual([u["id"] for u in start["memory"]["used"]], [item["id"]])
        self.assertEqual((last["type"], last["partial"]), ("error", True))
        self.assertEqual(last["memory_ids"], [item["id"]])
        _, view = self.rt.get_json(f"/api/chats/{chat}")
        self.assertEqual(view["messages"][1]["memory_ids"], [item["id"]])

    def test_archiving_a_chat_leaves_memory(self):
        item = self.add(store="personal", text="I like tea")
        chat = self.rt.new_chat()
        self.send(chat, "hi")
        self.assertEqual(self.rt.post_json(f"/api/chats/{chat}/archive", {})[0], 200)
        _, mem = self.rt.get_json("/api/memory?project_id=prj_general")
        self.assertEqual([m["id"] for m in mem["personal"]], [item["id"]])

    def test_project_memory_does_not_reach_another_project(self):
        status, _, raw = self.rt.post_json("/api/projects", {"name": "Other"})
        other = json.loads(raw)["project"]["id"]
        self.add(store="project", project_id="prj_general", text="General codename is Heron")
        self.add(store="personal", text="Only for General", scope=["prj_general"])
        chat = self.rt.new_chat(other)
        events, reply = self.send(chat, "What is the codename Heron?")
        self.assertNotIn("Heron", self.fake.requests[-1]["body"]["messages"][0]["content"])
        self.assertNotIn("Only for General", self.fake.requests[-1]["body"]["messages"][0]["content"])
        self.assertEqual(next(e for e in events if e["type"] == "start")["memory"]["used"], [])
        _, mem = self.rt.get_json(f"/api/memory?project_id={other}")
        self.assertEqual(mem["project"], [])
        self.assertFalse(mem["personal"][0]["applies"])

    def test_personal_memory_without_a_project_scope(self):
        shared = self.add(**{"store": "personal", "text": "Prefer concise explanations"})
        scoped = self.add(**{"store": "personal", "text": "Project release preference", "scope": ["prj_general"]})
        status, view = self.rt.get_json("/api/memory")
        self.assertEqual(status, 200)
        self.assertIsNone(view["project_id"])
        self.assertEqual(view["project"], [])
        self.assertEqual(view["candidates"], [])
        items = {m["id"]: m for m in view["personal"]}
        self.assertTrue(items[shared["id"]]["applies"])
        self.assertFalse(items[scoped["id"]]["applies"])

    def test_edit_and_errors(self):
        item = self.add(store="personal", text="draft")
        status, _, raw = self.rt.post_json(f"/api/memory/{item['id']}", {"text": "final", "kind": "preference"})
        self.assertEqual((status, json.loads(raw)["item"]["text"]), (200, "final"))
        proj = self.add(store="project", project_id="prj_general", text="x")
        self.assertEqual(self.rt.post_json(f"/api/memory/{proj['id']}", {"project_id": "prj_other"})[0], 400)
        self.assertEqual(self.rt.post_json("/api/memory", {"store": "nope", "text": "x"})[0], 400)
        self.assertEqual(self.rt.post_json("/api/memory", {"store": "project", "project_id": "prj_nope1",
                                                           "text": "x"})[0], 404)
        self.assertEqual(self.rt.post_json("/api/memory", {"store": "personal", "text": "pw sk-abcdefghijklmnopqrstuvwxyz0123"})[0], 400)
        self.assertEqual(self.rt.request("DELETE", f"/api/memory/{item['id']}",
                                         token=self.rt.policy.session_token)[0], 405)
        self.assertEqual(self.rt.get_json("/api/memory?project_id=bad")[0], 400)
        self.assertEqual(self.rt.request("GET", "/api/memory")[0], 401)   # needs the session token

    def test_memory_text_never_logged(self):
        self.add(store="personal", text="My cat is called Pistachio")
        self.assertNotIn("Pistachio", self.rt.log_stream.getvalue())


class SchemaTests(unittest.TestCase):
    def test_schema_7_upgrade_keeps_version_6_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path, isolation_level=None)
            with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:6]), mock.patch.object(db, "SCHEMA_VERSION", 6):
                db.migrate(conn)
            conn.execute("INSERT INTO chats (id, project_id, title, created_at, updated_at) "
                         "VALUES ('chat_x', 'prj_general', 't', 'now', 'now')")
            conn.execute("INSERT INTO messages (chat_id, seq, role, content, status, created_at) "
                         "VALUES ('chat_x', 1, 'user', 'hi', 'complete', 'now')")
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT content, memory_ids FROM messages").fetchone(), ("hi", None))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM personal_memory").fetchone()[0], 0)
            conn.close()
            self.assertIsNone(HistoryStore(path).messages("chat_x")[0].memory_ids)


if __name__ == "__main__":
    unittest.main()
