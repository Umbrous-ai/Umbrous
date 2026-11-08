"""Long tasks retain confirmed mutations even after detailed tool output is evicted."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.runloop.coding_context import cost, fit, progress


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
        self.assertIn('code.py', fitted[0]['content'])
        self.assertIn(original[-1], fitted)
        self.assertEqual(fitted[-1]['content'], 'recent result')
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
