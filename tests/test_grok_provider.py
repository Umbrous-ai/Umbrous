"""Grok provider boundaries; no account or model traffic needed."""
import json
import os
import queue
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from umbrous.model.base import CancelToken, ChatDelta, ChatToolCalls, ProviderError
from umbrous.model.grok import BUILT_IN_TOOLS, Conversation, GrokAdapter, GrokSignInRequired, command

TOOL = {"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
MODELS = "You are logged in with grok.com.\n\nDefault model: grok-4.7\n\nAvailable models:\n  * grok-4.7 (default)\n  - grok-4.6\n"


def turn(reply, calls=(), session="s1", tools=()):
    body = json.dumps({"reply": reply, "calls": [{"name": n, "arguments": json.dumps(a)} for n, a in calls]})
    return [{"type": "available_commands", "tools": list(tools), "commands": []},
            *({"type": "text", "data": body[i:i + 7]} for i in range(0, len(body), 7)),
            {"type": "usage", "usage": {"input_tokens": 400, "cache_read_input_tokens": 6000}},
            {"type": "end", "stopReason": "end_turn", "sessionId": session,
             "usage": {"input_tokens": 400, "cache_read_input_tokens": 6000, "output_tokens": 30}}]


class GrokTests(unittest.TestCase):
    def adapter(self, *turns, models=MODELS, resume=True):
        script, steps = list(turns), []
        def step(conv, argv):
            steps.append((conv, argv, open(argv[argv.index("--prompt-file") + 1]).read()))
            q = queue.Queue()
            for ev in script.pop(0):
                q.put(ev)
            q.put(None)
            return q
        runner = lambda *a, **k: SimpleNamespace(stdout=models, stderr="")
        return GrokAdapter(Path("/tmp"), step=step, binary="/bin/grok", runner=runner, resume=resume), steps

    def test_every_tool_denied_and_no_api_key(self):
        argv = command("/bin/grok", "grok-4.7", None, "policy", [TOOL], "/tmp/request.txt", None)
        denied = argv[argv.index("--disallowed-tools") + 1].split(",")
        self.assertTrue({"run_terminal_command", "read_file", "write", "search_replace"} <= set(denied))
        for flag in ("--no-subagents", "--disable-web-search", "--json-schema", "--system-prompt-override"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertNotIn("--resume", argv)
        from umbrous.model.grok import _environment
        with unittest.mock.patch.dict("os.environ", {"XAI_API_KEY": "x", "UMBROUS_TOKEN": "t"}):
            env = _environment()
        self.assertNotIn("XAI_API_KEY", env)

    def test_catalog_and_sign_in(self):
        a, _ = self.adapter()
        self.assertEqual([m.name for m in a.list_models()], ["grok/grok-4.7", "grok/grok-4.6"])
        signed_out, _ = self.adapter(models="You are not authenticated.\n")
        self.assertFalse(signed_out.status()["authenticated"])
        with self.assertRaises(GrokSignInRequired):
            list(signed_out.chat_stream("grok/grok-4.7", [{"role": "user", "content": "hi"}]))

    def test_a_task_resumes_one_session_and_sends_only_new_messages(self):
        a, steps = self.adapter(turn("Reading.", [("read_file", {"path": "x"})]), turn("x says hi."))
        token = CancelToken()
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        out = list(a.chat_stream("grok/grok-4.7", first, tools=[TOOL], cancel=token))
        self.assertEqual("".join(p.text for p in out if isinstance(p, ChatDelta)), "Reading.")
        self.assertIn(ChatToolCalls((("read_file", {"path": "x"}),)), out)
        self.assertEqual(out[-1].prompt_tokens, 6400)
        answer = {"role": "assistant", "content": "Reading.",
                  "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        result = {"role": "tool", "content": "UNTRUSTED data", "tool_name": "read_file"}
        record = {"role": "system", "content": "Runtime progress record. {}"}
        out = list(a.chat_stream("grok/grok-4.7", first + [answer, result, record], tools=[TOOL], cancel=token))
        self.assertEqual("".join(p.text for p in out if isinstance(p, ChatDelta)), "x says hi.")
        (conv1, argv1, sent1), (conv2, argv2, sent2) = steps
        self.assertIs(conv1, conv2)                                          # one session for the task
        self.assertNotIn("--resume", argv1)
        self.assertEqual(argv2[argv2.index("--resume") + 1], "s1")
        self.assertEqual(json.loads(sent1.split("\n", 1)[1]), {"transcript": first[1:], "available_tools": [TOOL]})
        self.assertEqual(json.loads(sent2.split("\n", 1)[1]), {"transcript_continues": [result, record]})
        self.assertTrue(argv1[argv1.index("--system-prompt-override") + 1].endswith("policy"))
        self.assertTrue(conv1.closed)                                        # a final answer ends it
        self.assertFalse(os.path.exists(conv1.directory))

    def test_reported_tools_or_tool_events_stop_the_step(self):
        for events in (turn("x", tools=["run_terminal_command"]),
                       turn("x")[:1] + [{"type": "tool_call", "name": "run_terminal_command"}] + turn("x")[1:],
                       turn("x")[1:]):                                       # no tool report at all
            a, steps = self.adapter(events)
            with self.assertRaises(ProviderError):
                list(a.chat_stream("grok/grok-4.7", [{"role": "user", "content": "hi"}], tools=[TOOL]))
            self.assertTrue(steps[0][0].closed)

    def test_another_task_or_a_changed_session_never_continues(self):
        a, steps = self.adapter(turn("", [("read_file", {"path": "x"})]), turn("done"),
                                turn("", [("read_file", {"path": "x"})]), turn("done", session="other"))
        first = [{"role": "user", "content": "read x"}]
        answer = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        nxt = first + [answer, {"role": "tool", "content": "UNTRUSTED d"}]
        list(a.chat_stream("grok/grok-4.7", first, tools=[TOOL], cancel=CancelToken()))
        list(a.chat_stream("grok/grok-4.7", nxt, tools=[TOOL], cancel=CancelToken()))
        self.assertIsNot(steps[0][0], steps[1][0])
        self.assertNotIn("--resume", steps[1][1])
        token = CancelToken()
        list(a.chat_stream("grok/grok-4.7", first, tools=[TOOL], cancel=token))
        with self.assertRaises(ProviderError):                               # Grok answered in another session
            list(a.chat_stream("grok/grok-4.7", nxt, tools=[TOOL], cancel=token))
        a.close()
        self.assertTrue(all(c.closed for c, _, _ in steps))

    def test_by_default_every_step_is_a_new_session(self):
        a, steps = self.adapter(turn("", [("read_file", {"path": "x"})]), turn("done"), resume=False)
        token = CancelToken()
        first = [{"role": "user", "content": "read x"}]
        answer = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        list(a.chat_stream("grok/grok-4.7", first, tools=[TOOL], cancel=token))
        self.assertTrue(steps[0][0].closed)                                  # nothing kept to resume
        list(a.chat_stream("grok/grok-4.7", first + [answer, {"role": "tool", "content": "UNTRUSTED d"}],
                           tools=[TOOL], cancel=token))
        self.assertNotIn("--resume", steps[1][1])
        self.assertIn('"transcript"', steps[1][2])

    def test_closing_removes_the_saved_sessions_of_its_folder(self):
        conv = Conversation()
        from urllib.parse import quote
        saved = Path.home() / ".grok" / "sessions" / quote(conv.directory, safe="")
        with unittest.mock.patch("umbrous.model.grok.shutil.rmtree") as rm:
            conv.close()
        removed = {str(c.args[0]) for c in rm.call_args_list}
        self.assertIn(str(saved), removed)
        self.assertIn(conv.directory, removed)
        os.rmdir(conv.directory)


if __name__ == "__main__":
    unittest.main()
