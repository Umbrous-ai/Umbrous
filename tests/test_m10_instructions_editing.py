"""M10: project instructions (AGENTS.md / CLAUDE.md) and better editing.

  InstructionTests  which files are read, in what order, how much of them, and
                    when not (unsafe files, a profile that asks before reads).
  EditTests         edit_file with several replacements (all or none),
                    replace_all, argument checks, and the parse check after a
                    write or an edit.
  LoopTests         a real runtime: the instructions reach the model's system
                    message and the start event says which files were used; a
                    broken edit is reported to the model and in the progress record.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama, scripted
from tests.helpers import RunningRuntime, project_fixture_db
from tests.test_m9_coding import CALC, make_project
from umbrous.context import instructions
from umbrous.gate.access import AccessStore
from umbrous.gate.gate import Call, Gate, ToolContext, ollama_tools
from umbrous.receipts.model import new_task_id
from umbrous.receipts.store import ReceiptStore
from umbrous.tools import check
from umbrous.tools.run import CommandRegistry


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_without_files(self):
        got = instructions.load(str(self.root))
        self.assertEqual((got.files, got.block()), ([], ""))

    def test_agents_then_claude_and_a_copy_is_read_once(self):
        (self.root / "AGENTS.md").write_text("Run tests with make test.\n")
        (self.root / "CLAUDE.md").write_text("Use tabs.\n")
        got = instructions.load(str(self.root))
        self.assertEqual(got.files, ["AGENTS.md", "CLAUDE.md"])
        self.assertLess(got.text.index("make test"), got.text.index("Use tabs"))
        self.assertIn("cannot widen your access", got.text)
        (self.root / "CLAUDE.md").write_text("Run tests with make test.\n")
        self.assertEqual(instructions.load(str(self.root)).files, ["AGENTS.md"])

    def test_long_files_are_cut_and_say_so(self):
        (self.root / "AGENTS.md").write_text("Use small functions.\n" * 4_000)
        got = instructions.load(str(self.root), max_chars=1_000)
        self.assertEqual(got.truncated, ["AGENTS.md"])
        self.assertLess(len(got.text), 2_000)
        self.assertIn("left out", got.text)

    def test_a_symlink_is_not_followed(self):
        outside = Path(self.tmp.name) / "secret.md"
        outside.write_text("SECRET\n")
        os.symlink(outside, self.root / "AGENTS.md")
        got = instructions.load(str(self.root))
        self.assertEqual(got.files, [])
        self.assertEqual(got.skipped, [("AGENTS.md", "symlink")])
        self.assertNotIn("SECRET", got.text)

    def test_a_profile_that_asks_before_reads_gets_nothing_loaded(self):
        (self.root / "AGENTS.md").write_text("Run tests with make test.\n")
        got = instructions.load(str(self.root), decide=lambda name: "ask")
        self.assertEqual((got.files, got.skipped), ([], [("AGENTS.md", "needs_approval")]))
        self.assertEqual(instructions.load(str(self.root), decide=lambda name: "block").skipped, [])


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ws = base / "ws"
        self.ws.mkdir()
        (self.ws / "calc.py").write_text(CALC)
        self.home = base / "home"
        self.home.mkdir()
        project_fixture_db(self.home / "u.db").close()
        self.access = AccessStore(self.home / "u.db", self.home)
        self.gate = Gate(self.access, ReceiptStore(self.home / "u.db"))
        self.access.attach("prj_general", str(self.ws), "workspace_code")
        self.task = new_task_id()
        self.commands = CommandRegistry()
        self.ctx = ToolContext(intent=lambda d: None, commands=self.commands, command_timeout_s=10)

    def tearDown(self):
        self.commands.end_task(self.task)
        self.tmp.cleanup()

    def call(self, tool, **args):
        return self.gate.run(self.task, "prj_general", None, Call(tool, args), cancelled=lambda: False, ctx=self.ctx)

    def test_several_edits_in_one_call(self):
        out = self.call("edit_file", path="calc.py", edits=[
            {"old_text": "def add(a, b):", "new_text": "def add(a: int, b: int) -> int:"},
            {"old_text": "# TODO: multiply\n", "new_text": "def multiply(a, b):\n    return a * b\n"}])
        self.assertEqual((out.decision, out.observed["op"]), ("allowed", "edited"), out.message)
        text = (self.ws / "calc.py").read_text()
        self.assertIn("def add(a: int, b: int) -> int:", text)
        self.assertIn("return a * b", text)
        again = self.call("edit_file", path="calc.py", edits=[
            {"old_text": "def add(a, b):", "new_text": "def add(a: int, b: int) -> int:"},
            {"old_text": "# TODO: multiply\n", "new_text": "def multiply(a, b):\n    return a * b\n"}])
        self.assertEqual(again.observed["op"], "already_applied")       # repeated: not made twice

    def test_several_edits_are_all_or_none(self):
        out = self.call("edit_file", path="calc.py", edits=[
            {"old_text": "def add(a, b):", "new_text": "def plus(a, b):"},
            {"old_text": "not in the file", "new_text": "x"}])
        self.assertEqual((out.decision, out.code), ("failed", "no_match"))
        self.assertIn("Edit 2 of 2", out.message)
        self.assertEqual((self.ws / "calc.py").read_text(), CALC)        # the first one was not written either

    def test_edits_may_arrive_as_a_json_string(self):
        edits = json.dumps([{"old_text": "# TODO: multiply", "new_text": "# later"}])
        out = self.call("edit_file", path="calc.py", edits=edits)
        self.assertEqual(out.decision, "allowed", out.message)

    def test_replace_all(self):
        (self.ws / "names.py").write_text("x = 1\nprint(x)\nx = x + 1\n")
        out = self.call("edit_file", path="names.py", old_text="x", new_text="count")
        self.assertEqual(out.code, "ambiguous")
        self.assertIn("replace_all", out.message)
        out = self.call("edit_file", path="names.py", old_text="x", new_text="count", replace_all=True)
        self.assertEqual(out.decision, "allowed", out.message)
        self.assertEqual((self.ws / "names.py").read_text(), "count = 1\nprint(count)\ncount = count + 1\n")
        again = self.call("edit_file", path="names.py", old_text="x", new_text="count", replace_all=True)
        self.assertEqual(again.observed["op"], "already_applied")

    def test_argument_checks(self):
        for args, words in (
                ({"old_text": "a"}, "needs old_text and new_text"),
                ({"old_text": "a", "new_text": "b", "edits": [{"old_text": "c", "new_text": "d"}]}, "not both"),
                ({"edits": []}, "1 to"),
                ({"edits": [{"old_text": "a"}]}, "exactly old_text and new_text"),
                ({"edits": [{"old_text": "", "new_text": "b"}]}, "must not be empty"),
                ({"edits": [{"old_text": "a", "new_text": "b\x00"}]}, "NUL"),
                ({"edits": "not json"}, "must be a list")):
            out = self.call("edit_file", path="calc.py", **args)
            self.assertEqual((out.decision, out.code), ("denied", "malformed"), args)
            self.assertIn(words, out.message, args)
        self.assertEqual((self.ws / "calc.py").read_text(), CALC)

    def test_the_offered_schema_describes_edits(self):
        spec = next(t for t in ollama_tools(["edit_file"]))["function"]["parameters"]
        self.assertEqual(spec["required"], ["path"])
        self.assertEqual(spec["properties"]["edits"]["type"], "array")
        self.assertEqual(spec["properties"]["edits"]["items"]["required"], ["old_text", "new_text"])

    def test_a_broken_python_edit_is_written_and_reported(self):
        out = self.call("edit_file", path="calc.py", old_text="    return a + b", new_text="    return a +")
        self.assertEqual((out.decision, out.observed["op"]), ("allowed", "edited"))
        self.assertFalse(out.observed["check"]["ok"])
        self.assertIn("does not parse", out.model_text)
        self.assertIn("line 2", out.model_text)
        self.assertIn("does not parse", out.summary)
        good = self.call("edit_file", path="calc.py", old_text="    return a +", new_text="    return a + b")
        self.assertTrue(good.observed["check"]["ok"])
        self.assertNotIn("does not parse", good.model_text)

    def test_json_and_unknown_kinds(self):
        out = self.call("write_file", path="conf.json", content='{"a": 1,}')
        self.assertIn("not valid JSON", out.model_text)
        out = self.call("write_file", path="notes.txt", content="anything {")
        self.assertIsNone(out.observed["check"])

    def test_the_check_never_runs_the_code(self):
        marker = Path(self.tmp.name) / "ran"
        self.assertIsNone(check.syntax("x.py", f"open({str(marker)!r}, 'w').write('1')\n")["message"])
        self.assertFalse(marker.exists())


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = make_project(Path(self.tmp.name))
        (self.proj / "AGENTS.md").write_text("Always run tests with: python3 -m unittest -q\n")
        self.seen = []

        def answer(messages):
            self.seen.append(messages)
            return "done"
        broken = [("read_file", {"path": "calc.py"}),
                  ("edit_file", {"path": "calc.py", "old_text": "    return a + b", "new_text": "    return a +"})]
        self.fake = FakeOllama(models=[
            FakeModel("reader:3b", agent=scripted(final=answer)),
            FakeModel("breaker:3b", agent=scripted(*broken, ("list_files", {}), final=answer))]).start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def run_task(self, model, profile):
        with RunningRuntime(ollama_url=self.fake.url) as rt:
            self.assertEqual(rt.post_json("/api/projects/prj_general/workspace",
                                          {"root": str(self.proj), "profile": profile})[0], 200)
            self.assertEqual(rt.post_json("/api/models/probe", {"model": model})[0], 200)
            cid = rt.new_chat()
            status, _, body = rt.post_json("/api/chat", {"chat_id": cid, "model": model, "message": "Go."})
            self.assertEqual(status, 200)
            return [json.loads(x) for x in body.decode().splitlines() if x.strip()]

    def test_instructions_reach_the_model_and_are_reported(self):
        ev = self.run_task("reader:3b", "workspace_code")
        self.assertEqual(ev[-1]["type"], "done", ev[-3:])
        self.assertEqual(ev[0]["tools"]["instructions"]["files"], ["AGENTS.md"])
        system = self.seen[-1][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("python3 -m unittest -q", system["content"])

    def test_not_loaded_when_reads_need_approval(self):
        ev = self.run_task("reader:3b", "ask_before_read")
        info = ev[0]["tools"]["instructions"]
        self.assertEqual(info["files"], [])
        self.assertEqual(info["skipped"], [{"file": "AGENTS.md", "code": "needs_approval"}])

    def test_a_broken_edit_reaches_the_progress_record(self):
        ev = self.run_task("breaker:3b", "workspace_code")
        self.assertEqual(ev[-1]["type"], "done", ev[-3:])
        edit = next(e for e in ev if e["type"] == "tool_result" and e["tool"] == "edit_file")
        self.assertFalse(edit["observed"]["check"]["ok"])
        last = self.seen[-1]
        record = [m for m in last if m["role"] == "system" and m["content"].startswith("Runtime progress record.")]
        self.assertTrue(record)
        self.assertIn('"files_not_parsing":["calc.py"]', record[-1]["content"])


if __name__ == "__main__":
    unittest.main()
