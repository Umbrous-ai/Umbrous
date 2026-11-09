"""Codex protocol boundaries; no account, model traffic, or credentials required."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from umbrous.model.base import CancelToken, Cancelled, ChatDelta, ChatDone, ChatToolCalls, Deadlines, ModelTimeout, ProviderError
from umbrous.model.codex import (AppServer, CodexAdapter, CodexSignInRequired, DISABLED_FEATURES,
                                 _environment, _startup_command, decode_reply, output_schema, reply_prefix)

CATALOG = {"model": "test-model", "displayName": "Test model", "hidden": False,
           "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
           "serviceTiers": [{"id": "priority", "name": "Fast"}]}
TOOL = {"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


class FakeServer:
    def __init__(self, reply=None, account=True, events=None):
        self.directory = type("D", (), {"name": "/tmp/umbrous-neutral"})()
        self.pending = []
        self.closed = False
        self.calls = []
        self.connected = account
        self.events = events if events is not None else [
            {"method": "item/agentMessage/delta", "params": {"threadId": "t", "delta": json.dumps(reply or {"reply": "hello", "calls": []})}},
            {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "completed"}}}]
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def close(self): self.closed = True
    def account(self): return {"connected": self.connected, "plan": "test"}
    def isolated_config(self): return {"features.shell_tool": False}
    def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "model/list": return {"data": [CATALOG], "nextCursor": None}
        if method == "thread/start": return {"thread": {"id": "t"}, "model": CATALOG["model"], "reasoningEffort": params['config']['model_reasoning_effort'], "serviceTier": params['serviceTier']}
        if method == "mcpServerStatus/list": return {"data": [], "nextCursor": None}
        if method == "turn/start": return {"turn": {"id": "turn", "status": "inProgress"}}
        raise AssertionError(method)
    def next(self, timeout, cancel=None):
        if cancel and cancel.cancelled: raise Cancelled("Stopped")
        if self.events: return self.events.pop(0)
        raise AssertionError("unexpected wait")


class CodexTests(unittest.TestCase):
    def adapter(self, **kwargs):
        servers = []
        def factory():
            s = FakeServer(**kwargs); servers.append(s); return s
        return CodexAdapter(Path('/tmp'), server_factory=factory), servers

    def test_catalog_carries_advertised_controls_and_invalidates_digest(self):
        a, _ = self.adapter()
        info = a.list_models()[0]
        self.assertEqual(info.name, 'codex/test-model')
        self.assertEqual(info.reasoning_efforts, ('low', 'high'))
        self.assertEqual(info.speeds, ('standard', 'fast'))
        self.assertEqual(info.provider, 'codex')
        self.assertEqual(len(info.digest), 64)

    def test_standard_is_explicit_and_no_environment_is_attached(self):
        a, servers = self.adapter()
        pieces = list(a.chat_stream('codex/test-model', [{'role': 'user', 'content': 'hi'}]))
        self.assertEqual(pieces, [ChatDelta('hello'), ChatDone('codex/test-model', done_reason='stop')])
        call = dict(servers[-1].calls)
        self.assertEqual(call['thread/start']['environments'], [])
        self.assertEqual(call['thread/start']['runtimeWorkspaceRoots'], [])
        self.assertEqual(call['thread/start']['sandbox'], 'read-only')
        self.assertTrue(call['thread/start']['ephemeral'])
        self.assertFalse(call['thread/start']['allowProviderModelFallback'])
        self.assertEqual(call['turn/start']['serviceTierForTurn'], 'default')
        self.assertEqual(call['turn/start']['effort'], 'low')
        self.assertTrue(all(s.closed for s in servers))

    def test_fast_and_reasoning_are_per_request(self):
        a, servers = self.adapter()
        list(a.chat_stream('codex/test-model', [], options={'speed': 'fast', 'reasoning_effort': 'high'}))
        first = dict(servers[-1].calls)['turn/start']
        list(a.chat_stream('codex/test-model', []))
        second = dict(servers[-1].calls)['turn/start']
        self.assertEqual((first['serviceTierForTurn'], first['effort']), ('priority', 'high'))
        self.assertEqual((second['serviceTierForTurn'], second['effort']), ('default', 'low'))

    def test_invalid_settings_do_not_start_inference(self):
        a, servers = self.adapter()
        for opts in ({'speed': 'turbo'}, {'reasoning_effort': 'unknown'}):
            with self.assertRaises(ProviderError): list(a.chat_stream('codex/test-model', [], options=opts))
        self.assertFalse(any(m == 'thread/start' for s in servers for m, _ in s.calls))

    def test_proposals_are_data_for_the_gate(self):
        a, _ = self.adapter(reply={'reply': '', 'calls': [{'name': 'read_file', 'arguments': '{"path":"x"}'}]})
        pieces = list(a.chat_stream('codex/test-model', [], tools=[TOOL]))
        self.assertEqual(pieces[0], ChatToolCalls((('read_file', {'path': 'x'}),)))
        msg = a.chat_once('codex/test-model', [], tools=[TOOL])['message']
        self.assertEqual(msg['tool_calls'][0]['function']['name'], 'read_file')

    def test_native_readonly_keeps_authorized_edit_proposals_available(self):
        tool = {"type": "function", "function": {"name": "edit_file", "description": "Edit an attached workspace file",
                                                "parameters": {"type": "object"}}}
        arguments = {"path": "calc.py", "old_text": "TODO", "new_text": "return a * b"}
        a, servers = self.adapter(reply={"reply": "", "calls": [{"name": "edit_file", "arguments": json.dumps(arguments)}]})
        pieces = list(a.chat_stream('codex/test-model', [], tools=[tool]))
        self.assertEqual(pieces[0], ChatToolCalls((("edit_file", arguments),)))
        thread = dict(servers[-1].calls)['thread/start']
        self.assertEqual(thread['sandbox'], 'read-only')
        self.assertEqual(thread['environments'], [])
        self.assertIn('application-owned proposals', thread['baseInstructions'])

    def test_unoffered_or_malformed_tool_proposals_fail_closed(self):
        for data in ({'reply': '', 'calls': [{'name': 'run_command', 'arguments': '{}'}]},
                     {'reply': '', 'calls': [{'name': 'read_file', 'arguments': '[]'}]},
                     {'reply': '', 'calls': [{'name': 'read_file', 'arguments': '{'}]}):
            with self.assertRaises(ProviderError): decode_reply(json.dumps(data), [TOOL])
        self.assertEqual(output_schema([])['properties']['calls']['maxItems'], 0)

    def message_events(self, ident, text, phase=None, deltas=True):
        item = {'type': 'agentMessage', 'id': ident, 'text': '', 'phase': phase}
        events = [{'method': 'item/started', 'params': {'item': dict(item)}}]
        if deltas:
            events.append({'method': 'item/agentMessage/delta',
                           'params': {'itemId': ident, 'delta': text}})
        item['text'] = text
        events.append({'method': 'item/completed', 'params': {'item': item}})
        return events

    def test_commentary_is_not_concatenated_with_final_envelope(self):
        for phase in ('final_answer', None):
            with self.subTest(phase=phase):
                events = self.message_events('progress', 'I will check the app.', 'commentary')
                events += self.message_events('answer', json.dumps({'reply': 'Ready', 'calls': []}), phase)
                events += [{'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}]
                a, _ = self.adapter(events=events)
                self.assertEqual(list(a.chat_stream('codex/test-model', [])),
                                 [ChatDelta('Ready'), ChatDone('codex/test-model', done_reason='stop')])

    def test_final_reply_streams_before_turn_completion(self):
        events = [{"method": "item/started", "params": {"item": {"type":"agentMessage", "id":"a", "phase":"final_answer"}}},
                  {"method":"item/agentMessage/delta", "params":{"itemId":"a", "delta":'{"reply":"First '}},
                  {"method":"item/agentMessage/delta", "params":{"itemId":"a", "delta":'second","calls":[]}'}},
                  {"method":"turn/completed", "params":{"turn":{"status":"completed"}}}]
        adapter, servers = self.adapter(events=events)
        stream = adapter.chat_stream('codex/test-model', [])
        self.assertEqual(next(stream), ChatDelta('First '))
        self.assertFalse(servers[-1].closed)
        self.assertEqual(next(stream), ChatDelta('second'))
        self.assertEqual(list(stream), [ChatDone('codex/test-model', done_reason='stop')])

    def test_reply_prefix_keeps_escapes_and_tool_arguments_private(self):
        text = json.dumps({"reply": 'Line 1\n"quoted" 😀', "calls": [{"arguments":"secret"}]})
        previous = ''
        for end in range(len(text) + 1):
            value = reply_prefix(text[:end])
            self.assertTrue(value.startswith(previous))
            self.assertNotIn('secret', value)
            value.encode('utf-8')
            previous = value
        self.assertEqual(previous, 'Line 1\n"quoted" 😀')
        self.assertEqual(reply_prefix('{"calls":[{"reply":"secret"}]}'), '')

    def test_completed_message_works_without_deltas(self):
        events = self.message_events('answer', json.dumps({'reply': '', 'calls': [
            {'name': 'read_file', 'arguments': '{"path":"x"}'}]}), 'final_answer', deltas=False)
        events += [{'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}]
        a, _ = self.adapter(events=events)
        self.assertEqual(list(a.chat_stream('codex/test-model', [], tools=[TOOL]))[0],
                         ChatToolCalls((('read_file', {'path': 'x'}),)))

    def test_commentary_proposals_are_never_executed(self):
        events = self.message_events('progress', json.dumps({'reply': '', 'calls': [
            {'name': 'read_file', 'arguments': '{"path":"x"}'}]}), 'commentary')
        events += [{'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}]
        a, _ = self.adapter(events=events)
        with self.assertRaises(ProviderError):
            list(a.chat_stream('codex/test-model', [], tools=[TOOL]))

    def test_invalid_final_does_not_fall_back_to_earlier_valid_message(self):
        events = self.message_events('earlier', json.dumps({'reply': 'stale', 'calls': []}))
        events += self.message_events('answer', 'invalid JSON', 'final_answer')
        events += [{'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}]
        a, _ = self.adapter(events=events)
        with self.assertRaises(ProviderError):
            list(a.chat_stream('codex/test-model', []))

    def test_completed_text_still_obeys_size_limit(self):
        events = self.message_events('answer', 'x' * 101, 'final_answer', deltas=False)
        a, _ = self.adapter(events=events)
        with patch('umbrous.model.codex.MAX_REPLY', 100):
            with self.assertRaisesRegex(ProviderError, 'safe size limit'):
                list(a.chat_stream('codex/test-model', []))

    def test_failed_turn_is_never_success(self):
        a, servers = self.adapter(events=[{'method': 'turn/completed', 'params': {'turn': {'status': 'failed', 'error': {'message': 'secret'}}}}])
        with self.assertRaisesRegex(ProviderError, 'ChatGPT request failed') as err:
            list(a.chat_stream('codex/test-model', []))
        self.assertNotIn('secret', str(err.exception))
        self.assertTrue(servers[-1].closed)

    def test_unexpected_native_tool_event_aborts(self):
        a, servers = self.adapter(events=[{'method': 'item/started', 'params': {'item': {'type': 'commandExecution'}}}])
        with self.assertRaisesRegex(ProviderError, "outside Umbrous"):
            list(a.chat_stream('codex/test-model', []))
        self.assertTrue(servers[-1].closed)

    def test_cancellation_before_start_never_starts_a_process(self):
        a, servers = self.adapter(); c = CancelToken(); c.cancel()
        with self.assertRaises(Cancelled): list(a.chat_stream('codex/test-model', [], cancel=c))
        self.assertEqual(servers, [])

    def test_unrelated_notifications_do_not_reset_first_token_deadline(self):
        class Noisy(FakeServer):
            def next(self, timeout, cancel=None):
                time.sleep(.01)
                return {'method': 'notice', 'params': {}}
        a = CodexAdapter(Path('/tmp'), server_factory=Noisy)
        start = time.monotonic()
        with self.assertRaises(ModelTimeout):
            list(a.chat_stream('codex/test-model', [], deadlines=Deadlines(first_token=.03, idle=.03, total=.2)))
        self.assertLess(time.monotonic() - start, .15)

    def test_no_api_key_fallback(self):
        a, servers = self.adapter(account=False)
        self.assertFalse(a.status()['authenticated'])
        with self.assertRaises(CodexSignInRequired): a.list_models()
        self.assertFalse(any(m == 'thread/start' for s in servers for m, _ in s.calls))

    def test_environment_does_not_pass_application_tokens_or_api_keys(self):
        with patch.dict(os.environ, {'UMBROUS_SESSION_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret',
                                     'ACCESS_TOKEN': 'secret', 'HOME': '/tmp/home'}, clear=True):
            self.assertEqual(_environment(), {'HOME': '/tmp/home'})

    def test_startup_disables_integrations_before_reading_config(self):
        args = _startup_command('/codex')
        self.assertEqual(args[:3], ['/codex', 'app-server', '--stdio'])
        for feature in DISABLED_FEATURES:
            index = args.index(feature)
            self.assertEqual(args[index-1], '--disable')
        self.assertIn('notify=[]', args)
        self.assertIn('include_permissions_instructions=false', args)

    def test_nested_mcp_overrides_disable_every_inherited_server(self):
        s = AppServer.__new__(AppServer)
        s.call = lambda *a, **k: {'config': {'features': {f: False for f in DISABLED_FEATURES},
                                            'mcp_servers': {'simple': {}, 'name.with.dots': {}}}}
        config = s.isolated_config()
        self.assertEqual(config['mcp_servers'], {'simple': {'enabled': False}, 'name.with.dots': {'enabled': False}})
        s.call = lambda *a, **k: {'config': {'features': {}}}
        with self.assertRaises(ProviderError): s.isolated_config()

    def test_selected_controls_cannot_silently_fallback(self):
        for field, value in (("model", "different"), ("reasoningEffort", "high"), ("serviceTier", "priority")):
            class Changed(FakeServer):
                def call(self, method, params, **kwargs):
                    result = super().call(method, params, **kwargs)
                    if method == "thread/start": result[field] = value
                    return result
            a = CodexAdapter(Path('/tmp'), server_factory=Changed)
            with self.assertRaisesRegex(ProviderError, 'did not confirm'):
                list(a.chat_stream('codex/test-model', []))

    def test_enabled_mcp_integration_prevents_inference(self):
        servers = []
        class Enabled(FakeServer):
            def call(self, method, params, **kwargs):
                if method == 'mcpServerStatus/list':
                    return {'data': [{'runtimeStatus': 'ready', 'tools': {'write': {}}}]}
                return super().call(method, params, **kwargs)
        def factory():
            server = Enabled(); servers.append(server); return server
        a = CodexAdapter(Path('/tmp'), server_factory=factory)
        with self.assertRaisesRegex(ProviderError, 'isolate'):
            list(a.chat_stream('codex/test-model', []))
        self.assertFalse(any(m == 'turn/start' for server in servers for m, _ in server.calls))

    def test_unread_stdin_has_a_bounded_write_deadline(self):
        import threading
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        stream = os.fdopen(write_fd, 'wb', buffering=0)
        server = AppServer.__new__(AppServer)
        server.proc = type('P', (), {'stdin': stream})()
        server.closed = False
        server.lock = threading.Lock()
        try:
            with self.assertRaises(ModelTimeout):
                server.send({'payload': 'x' * 500000}, timeout=.03)
        finally:
            stream.close(); os.close(read_fd)

    # ---------------------------------------------------------------- one conversation per task
    def turn(self, server, reply, calls=()):
        body = {"reply": reply, "calls": [{"name": n, "arguments": json.dumps(a)} for n, a in calls]}
        server.events += [{"method": "item/agentMessage/delta", "params": {"threadId": "t", "delta": json.dumps(body)}},
                          {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "completed"}}}]

    def task_adapter(self):
        a = CodexAdapter(Path('/tmp'), server_factory=FakeServer)
        list(a._models())               # the catalog (its own short-lived server) is cached first
        return a, None

    def inputs(self, server):
        return [json.loads(p["input"][0]["text"]) for m, p in server.calls if m == "turn/start"]

    def test_a_task_continues_one_conversation_and_sends_only_new_messages(self):
        a, servers = self.task_adapter()
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        token = CancelToken()                     # one task: its token is its identity
        step = lambda msgs, tools=[TOOL]: list(a.chat_stream('codex/test-model', msgs, tools=tools, cancel=token))
        # step 1: a new conversation with the whole transcript
        task = FakeServer(events=[]); self.turn(task, "Reading.", [("read_file", {"path": "x"})])
        a.server_factory = lambda: task
        out = step(first)
        self.assertIn(ChatToolCalls((("read_file", {"path": "x"}),)), out)
        self.assertFalse(task.closed)                                       # kept for the next step
        answer = {"role": "assistant", "content": "Reading.", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        result = {"role": "tool", "content": "UNTRUSTED ... 1: hi", "tool_name": "read_file"}
        record = {"role": "system", "content": "Runtime progress record. {}"}
        # step 2: the same conversation; only the tool result and the fresh record are sent
        a.server_factory = lambda: self.fail("a new conversation was started")
        self.turn(task, "x says hi.")
        out = step(first + [answer, result, record])
        self.assertEqual(out[-1], ChatDone('codex/test-model', done_reason='stop'))
        self.assertEqual([m for m, _ in task.calls].count("thread/start"), 1)
        sent = self.inputs(task)
        self.assertEqual(sent[0], {"transcript": first[1:], "available_tools": [TOOL]})
        self.assertEqual(sent[1], {"transcript_continues": [result, record]})    # tools unchanged: not resent
        start = next(p for m, p in task.calls if m == "thread/start")
        self.assertTrue(start["baseInstructions"].endswith("policy"))           # the record is not in the instructions
        self.assertTrue(task.closed)                                            # a final answer ends the conversation

    def test_a_request_that_does_not_continue_the_task_starts_a_new_conversation(self):
        a, servers = self.task_adapter()
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        one = FakeServer(events=[]); self.turn(one, "", [("read_file", {"path": "x"})])
        a.server_factory = lambda: one
        token = CancelToken()
        list(a.chat_stream('codex/test-model', first, tools=[TOOL], cancel=token))
        answer = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]}
        result = {"role": "tool", "content": "UNTRUSTED data", "tool_name": "read_file"}
        two = FakeServer(events=[]); self.turn(two, "done")
        a.server_factory = lambda: two
        changed = [first[0], {"role": "user", "content": "read y"}, answer, result]     # an earlier message differs
        list(a.chat_stream('codex/test-model', changed, tools=[TOOL], cancel=token))
        self.assertEqual(self.inputs(two)[0]["transcript"], changed[1:])               # full transcript, new thread
        self.assertFalse(one.closed)                                                   # still waiting for its own task
        a.close()
        self.assertTrue(one.closed)

    def test_a_failed_step_never_leaves_a_conversation_to_continue(self):
        a, servers = self.task_adapter()
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        one = FakeServer(events=[{"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "failed"}}}])
        a.server_factory = lambda: one
        with self.assertRaises(ProviderError):
            list(a.chat_stream('codex/test-model', first, tools=[TOOL]))
        self.assertTrue(one.closed)
        self.assertEqual(a._idle, [])

    def started_task(self, a, reply="", calls=(("read_file", {"path": "x"}),)):
        first = [{"role": "system", "content": "policy"}, {"role": "user", "content": "read x"}]
        server = FakeServer(events=[]); self.turn(server, reply, calls)
        a.server_factory = lambda: server
        token = CancelToken()
        list(a.chat_stream('codex/test-model', first, tools=[TOOL], cancel=token))
        answer = {"role": "assistant", "content": reply,
                  "tool_calls": [{"function": {"name": n, "arguments": x}} for n, x in calls]}
        return server, token, first + [answer, {"role": "tool", "content": "UNTRUSTED data", "tool_name": "read_file"}]

    def test_another_task_never_continues_a_conversation_even_with_the_same_content(self):
        a, _ = self.task_adapter()
        one, token_a, next_messages = self.started_task(a)
        two = FakeServer(events=[]); self.turn(two, "done")
        a.server_factory = lambda: two
        other_task = CancelToken()
        list(a.chat_stream('codex/test-model', next_messages, tools=[TOOL], cancel=other_task))
        self.assertEqual([m for m, _ in two.calls].count("thread/start"), 1)   # its own conversation
        self.assertEqual([m for m, _ in one.calls].count("turn/start"), 1)     # A's was not used
        other_task.cancel()
        self.assertFalse(one.closed)                                           # B's stop cannot close A's
        token_a.cancel()
        self.assertTrue(one.closed)
        a.close()

    def test_a_stale_idle_timer_cannot_close_a_conversation_in_use(self):
        a, _ = self.task_adapter()
        one, token, next_messages = self.started_task(a)
        sess = a._idle[0]
        stale = sess.generation
        claimed = a._claim(sess.key, [m for m in next_messages if m["role"] != "system"], token)
        self.assertIs(claimed, sess)
        a._expire(sess, stale)                     # the timer's callback was already running
        self.assertFalse(one.closed)
        a._expire(sess, sess.generation)           # not idle any more: still not closed
        self.assertFalse(one.closed)
        a._release(sess, False)
        self.assertTrue(one.closed)

    def test_close_during_a_step_is_never_missed(self):
        a, _ = self.task_adapter()
        server = FakeServer(events=[])
        a.server_factory = lambda: server
        token = CancelToken()
        with a._session(("k",), [], cancel=token) as sess:
            sess.keep = True                       # the step would keep its conversation...
            a.close()                              # ...but the adapter is shutting down meanwhile
        self.assertTrue(server.closed)
        self.assertEqual(a._idle, [])
        later = FakeServer(events=[])
        a.server_factory = lambda: later
        with a._session(("k",), [], cancel=token) as sess:   # after close, new sessions work as before
            sess.keep = True
        self.assertFalse(later.closed)
        self.assertEqual(len(a._idle), 1)
        a.close()
        self.assertTrue(later.closed)

    def test_close_stops_live_processes(self):
        a, _ = self.adapter(); s = FakeServer()
        a._active.add(s); a.close(); self.assertTrue(s.closed)


if __name__ == '__main__': unittest.main()
