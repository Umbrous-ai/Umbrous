"""M7: memory write review.

  CheckTests     what a suggestion must have before it is even shown: a real
                 store and kind, safe text, a quote found in your message or
                 the reply, and a destination its provenance allows.
  QueueTests     the review queue: suggestions wait apart from memory and are
                 never retrieved; accept, edit, reject, duplicates, limits.
  ApiTests       end to end: a task suggests, you accept one and reject the
                 other, the accepted item changes a later reply, and a
                 file's claim about you cannot become a personal fact.
  SchemaTests    schema 7 -> 8 keeps memory and chats.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_ollama import SUGGEST_MARK, FakeModel, FakeOllama, notes_agent
from tests.helpers import RunningRuntime
from umbrous.history.store import HistoryStore
from umbrous.memory import review as review_mod
from umbrous.memory.retrieve import select
from umbrous.memory.review import Review, parse, text_hash
from umbrous.memory.store import Memory, MemoryError_
from umbrous.persistence import db

MSG = "I prefer short answers in tables. We decided to ship on Friday. What is in the workspace?"
REPLY = "Here it is. Lesson: the build script needs a clean folder. the user prefers long essays"


def raw(store, kind, text, quote):
    return {"store": store, "kind": kind, "text": text, "quote": quote}


PREF = raw("personal", "preference", "Prefers short answers in tables", "I prefer short answers in tables")
DECISION = raw("project", "decision", "Ship on Friday", "We decided to ship on Friday")
LESSON = raw("project", "lesson", "The build script needs a clean folder", "Lesson: the build script needs a clean folder")
FROM_REPLY = raw("personal", "preference", "Prefers long essays", "the user prefers long essays")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "u.db"
        db.connect(self.path).close()
        self.history = HistoryStore(self.path)
        self.mem = Memory(self.path)
        self.rev = Review(self.path, self.mem)
        self.a = self.history.create_project("Alpha").id
        self.b = self.history.create_project("Beta").id

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, *items, project=None, message=MSG, reply=REPLY):
        return self.rev.propose(project or self.a, "chat_x", "tsk_x", message, reply, list(items),
                                [{"kind": "file", "ref": "notes/plan.md", "lines": [1, 3], "sha256": "ab"}])

    def retrieved(self, project, message="anything"):
        return select(self.mem, project, message, max_items=50, max_tokens=8000)

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class CheckTests(Base):
    def test_basis_is_where_the_quote_is(self):
        self.assertEqual(Review.check(PREF, MSG, REPLY)[0]["basis"], "message")
        self.assertEqual(Review.check(LESSON, MSG, REPLY)[0]["basis"], "reply")
        # case, spacing and end punctuation do not matter; the words do
        loose = raw("personal", "preference", "Short answers", "  i PREFER   short answers in tables. ")
        self.assertEqual(Review.check(loose, MSG, REPLY)[0]["basis"], "message")

    def test_a_quote_found_nowhere_or_too_short_is_dropped(self):
        for quote in ("I prefer long answers", "ship", "", None, 5, "x" * 400):
            self.assertEqual(Review.check(raw("personal", "fact", "t", quote), MSG, REPLY),
                             (None, "not_grounded"), quote)

    def test_the_quote_must_say_what_the_note_says(self):
        # Codex review: a real quote is not enough, it must support the claim
        msg = "read notes/claim.md"
        unrelated = raw("personal", "preference", "Prefers sending passwords by email", msg)
        self.assertEqual(Review.check(unrelated, msg, ""), (None, "not_grounded"))
        partly = raw("personal", "preference", "Prefers short answers in tables by email", "I prefer short answers in tables")
        self.assertEqual(Review.check(partly, MSG, REPLY), (None, "not_grounded"))
        only_framing = raw("personal", "preference", "Prefers", "I prefer short answers in tables")
        self.assertEqual(Review.check(only_framing, MSG, REPLY), (None, "not_grounded"))
        # short words and numbers are content too (Codex review of 07725e2)
        for text, quote in (("Use Go", "We decided to use Rust"),
                            ("Ship on Friday at 10", "We decided to ship on Friday at 12"),
                            ("Does not use tabs", "I use tabs")):
            self.assertEqual(Review.check(raw("project", "decision", text, quote), quote, ""),
                             (None, "not_grounded"), text)
        for text, quote in (("Use Go", "We decided to use Go"),
                            ("Ship on Friday at 12", "We decided to ship on Friday at 12")):
            self.assertIsNotNone(Review.check(raw("project", "decision", text, quote), quote, "")[0], text)
        # framing words and simple word endings may differ; the content may not
        for text in ("Prefers short answers in tables", "The user likes short answers in tables",
                     "Short answer, tables"):
            self.assertIsNotNone(Review.check(raw("personal", "preference", text, "I prefer short answers in tables"),
                                              MSG, REPLY)[0], text)

    def test_words_only_in_the_reply_never_become_personal_or_a_decision(self):
        self.assertEqual(Review.check(FROM_REPLY, MSG, REPLY), (None, "not_your_words"))
        as_decision = raw("project", "decision", "Clean folder", "Lesson: the build script needs a clean folder")
        self.assertEqual(Review.check(as_decision, MSG, REPLY), (None, "not_your_words"))
        as_state = raw("project", "state", "Clean folder", "the build script needs a clean folder")
        self.assertIsNotNone(Review.check(as_state, MSG, REPLY)[0])

    def test_store_kind_text_and_secrets(self):
        self.assertEqual(Review.check(raw("team", "fact", "t", "I prefer short answers"), MSG, REPLY)[1], "bad_store")
        self.assertEqual(Review.check(raw("personal", "decision", "t", "I prefer short answers"), MSG, REPLY)[1],
                         "bad_kind")
        self.assertEqual(Review.check(raw("personal", "fact", "", "I prefer short answers"), MSG, REPLY)[1],
                         "bad_text")
        key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
        self.assertEqual(Review.check(raw("personal", "fact", "My key is " + key, "I prefer short answers"),
                                      MSG, REPLY)[1], "looks_secret")
        self.assertEqual(Review.check(raw("personal", "fact", "A key", "my key " + key), "my key " + key, "")[1],
                         "looks_secret")

    def test_parse_reads_only_the_json_asked_for(self):
        self.assertEqual(parse('```json\n{"candidates": [{"a": 1}, 2]}\n```'), [{"a": 1}])
        self.assertEqual(parse('Sure! {"candidates": []} done'), [])
        for junk in ("", "no json here", '{"candidates": "x"}', "[1, 2]", "{broken"):
            self.assertEqual(parse(junk), [], junk)
        self.assertEqual(len(parse(json.dumps({"candidates": [{}] * 50}))), review_mod.MAX_RAW)


class QueueTests(Base):
    def test_suggestions_wait_apart_from_memory_and_are_never_retrieved(self):
        queued, dropped = self.propose(PREF, DECISION, LESSON, FROM_REPLY)
        self.assertEqual([c.status for c in queued], ["pending"] * 3)
        self.assertEqual(dropped, {"not_your_words": 1})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM personal_memory")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM project_memory")[0][0], 0)
        self.assertEqual(self.retrieved(self.a, "short answers ship Friday build").items, [])
        pref = next(c for c in queued if c.store == "personal")
        self.assertEqual(pref.scope, [self.a])            # only the project it came from, until you widen it
        self.assertEqual((pref.basis, pref.task_id, pref.chat_id), ("message", "tsk_x", "chat_x"))
        self.assertEqual(pref.evidence, [{"kind": "file", "ref": "notes/plan.md", "lines": [1, 3]}])

    def test_accept_writes_to_the_intended_store_only(self):
        (pref, dec, lesson), _ = self.propose(PREF, DECISION, LESSON)
        cand, item = self.rev.accept(dec.id)
        self.assertEqual((item.store, item.kind, item.scope, item.approval), ("project", "decision", self.a, "approved"))
        self.assertEqual(item.source, {"type": "task", "task_id": "tsk_x", "chat_id": "chat_x",
                                       "candidate_id": dec.id, "basis": "message", "edited": False,
                                       "evidence": dec.evidence})
        self.assertEqual((cand.status, cand.memory_id, cand.text, cand.quote), ("accepted", item.id, "", ""))
        self.assertEqual([m.id for m in self.mem.project.list(self.a)], [item.id])
        self.assertEqual(self.mem.project.list(self.b), [])
        self.assertEqual(self.mem.personal.list(), [])
        _, p = self.rev.accept(pref.id)
        self.assertEqual((p.store, p.scope), ("personal", [self.a]))
        self.assertEqual([m.id for m in self.retrieved(self.a, "ship short").items], [item.id, p.id])
        self.assertEqual(self.retrieved(self.b, "ship short").items, [])      # neither reaches another project
        self.assertEqual(self.rev.list(self.a), [lesson])

    def test_edit_then_accept(self):
        (pref, lesson), _ = self.propose(PREF, LESSON)
        edited = self.rev.edit(pref.id, text="Prefers tables for comparisons", scope="all")
        self.assertTrue(edited.edited)
        self.assertEqual((edited.text, edited.scope, edited.status), ("Prefers tables for comparisons", "all", "pending"))
        _, item = self.rev.accept(pref.id, kind="fact")
        self.assertEqual((item.text, item.kind, item.scope, item.source["edited"]),
                         ("Prefers tables for comparisons", "fact", "all", True))
        self.assertEqual(self.retrieved(self.b, "tables").items[0].id, item.id)   # widened by you
        # a lesson from the reply may be edited, but not made personal or a decision
        for bad in ({"store": "personal"}, {"kind": "decision"}):
            with self.assertRaises(MemoryError_) as e:
                self.rev.edit(lesson.id, **bad)
            self.assertEqual(e.exception.code, "not_your_words")
        with self.assertRaises(MemoryError_) as e:
            self.rev.edit(lesson.id, scope=self.b)
        self.assertEqual(e.exception.code, "bad_scope")
        with self.assertRaises(MemoryError_) as e:
            self.rev.accept(lesson.id, text="token sk-abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertEqual(e.exception.code, "looks_secret")
        self.assertEqual(self.rev.get(lesson.id).status, "pending")
        _, item = self.rev.accept(lesson.id, kind="state")
        self.assertEqual((item.store, item.kind), ("project", "state"))

    def test_a_decision_can_move_to_personal_because_it_is_your_words(self):
        (dec,), _ = self.propose(DECISION)
        _, item = self.rev.accept(dec.id, store="personal")
        self.assertEqual((item.store, item.kind, item.scope), ("personal", "fact", [self.a]))
        self.assertEqual(self.mem.project.list(self.a), [])

    def test_reject_keeps_no_text_and_is_never_retrieved_or_offered_again(self):
        (pref,), _ = self.propose(PREF)
        cand = self.rev.reject(pref.id)
        self.assertEqual((cand.status, cand.text, cand.quote), ("rejected", "", ""))
        self.assertEqual(self.rows(f"SELECT text, quote, text_hash FROM memory_candidates WHERE id = '{pref.id}'"),
                         [("", "", text_hash(PREF["text"]))])
        self.assertEqual(self.retrieved(self.a, "short answers tables").items, [])
        self.assertEqual(self.propose(PREF), ([], {"rejected_before": 1}))
        # in another project it is a new suggestion (rejection is per project)
        self.assertEqual(len(self.propose(PREF, project=self.b)[0]), 1)

    def test_duplicates(self):
        self.mem.project.add(self.a, "Ship on Friday.", "decision")
        queued, dropped = self.propose(DECISION, PREF, PREF)            # in memory; and twice in one batch
        self.assertEqual(([c.store for c in queued], dropped), (["personal"], {"duplicate": 2}))
        self.assertEqual(self.propose(PREF), ([], {"duplicate": 1}))      # already waiting
        # you add the same thing yourself while it waits: accepting it is refused, nothing is written twice
        self.mem.personal.add("prefers short answers in tables", "preference")
        with self.assertRaises(MemoryError_) as e:
            self.rev.accept(queued[0].id)
        self.assertEqual(e.exception.code, "duplicate")
        self.assertEqual((self.rev.get(queued[0].id).status, len(self.mem.personal.list())), ("pending", 1))
        # an edit that makes it different can then be accepted
        self.rev.accept(queued[0].id, text="Prefers tables over lists")
        self.assertEqual(len(self.mem.personal.list()), 2)

    def test_duplicates_follow_personal_scope(self):
        # Codex review: an item scoped to A is not a duplicate in B, which cannot retrieve it
        (in_a,), _ = self.propose(PREF)
        _, item_a = self.rev.accept(in_a.id)
        self.assertEqual(item_a.scope, [self.a])
        self.assertEqual(self.propose(PREF), ([], {"duplicate": 1}))     # A already has it
        (in_b,), dropped = self.propose(PREF, project=self.b)
        self.assertEqual(dropped, {})
        # widening B's to every project would overlap A's: refused, nothing written
        self.rev.edit(in_b.id, scope="all")
        with self.assertRaises(MemoryError_) as e:
            self.rev.accept(in_b.id)
        self.assertEqual(e.exception.code, "duplicate")
        self.assertEqual(len(self.mem.personal.list()), 1)
        # kept to B, it is accepted, and each project retrieves exactly one
        self.rev.edit(in_b.id, scope=[self.b])
        _, item_b = self.rev.accept(in_b.id)
        self.assertEqual(item_b.scope, [self.b])
        for proj, item in ((self.a, item_a), (self.b, item_b)):
            self.assertEqual([m.id for m in self.retrieved(proj, "short answers tables").items], [item.id])
        # a personal item for every project is a duplicate everywhere
        self.mem.personal.add("Prefers dark mode", "preference")
        dark = raw("personal", "preference", "Prefers dark mode", "I prefer dark mode")
        for proj in (self.a, self.b):
            self.assertEqual(self.propose(dark, project=proj, message="I prefer dark mode"), ([], {"duplicate": 1}))
        # a project note in A blocks moving the same words to personal for A, not a personal note kept to B
        (dec,), _ = self.propose(DECISION, project=self.b)
        self.mem.project.add(self.a, "Ship on Friday", "decision")
        self.rev.edit(dec.id, store="personal", scope="all")
        with self.assertRaises(MemoryError_) as e:
            self.rev.accept(dec.id)
        self.assertEqual(e.exception.code, "duplicate")
        _, item = self.rev.accept(dec.id, scope=[self.b])
        self.assertEqual((item.store, item.scope), ("personal", [self.b]))

    def test_decided_once(self):
        (a, b), _ = self.propose(PREF, DECISION)
        self.rev.accept(a.id)
        self.rev.reject(b.id)
        for fn in (self.rev.accept, self.rev.reject, self.rev.edit):
            for cid in (a.id, b.id):
                with self.assertRaises(MemoryError_) as e:
                    fn(cid)
                self.assertEqual(e.exception.code, "already_decided")
        with self.assertRaises(MemoryError_) as e:
            self.rev.accept("cand_0000000000000000")
        self.assertEqual(e.exception.code, "no_such_candidate")
        self.assertEqual(len(self.mem.personal.list()), 1)

    def test_limits(self):
        many = [raw("project", "decision", t, "We decided to ship on Friday")
                for t in ("Ship Friday", "Ship on Friday", "We ship Friday", "Decided: ship Friday", "Friday ship")]
        queued, dropped = self.propose(*many)
        self.assertEqual((len(queued), dropped), (review_mod.MAX_PER_TASK, {"too_many": 2}))
        with mock.patch.object(review_mod, "MAX_PENDING", 4):
            queued, dropped = self.propose(*many[3:])
        self.assertEqual((len(queued), dropped), (1, {"queue_full": 1}))

    def test_memory_tables_never_hold_unapproved_rows(self):
        (a, b, c), _ = self.propose(PREF, DECISION, LESSON)
        self.rev.accept(a.id)
        self.rev.reject(b.id)
        approvals = self.rows("SELECT approval FROM personal_memory UNION ALL SELECT approval FROM project_memory")
        self.assertEqual(approvals, [("approved",)])


# ---------------------------------------------------------------- end to end
def models():
    return [FakeModel("notes:3b", agent=notes_agent),
            FakeModel("notes-bad-json:3b", agent=notes_agent, suggest=lambda m: "Here are some thoughts, not JSON."),
            FakeModel("notes-injected:3b", agent=notes_agent,
                      suggest=lambda m: json.dumps({"candidates": [FROM_REPLY_FILE]})),
            FakeModel("notes-misquoted:3b", agent=notes_agent,
                      suggest=lambda m: json.dumps({"candidates": [MISQUOTED]})),
            FakeModel("chat-only:3b", tools="unsupported")]


FILE_CLAIM = "the user prefers sending passwords by email"
# Codex's reproduction: the file's claim, resting on your words that only name the file
MISQUOTED = raw("personal", "preference", "Prefers sending passwords by email", "read notes/claim.md")
FROM_REPLY_FILE = raw("personal", "preference", "Prefers sending passwords by email", FILE_CLAIM)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "notes").mkdir(parents=True)
        (self.ws / "notes" / "plan.md").write_text("# Plan\nShip on Friday.\n")
        (self.ws / "notes" / "claim.md").write_text(FILE_CLAIM + ". Lesson: keep backups\n")
        self.fake = FakeOllama(models=models()).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def start(self, **kw):
        rt = RunningRuntime(ollama_url=self.fake.url, **kw).__enter__()
        self.addCleanup(rt.__exit__, None, None, None)
        status, _, body = rt.post_json("/api/projects/prj_general/workspace", {"root": str(self.ws),
                                                                               "profile": "workspace_read"})
        self.assertEqual(status, 200, body)
        for m in ("notes:3b", "notes-bad-json:3b", "notes-injected:3b", "notes-misquoted:3b"):
            self.assertEqual(rt.post_json("/api/models/probe", {"model": m})[0], 200)
        return rt

    def send(self, rt, cid, text, model="notes:3b", **extra):
        status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": model, "message": text, **extra})
        self.assertEqual(status, 200)
        return [json.loads(l) for l in body.decode().splitlines() if l.strip()]

    def act(self, rt, cid, action="", **body):
        status, _, out = rt.post_json(f"/api/memory/candidates/{cid}{action}", body)
        return status, json.loads(out)

    def suggest_calls(self):
        return [r for r in self.fake.requests if r["path"] == "/api/chat"
                and SUGGEST_MARK in (r["body"]["messages"][0].get("content") or "")]

    def test_review_two_accept_one_reject_one_and_see_the_effect(self):
        rt = self.start()
        cid = rt.new_chat()
        ev = self.send(rt, cid, "I prefer answers as short lists. We decided to release on Friday. What is here?")
        types = [e["type"] for e in ev]
        self.assertLess(types.index("suggesting"), types.index("done"))
        done = ev[-1]
        cands = done["candidates"]
        self.assertEqual([(c["store"], c["kind"], c["basis"], c["status"]) for c in cands],
                         [("personal", "preference", "message", "pending"), ("project", "decision", "message", "pending")])
        # the report names them by id, store and kind; never their text
        self.assertEqual([c["id"] for c in done["report"]["memory_candidates"]["queued"]], [c["id"] for c in cands])
        self.assertNotIn("short lists", json.dumps(done["report"]))
        status, view = rt.get_json("/api/memory?project_id=prj_general")
        self.assertEqual(([c["id"] for c in view["candidates"]], view["personal"], view["project"]),
                         ([c["id"] for c in cands], [], []))
        pref, dec = cands
        status, out = self.act(rt, dec["id"], "/accept")
        self.assertEqual(status, 200, out)
        self.assertEqual((out["item"]["store"], out["item"]["kind"], out["item"]["source"]["task_id"]),
                         ("project", "decision", done["task_id"]))
        status, out = self.act(rt, pref["id"], "/reject")
        self.assertEqual((status, out["candidate"]["status"], out["candidate"]["text"]), (200, "rejected", ""))
        status, view = rt.get_json("/api/memory?project_id=prj_general")
        self.assertEqual((view["candidates"], view["personal"], [m["text"] for m in view["project"]]),
                         ([], [], ["Decided: release on Friday"]))
        # a later reply, in a new chat, is given the accepted item and not the rejected one
        ev = self.send(rt, rt.new_chat(), "When do we release?")
        answer = "".join(e["text"] for e in ev if e["type"] == "delta")
        self.assertIn("Decided: release on Friday", answer)
        self.assertNotIn("short lists", json.dumps(self.fake.requests[-2]["body"]))
        self.assertEqual(ev[-1]["memory_ids"], [out_id for out_id in [view["project"][0]["id"]]])
        self.assertEqual(ev[-1]["candidates"], [])     # nothing new to suggest
        # the log has ids and counts, never the suggested text
        self.assertNotIn("short lists", rt.log_stream.getvalue())
        self.assertNotIn("release on Friday", rt.log_stream.getvalue())

    def test_a_files_claim_about_you_cannot_become_a_personal_fact(self):
        rt = self.start()
        ev = self.send(rt, rt.new_chat(), "read notes/claim.md", model="notes-injected:3b")
        answer = "".join(e["text"] for e in ev if e["type"] == "delta")
        self.assertIn(FILE_CLAIM, answer)                 # the model repeated the file
        self.assertEqual((ev[-1]["candidates"], ev[-1]["candidates_dropped"]), ([], {"not_your_words": 1}))
        # nor by quoting your own unrelated words
        ev = self.send(rt, rt.new_chat(), "read notes/claim.md", model="notes-misquoted:3b")
        self.assertEqual((ev[-1]["candidates"], ev[-1]["candidates_dropped"]), ([], {"not_grounded": 1}))
        # the default suggester offers the file's lesson, as project state of the reply, nothing personal
        ev = self.send(rt, rt.new_chat(), "read notes/claim.md")
        self.assertEqual([(c["store"], c["kind"], c["basis"]) for c in ev[-1]["candidates"]],
                         [("project", "lesson", "reply")])
        self.assertEqual(ev[-1]["candidates_dropped"], {"not_your_words": 1})
        status, out = self.act(rt, ev[-1]["candidates"][0]["id"], "/accept", store="personal")
        self.assertEqual((status, out["error"]), (400, "not_your_words"))
        self.assertEqual(rt.get_json("/api/memory?project_id=prj_general")[1]["personal"], [])

    def test_when_nothing_is_suggested(self):
        rt = self.start()
        cid = rt.new_chat()
        ev = self.send(rt, cid, "I prefer tea to coffee today.", use_memory=False)       # memory off: none
        self.assertNotIn("suggesting", [e["type"] for e in ev])
        ev = self.send(rt, cid, "I prefer tea to coffee today.", model="chat-only:3b")    # not a task: none
        self.assertEqual(ev[-1]["type"], "done")
        self.assertNotIn("suggesting", [e["type"] for e in ev])
        self.assertEqual(self.suggest_calls(), [])
        ev = self.send(rt, cid, "What is here?")                                         # nothing worth keeping
        self.assertEqual((ev[-1]["candidates"], ev[-1]["candidates_dropped"]), ([], {}))
        self.assertEqual(len(self.suggest_calls()), 1)

    def test_switched_off_in_settings(self):
        rt = self.start(memory_suggestions=False)
        ev = self.send(rt, rt.new_chat(), "I prefer tea to coffee today.")
        self.assertEqual((ev[-1]["type"], ev[-1]["candidates"]), ("done", []))
        self.assertEqual(self.suggest_calls(), [])

    def test_a_failed_suggestion_never_fails_the_reply(self):
        rt = self.start()
        cid = rt.new_chat()
        ev = self.send(rt, cid, "I prefer tea to coffee today.", model="notes-bad-json:3b")
        self.assertEqual(ev[-1]["type"], "done")
        self.assertEqual((ev[-1]["candidates"], ev[-1]["candidates_dropped"]), ([], {"unreadable": 1}))
        status, chat = rt.get_json(f"/api/chats/{cid}")
        self.assertEqual([m["status"] for m in chat["messages"]], ["complete", "complete"])
        with mock.patch("umbrous.memory.review.Review.propose", side_effect=sqlite3.OperationalError("locked")):
            ev = self.send(rt, cid, "I prefer tea to coffee today.")
        self.assertEqual((ev[-1]["type"], ev[-1]["candidates_dropped"]), ("done", {"error": 1}))
        self.assertEqual(len(rt.get_json(f"/api/chats/{cid}")[1]["messages"]), 4)

    def test_page_gone_while_suggesting_keeps_the_reply_once(self):
        from umbrous.runloop.chat import run_chat
        rt = self.start()
        r = rt.runtime
        cid = rt.new_chat()
        gen = run_chat(r.config, r.router, r.history, r.chat_locks, r.runs, cid, "notes:3b",
                       "I prefer tea to coffee today.", gate=r.gate, tasks=r.tasks, memory=r.memory, review=r.review)
        task_id = None
        for ev in gen:
            task_id = task_id or ev.get("task_id")
            if ev["type"] == "suggesting":
                break
        gen.close()                     # the page went away after the reply, while suggestions ran
        msgs = rt.get_json(f"/api/chats/{cid}")[1]["messages"]
        self.assertEqual([m["status"] for m in msgs], ["complete", "complete"])    # stored once, as complete
        self.assertEqual(rt.get_json(f"/api/tasks/{task_id}")[1]["task"]["status"], "completed")

    def test_api_errors_and_listing(self):
        rt = self.start()
        ev = self.send(rt, rt.new_chat(), "I prefer tea to coffee today. We decided to use SQLite.")
        a, b = ev[-1]["candidates"]
        self.assertEqual(self.act(rt, a["id"], "", text="Prefers green tea")[1]["candidate"]["edited"], True)
        self.assertEqual(self.act(rt, a["id"], "", kind="decision")[0], 400)
        self.assertEqual(self.act(rt, "cand_0000000000000000", "/accept")[0], 404)
        self.assertEqual(self.act(rt, a["id"], "/accept")[0], 200)
        self.assertEqual(self.act(rt, a["id"], "/reject")[1]["error"], "already_decided")
        self.assertEqual(rt.get_json("/api/memory/candidates?status=bad")[0], 400)
        status, out = rt.get_json("/api/memory/candidates?project_id=prj_general&status=all")
        self.assertEqual([(c["id"], c["status"]) for c in out["candidates"]], [(a["id"], "accepted"), (b["id"], "pending")])
        status, out = rt.get_json("/api/memory/candidates?project_id=prj_general")
        self.assertEqual([c["id"] for c in out["candidates"]], [b["id"]])
        task = rt.get_json(f"/api/tasks/{ev[-1]['task_id']}")[1]
        self.assertIn("memory.suggested", [e["kind"] for e in task["events"]])


class CliTests(unittest.TestCase):
    def test_review_from_the_command_line(self):
        from tests.test_cli import run
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertEqual(run("memory", "suggestions", home=home).returncode, 0)
            path = home / "umbrous.db"
            mem = Memory(path)
            rev = Review(path, mem)
            (pref, dec, lesson), _ = rev.propose("prj_general", "chat_x", "tsk_x", MSG, REPLY, [PREF, DECISION, LESSON])
            out = run("memory", "suggestions", home=home)
            self.assertIn(pref.id, out.stdout)
            self.assertIn('from your message: "I prefer short answers in tables"', out.stdout)
            out = run("memory", "accept", pref.id, "Prefers tables", "--scope", "all", home=home)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("personal preference [all projects] Prefers tables", " ".join(out.stdout.split()))
            out = run("memory", "reject", dec.id, home=home)
            self.assertIn("will not be suggested again", out.stdout)
            out = run("memory", "accept", lesson.id, "--to", "personal", home=home)
            self.assertEqual(out.returncode, 1)
            self.assertIn("cannot become a personal fact", out.stderr)
            out = run("memory", "suggestions", "--all", home=home)
            self.assertIn("accepted", out.stdout)
            self.assertIn("rejected", out.stdout)
            self.assertNotIn("Ship on Friday", out.stdout)       # a rejected suggestion keeps no text
            self.assertEqual([m.text for m in mem.personal.list()], ["Prefers tables"])


class SchemaTests(unittest.TestCase):
    def test_schema_8_upgrade_keeps_version_7_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path, isolation_level=None)
            with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:7]), mock.patch.object(db, "SCHEMA_VERSION", 7):
                db.migrate(conn)
            conn.execute("INSERT INTO personal_memory VALUES ('mem_0000000000000001', 'fact', 'kept', 'all', "
                         "'{\"type\": \"manual\"}', 'approved', 'now', 'now')")
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT text FROM personal_memory").fetchall(), [("kept",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
