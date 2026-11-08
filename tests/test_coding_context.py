"""Long tasks retain confirmed mutations even after detailed tool output is evicted."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.runloop.coding_context import SUPERSEDED, cost, fit, progress, supersede
from umbrous.gate.gate import ollama_tools, wrap_untrusted


class CodingContextTests(unittest.TestCase):
    def test_large_edit_arguments_count_and_complete_rounds_are_evicted(self):
        original = [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'finish the change'}]
        messages = original + [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'edit_file', 'arguments': {'new_text': 'x' * 20000}}}]},
            {'role': 'tool', 'content': 'edit succeeded'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'read_file', 'arguments': {'path': 'notes'}}}]},
            {'role': 'tool', 'content': 'recent result'}]
        run = SimpleNamespace(changes=[{'path': 'code.py', 'op': 'edited', 'after': 'sha'}], commands=[], actions={})
        record = progress(run)
        fitted, used = fit(messages, 1800, record)
        self.assertLessEqual(used, 1800)
        self.assertIn('code.py', fitted[-1]['content'])      # the record goes last (a stable, cacheable prefix)
        self.assertNotIn('"after"', fitted[-1]['content'])   # no file hashes in the record
        self.assertIn(original[-1], fitted)
        self.assertEqual(fitted[-2]['content'], 'recent result')
        self.assertNotIn(messages[2], fitted)
        self.assertEqual(len(messages), 6, 'fitting must not mutate a caller transcript')

    def test_long_task_edits_then_verifies_without_losing_receipt(self):
        calls = [('read_file', {'path': 'value.txt'}),
                 ('edit_file', {'path': 'value.txt', 'old_text': 'old', 'new_text': 'new'})]
        calls += [('read_file', {'path': f'notes{i}.txt'}) for i in range(8)]
        calls += [('run_command', {'command': "test \"$(cat value.txt)\" = new"})]
        observed = []
        def agent(messages):
            i = len(observed)
            observed.append(messages)
            if i < len(calls):
                return {'tool_calls': [calls[i]]}
            record = next(m['content'] for m in messages if m['content'].startswith('Runtime progress record.'))
            data = json.loads(record.split('\n', 1)[1])
            self.assertEqual(data['changes_total'], 1)
            self.assertEqual(data['recent_changes'][0]['path'], 'value.txt')
            self.assertEqual(data['recent_commands'][-1]['exit'], 0)
            return {'content': 'Edited value.txt and verified its content with a passing command.'}
        fake = FakeOllama(models=[FakeModel('long:3b', agent=agent)]).start()
        self.addCleanup(fake.stop)
        with tempfile.TemporaryDirectory() as tmp, RunningRuntime(ollama_url=fake.url, memory_suggestions=False) as rt:
            root = Path(tmp); (root/'value.txt').write_text('old')
            for i in range(8): (root/f'notes{i}.txt').write_text(('reference text ' * 30 + '\n') * 90)
            rt.runtime.access.attach('prj_general', str(root), 'workspace_code')
            self.assertEqual(rt.post_json('/api/models/probe', {'model':'long:3b'})[0], 200)
            events = rt.chat(rt.new_chat(), 'long:3b', 'Change value.txt and verify the result')
            self.assertEqual(events[-1]['type'], 'done', events[-1])
            self.assertEqual((root/'value.txt').read_text(), 'new')
            self.assertEqual(len(events[-1]['report']['changes']), 1)
            self.assertEqual(events[-1]['report']['commands'][-1]['exit'], 0)
            self.assertEqual(len(observed), len(calls) + 1)
            for messages in observed:
                self.assertLessEqual(cost(messages), 8192 - 1024 - 128)
            for messages in observed[1:]:
                # The record is last, so each step's request starts with the previous one (cacheable).
                self.assertTrue(messages[-1]['content'].startswith('Runtime progress record.'))
            for before, after in zip(observed[1:], observed[2:]):
                if len(after) > len(before):      # no compaction in between
                    self.assertEqual(before[:-1], after[:len(before) - 1])


def _round(name, args, result):
    return [{'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': name, 'arguments': args}}]},
            {'role': 'tool', 'content': result}]


class TokenSavingTests(unittest.TestCase):
    def test_an_identical_later_call_supersedes_the_older_result(self):
        read = wrap_untrusted('read_file a.py lines 1-2 of 2.\n1: x\n2: y' + '\nfiller' * 200)
        messages = ([{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'go'}]
                    + _round('read_file', {'path': 'a.py'}, read)
                    + _round('edit_file', {'path': 'a.py', 'old_text': 'x', 'new_text': 'z'}, wrap_untrusted('edited'))
                    + _round('edit_file', {'path': 'a.py', 'old_text': 'x', 'new_text': 'z'}, wrap_untrusted('again'))
                    + _round('read_file', {'path': 'a.py', 'start_line': 2}, read)
                    + _round('run_command', {'command': 'make'}, 'TOOL ERROR (failed, cancelled): Stopped.')
                    + _round('run_command', {'command': 'make'}, wrap_untrusted('exit 0'))
                    + _round('read_file', {'path': 'a.py'}, read))
        saved = supersede(messages)
        self.assertGreater(saved, 0)
        contents = [m['content'] for m in messages if m['role'] == 'tool']
        self.assertEqual(contents[0], SUPERSEDED)                   # the same read, made again later
        self.assertEqual(contents[1:3], [wrap_untrusted('edited'), wrap_untrusted('again')])   # changes stay
        self.assertEqual(contents[3], read)                         # a different range stays
        self.assertTrue(contents[4].startswith('TOOL ERROR'))       # a failure is never replaced
        self.assertEqual(contents[5:], [wrap_untrusted('exit 0'), read])   # the newest results stay
        self.assertEqual(supersede(messages), 0)                    # idempotent

    def test_tool_definitions_hide_the_hash_parameter_but_keep_the_rules(self):
        tools = {t['function']['name']: t['function'] for t in ollama_tools(['write_file', 'edit_file', 'read_file'])}
        self.assertNotIn('expected_sha256', tools['edit_file']['parameters']['properties'])
        self.assertEqual(tools['edit_file']['parameters']['required'], ['path', 'old_text', 'new_text'])
        self.assertIn('exactly once', tools['edit_file']['description'])
        self.assertIn('conflict', tools['write_file']['description'])
