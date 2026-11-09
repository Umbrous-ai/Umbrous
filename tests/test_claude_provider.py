"""Claude provider boundaries and its session pool; no account or model traffic needed."""
import json
import unittest
import unittest.mock
from pathlib import Path

from umbrous.model.base import CancelToken, ChatDelta, ChatDone, ChatToolCalls, ProviderError
from umbrous.model.claude import ClaudeAdapter, ClaudeSignInRequired, command
from umbrous.model.sessions import SessionPool

TOOL = {"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
MODEL = "claude-sonnet-5-5"
HANDSHAKE = {"account": {"subscriptionType": "Claude Ultra", "apiProvider": "firstParty"},
             "current_permission_mode": "dontAsk", "commands": [],
             "models": [{"value": "sonnet", "resolvedModel": MODEL, "displayName": "Sonnet 5.5", "supportsEffort": True,
                         "supportedEffortLevels": ["low", "medium", "high"]}]}
INIT = {"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": [], "model": MODEL,
        "permissionMode": "dontAsk"}


class FakeProcess:
    def __init__(self, argv=None, handshake=None):
        self.argv, self.events, self.sent, self.closed = argv, [], [], False
        self.init, self.handshakes = None, 0
        self._handshake = handshake or HANDSHAKE

    def handshake(self, timeout, cancel=None):
        self.handshakes += 1
        return self._handshake

    def send(self, text):
        self.sent.append(json.loads(text))

    def next(self, timeout, cancel=None):
        if self.events:
            return self.events.pop(0)
        raise AssertionError("unexpected wait")

    def close(self):
        self.closed = True

    def turn(self, reply, calls=(), init=INIT, extra=()):
        answer = {"reply": reply, "calls": [{"name": n, "arguments": json.dumps(a)} for n, a in calls]}
        self.events += [init, *extra,
                        {"type": "result", "subtype": "success", "is_error": False, "structured_output": answer,
                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900,
                                   "output_tokens": 20}}]


class ClaudeTests(unittest.TestCase):
    def adapter(self, *processes):
        queue, made, catalog = list(processes), [], FakeProcess()
        def factory(argv):
            if argv[argv.index("--model") + 1] == "default":      # the catalog/sign-in check
                return catalog
            p = queue.pop(0)
            p.argv = argv
            made.append(p)
            return p
        return ClaudeAdapter(Path("/tmp"), process_factory=factory, binary="/bin/claude"), made

    def test_isolating_flags_and_no_api_key(self):
        argv = command("/bin/claude", MODEL, "high", "policy", [TOOL])
        for flag in ("--tools", "--strict-mcp-config", "--safe-mode", "--disable-slash-commands",
                     "--no-session-persistence", "--setting-sources", "--json-schema", "--system-prompt"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertNotIn("--bare", argv)                        # --bare would mean an API key
        from umbrous.model.claude import _environment
        with unittest.mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-x", "UMBROUS_TOKEN": "t"}):
            env = _environment()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("UMBROUS_TOKEN", env)

    def test_catalog_uses_exact_model_ids(self):
        a, _ = self.adapter()
        names = [m.name for m in a.list_models()]
        self.assertEqual(names, ["claude/" + MODEL])
        self.assertTrue(a.status()["authenticated"])

    def test_a_task_continues_one_process_and_sends_only_new_messages(self):
        p = FakeProcess(); p.turn("Reading.", [("read_file", {"path": "x"})])
        a, made = self.adapter(p)
        token = CancelToken()
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        out = list(a.chat_stream("claude/" + MODEL, first, tools=[TOOL], cancel=token))
        self.assertEqual(out[0], ChatDelta("Reading."))
        self.assertIn(ChatToolCalls((("read_file", {"path": "x"}),)), out)
        self.assertEqual(out[-1].prompt_tokens, 1002)
        self.assertFalse(p.closed)
        answer = {"role": "assistant", "content": "Reading.",
                  "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        result = {"role": "tool", "content": "UNTRUSTED data", "tool_name": "read_file"}
        record = {"role": "system", "content": "Runtime progress record. {}"}
        p.turn("x says hi.")
        out = list(a.chat_stream("claude/" + MODEL, first + [answer, result, record], tools=[TOOL], cancel=token))
        self.assertEqual(out[0], ChatDelta("x says hi."))
        self.assertEqual(len(made), 1)                                       # one process for the task
        self.assertEqual(p.handshakes, 1)
        self.assertEqual(p.sent[0], {"transcript": first[1:], "available_tools": [TOOL]})
        self.assertEqual(p.sent[1], {"transcript_continues": [result, record]})
        self.assertTrue(p.argv[p.argv.index("--system-prompt") + 1].endswith("policy"))
        self.assertTrue(p.closed)                                            # a final answer ends it

    def test_another_task_never_continues_the_process(self):
        one = FakeProcess(); one.turn("", [("read_file", {"path": "x"})])
        two = FakeProcess(); two.turn("done")
        a, made = self.adapter(one, two)
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        list(a.chat_stream("claude/" + MODEL, first, tools=[TOOL], cancel=CancelToken()))
        answer = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        list(a.chat_stream("claude/" + MODEL, first + [answer, {"role": "tool", "content": "UNTRUSTED d"}],
                           tools=[TOOL], cancel=CancelToken()))
        self.assertEqual(len(made), 2)
        self.assertIn("transcript", two.sent[0])
        a.close()
        self.assertTrue(one.closed)

    def test_a_refused_direct_tool_call_is_tolerated_but_a_tool_that_ran_is_not(self):
        start = {"type": "stream_event", "event": {"type": "content_block_start",
                 "content_block": {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}}}}
        refused = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "is_error": True}]}}
        ran = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "is_error": False}]}}
        ok = FakeProcess(); ok.turn("fine", extra=(start, refused))
        bad = FakeProcess(); bad.turn("fine", extra=(start, ran))
        a, _ = self.adapter(ok, bad)
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(list(a.chat_stream("claude/" + MODEL, msgs, tools=[TOOL]))[0], ChatDelta("fine"))
        with self.assertRaises(ProviderError):
            list(a.chat_stream("claude/" + MODEL, msgs, tools=[TOOL]))
        self.assertTrue(bad.closed)

    def test_unexpected_tools_mcp_or_model_stop_the_step(self):
        for init in (dict(INIT, tools=["StructuredOutput", "Bash"]), dict(INIT, mcp_servers=[{"name": "x"}]),
                     dict(INIT, model="claude-haiku-5-5")):
            p = FakeProcess(); p.turn("x", init=init)
            a, _ = self.adapter(p)
            with self.assertRaises(ProviderError):
                list(a.chat_stream("claude/" + MODEL, [{"role": "user", "content": "hi"}], tools=[TOOL]))
            self.assertTrue(p.closed)

    def test_api_key_or_signed_out_accounts_send_nothing(self):
        for account in ({}, {"subscriptionType": None}, {"subscriptionType": "pro", "apiProvider": "bedrock"}):
            p = FakeProcess(handshake=dict(HANDSHAKE, account=account)); p.turn("x")
            a, _ = self.adapter(p)
            a._catalog, a._catalog_at = HANDSHAKE, 1e18           # catalog cached; the step's own handshake fails
            with self.assertRaises(ClaudeSignInRequired):
                list(a.chat_stream("claude/" + MODEL, [{"role": "user", "content": "hi"}], tools=[TOOL]))
            self.assertEqual(p.sent, [])

    def test_unsupported_effort_is_refused_before_starting(self):
        a, made = self.adapter()
        with self.assertRaises(ProviderError):
            list(a.chat_stream("claude/" + MODEL, [{"role": "user", "content": "hi"}], options={"reasoning_effort": "max"}))
        self.assertEqual(made, [])


class Handle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SessionPoolTests(unittest.TestCase):
    def parked(self, pool, owner):
        h = Handle()
        sess = pool.open(lambda: (h, h.close), ("k",), owner)
        sess.done_step([{"role": "user", "content": "go"}], "r", (("read_file", {"path": "x"}),))
        pool.release(sess, True)
        return sess, h, [{"role": "user", "content": "go"},
                         {"role": "assistant", "content": "r",
                          "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}]

    def test_owner_and_exact_continuation_are_both_required(self):
        pool, owner = SessionPool(), CancelToken()
        sess, h, nxt = self.parked(pool, owner)
        self.assertIsNone(pool.claim(("k",), nxt, CancelToken()))           # another task
        self.assertIsNone(pool.claim(("k",), nxt, None))                    # no task identity
        self.assertIsNone(pool.claim(("k",), nxt[:1] + [dict(nxt[1], content="other")], owner))
        self.assertIs(pool.claim(("k",), nxt, owner), sess)
        pool.release(sess, False)
        self.assertTrue(h.closed)

    def test_a_stale_idle_timer_cannot_close_a_claimed_session(self):
        pool, owner = SessionPool(), CancelToken()
        sess, h, nxt = self.parked(pool, owner)
        stale = sess.generation
        self.assertIs(pool.claim(("k",), nxt, owner), sess)
        pool._expire(sess, stale)
        self.assertFalse(h.closed)

    def test_close_between_a_step_and_parking_closes_the_session(self):
        pool, owner = SessionPool(), CancelToken()
        h = Handle()
        sess = pool.open(lambda: (h, h.close), ("k",), owner)
        pool.close()                                                        # shutdown while the step runs
        self.assertTrue(h.closed)
        pool.release(sess, True)
        self.assertEqual(pool._idle, [])
        later = Handle()
        sess2 = pool.open(lambda: (later, later.close), ("k",), owner)      # after close, it works again
        pool.release(sess2, True)
        self.assertEqual(len(pool._idle), 1)
        pool.close()
        self.assertTrue(later.closed)


if __name__ == "__main__":
    unittest.main()
