"""M2 acceptance: chat history and projects survive a restart; archive is lossless; nothing deletes."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.history.store import HistoryError, HistoryStore, make_title
from umbrous.persistence import db


def row_counts(path):
    conn = sqlite3.connect(str(path))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("projects", "chats", "messages")}
    finally:
        conn.close()


# ---------------------------------------------------------------- store
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "u.db"
        db.connect(self.path).close()
        self.h = HistoryStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_database_has_general(self):
        self.assertEqual([(p.id, p.name) for p in self.h.list_projects()], [("prj_general", "General")])

    def test_project_names(self):
        p = self.h.create_project("  Tax   2026 ")
        self.assertEqual(p.name, "Tax 2026")
        with self.assertRaises(HistoryError) as e:
            self.h.create_project("tax 2026")           # names are unique, ignoring case
        self.assertEqual(e.exception.code, "project_exists")
        for bad in ("", "   ", "x" * 61, None, 5):
            with self.assertRaises(HistoryError):
                self.h.create_project(bad)

    def test_chat_needs_an_existing_project(self):
        with self.assertRaises(HistoryError) as e:
            self.h.create_chat("prj_nope")
        self.assertEqual(e.exception.code, "no_such_project")

    def test_exchange_is_stored_in_order_and_titles_the_chat(self):
        c = self.h.create_chat("prj_general")
        self.h.append_exchange(c.id, "What is   the capital\nof France?", "Paris.", "m:1b", "complete", 12, 2)
        self.h.append_exchange(c.id, "And Spain?", "Madrid.", "m:1b", "complete")
        msgs = self.h.messages(c.id)
        self.assertEqual([(m.seq, m.role, m.content) for m in msgs],
                         [(1, "user", "What is   the capital\nof France?"), (2, "assistant", "Paris."),
                          (3, "user", "And Spain?"), (4, "assistant", "Madrid.")])
        self.assertEqual(msgs[1].model, "m:1b")
        self.assertEqual((msgs[1].prompt_tokens, msgs[1].reply_tokens), (12, 2))
        self.assertEqual(self.h.get_chat(c.id).title, "What is the capital of France?")  # first message only

    def test_long_title_is_cut(self):
        t = make_title("word " * 50)
        self.assertLessEqual(len(t), 80)
        self.assertTrue(t.endswith("…"))

    def test_projects_keep_their_chats_apart(self):
        work = self.h.create_project("Work")
        a = self.h.create_chat("prj_general")
        b = self.h.create_chat(work.id)
        self.assertEqual([c.id for c in self.h.list_chats("prj_general")], [a.id])
        self.assertEqual([c.id for c in self.h.list_chats(work.id)], [b.id])

    def test_archive_round_trip_loses_nothing(self):
        c = self.h.create_chat("prj_general")
        self.h.append_exchange(c.id, "keep me", "kept", "m:1b", "complete")
        before = row_counts(self.path)
        self.assertTrue(self.h.set_archived(c.id, True).archived)
        self.assertEqual(row_counts(self.path), before)               # archiving deletes no row
        self.assertEqual(self.h.list_chats("prj_general"), [])          # hidden from the normal list
        self.assertEqual([x.id for x in self.h.list_chats("prj_general", archived=True)], [c.id])
        with self.assertRaises(HistoryError) as e:                      # and read-only
            self.h.append_exchange(c.id, "more", "no", "m:1b", "complete")
        self.assertEqual(e.exception.code, "chat_archived")
        self.assertFalse(self.h.set_archived(c.id, False).archived)
        self.assertEqual([m.content for m in self.h.messages(c.id)], ["keep me", "kept"])
        self.h.append_exchange(c.id, "more", "yes", "m:1b", "complete")  # restored: can continue

    def test_store_has_no_delete_operation(self):
        names = [n for n in dir(HistoryStore) if not n.startswith("__")]
        self.assertFalse([n for n in names if any(w in n.lower() for w in ("delete", "remove", "purge", "drop"))])

    def test_failed_exchange_writes_nothing(self):
        c = self.h.create_chat("prj_general")
        with self.assertRaises(HistoryError):
            self.h.append_exchange(c.id, "x", "y", "m:1b", "not-a-status")
        self.assertEqual(self.h.messages(c.id), [])

    def test_upgrade_from_m1_database_keeps_data(self):
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(str(path), isolation_level=None)
        for v, script in enumerate(db.MIGRATIONS[:2], start=1):   # a database as M1 left it
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {v};\nCOMMIT;")
        db.record_launch(conn, "0.1.0")
        db.save_probe(conn, "q:3b", "sha256:1", True, "passed", "ok", 10)
        conn.close()
        conn = db.connect(path)
        self.assertEqual(db.schema_version(conn), 3)
        self.assertEqual(db.launch_count(conn), 1)
        self.assertIn(("q:3b", "sha256:1"), db.load_probes(conn))
        conn.close()
        self.assertEqual([p.name for p in HistoryStore(path).list_projects()], ["General"])


# ---------------------------------------------------------------- API
class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(models=[FakeModel("qwen2.5:3b", context_length=4096),
                                       FakeModel("dies:1b", behavior="die_midstream", reply="partial " * 10)]).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def rt(self):
        return RunningRuntime(home=self.home, ollama_url=self.fake.url)

    def chat_requests(self):
        return [r["body"] for r in self.fake.requests if r["path"] == "/api/chat"]

    def test_history_survives_a_restart_in_the_right_project(self):
        with self.rt() as rt:
            status, _, body = rt.post_json("/api/projects", {"name": "Garden"})
            self.assertEqual(status, 201)
            garden = json.loads(body)["project"]["id"]
            cid = rt.new_chat(garden)
            self.assertEqual(rt.chat(cid, "qwen2.5:3b", "when do I plant tulips?")[-1]["type"], "done")
        with self.rt() as rt:                                   # a new runtime on the same data
            _, chats = rt.get_json(f"/api/chats?project_id={garden}")
            self.assertEqual([(c["id"], c["title"], c["messages"]) for c in chats["chats"]],
                             [(cid, "when do I plant tulips?", 2)])
            _, general = rt.get_json("/api/chats?project_id=prj_general")
            self.assertEqual(general["chats"], [])               # not in General
            _, full = rt.get_json(f"/api/chats/{cid}")
            self.assertEqual([m["content"] for m in full["messages"]],
                             ["when do I plant tulips?", "You said: when do I plant tulips?"])
            self.assertEqual(full["messages"][1]["model"], "qwen2.5:3b")
            rt.chat(cid, "qwen2.5:3b", "and daffodils?")         # continuing uses the saved turns
            sent = [m["content"] for m in self.chat_requests()[-1]["messages"][1:]]
            self.assertEqual(sent, ["when do I plant tulips?", "You said: when do I plant tulips?", "and daffodils?"])

    def test_projects_list_and_separation(self):
        with self.rt() as rt:
            rt.post_json("/api/projects", {"name": "Work"})
            status, _, body = rt.post_json("/api/projects", {"name": "WORK"})
            self.assertEqual((status, json.loads(body)["error"]), (409, "project_exists"))
            _, data = rt.get_json("/api/projects")
            names = [p["name"] for p in data["projects"]]
            self.assertEqual(names, ["General", "Work"])
            work = data["projects"][1]["id"]
            a, b = rt.new_chat("prj_general"), rt.new_chat(work)
            rt.chat(a, "qwen2.5:3b", "general question")
            rt.chat(b, "qwen2.5:3b", "work question")
            _, w = rt.get_json(f"/api/chats?project_id={work}")
            self.assertEqual([c["id"] for c in w["chats"]], [b])
            # the model never sees another chat's messages
            self.assertNotIn("general question", json.dumps(self.chat_requests()[-1]))

    def test_archive_and_restore_through_the_api(self):
        with self.rt() as rt:
            cid = rt.new_chat()
            rt.chat(cid, "qwen2.5:3b", "remember this")
            before = row_counts(rt.config.db_path)
            status, _, _ = rt.post_json(f"/api/chats/{cid}/archive", {})
            self.assertEqual(status, 200)
            self.assertEqual(row_counts(rt.config.db_path), before)
            self.assertEqual(rt.get_json("/api/chats?project_id=prj_general")[1]["chats"], [])
            archived = rt.get_json("/api/chats?project_id=prj_general&archived=1")[1]["chats"]
            self.assertEqual([(c["id"], c["archived"], c["messages"]) for c in archived], [(cid, True, 2)])
            n = len(self.chat_requests())
            ev = rt.chat(cid, "qwen2.5:3b", "one more")              # read-only while archived
            self.assertEqual([e["code"] for e in ev], ["chat_archived"])
            self.assertEqual(len(self.chat_requests()), n)           # the model was not called
            rt.post_json(f"/api/chats/{cid}/unarchive", {})
            self.assertEqual(rt.chat(cid, "qwen2.5:3b", "one more")[-1]["type"], "done")
            self.assertEqual(rt.get_json(f"/api/chats/{cid}")[1]["chat"]["messages"], 4)

    def test_there_is_no_way_to_delete(self):
        with self.rt() as rt:
            cid = rt.new_chat()
            rt.chat(cid, "qwen2.5:3b", "still here?")
            tok = rt.policy.session_token
            h = {"Origin": rt.origin}
            for method, path in (("DELETE", f"/api/chats/{cid}"), ("DELETE", "/api/projects"),
                                 ("DELETE", "/api/chats"), ("PUT", f"/api/chats/{cid}")):
                status, _, _ = rt.request(method, path, headers=h, token=tok)
                self.assertEqual(status, 405, (method, path))
            for path in (f"/api/chats/{cid}/delete", f"/api/chats/{cid}/purge", "/api/projects/prj_general"):
                self.assertEqual(rt.post_json(path, {})[0], 404, path)
            self.assertEqual(rt.get_json(f"/api/chats/{cid}")[1]["chat"]["messages"], 2)

    def test_unknown_chats_and_projects(self):
        with self.rt() as rt:
            ev = rt.chat("chat_doesnotexist1", "qwen2.5:3b", "hi")
            self.assertEqual([e["code"] for e in ev], ["no_such_chat"])
            self.assertEqual(self.chat_requests(), [])
            self.assertEqual(rt.get_json("/api/chats/chat_doesnotexist1")[0], 404)
            self.assertEqual(rt.post_json("/api/chats", {"project_id": "prj_nope1234"})[0], 404)
            self.assertEqual(rt.post_json("/api/chats", {"project_id": "../etc"})[0], 400)
            self.assertEqual(rt.get_json("/api/chats?project_id=x%27%20OR%201=1")[0], 400)
            self.assertEqual(rt.get_json("/api/chats")[0], 400)

    def test_partial_reply_is_saved_with_its_status(self):
        with self.rt() as rt:
            cid = rt.new_chat()
            ev = rt.chat(cid, "dies:1b", "go")
            self.assertTrue(ev[-1]["partial"])
            msgs = rt.get_json(f"/api/chats/{cid}")[1]["messages"]
            self.assertEqual(msgs[1]["status"], "interrupted")
            self.assertTrue(msgs[1]["content"].startswith("partial"))

    def test_history_needs_the_session_credential(self):
        with self.rt() as rt:
            for path in ("/api/projects", "/api/chats?project_id=prj_general"):
                status, _, _ = rt.request("GET", path)
                self.assertEqual(status, 401, path)
            status, _, _ = rt.request("POST", "/api/projects", headers={"Content-Type": "application/json",
                                                                         "Origin": "http://evil.example"},
                                      body=b'{"name":"x"}', token=rt.policy.session_token)
            self.assertEqual(status, 403)

    def test_content_and_names_are_not_logged(self):
        with self.rt() as rt:
            rt.post_json("/api/projects", {"name": "Secret-Project-Name"})
            cid = rt.new_chat()
            rt.chat(cid, "qwen2.5:3b", "PRIVATE-HISTORY-TEXT")
            rt.get_json(f"/api/chats/{cid}")
            logs = rt.log_stream.getvalue()
        self.assertIn("history.chat_created", logs)
        self.assertNotIn("PRIVATE-HISTORY-TEXT", logs)
        self.assertNotIn("Secret-Project-Name", logs)


if __name__ == "__main__":
    unittest.main()
