"""Long tasks retain confirmed mutations even after detailed tool output is evicted."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.runloop.coding_context import NOTES, SUPERSEDED, compact_old, cost, fit, progress, supersede, trim
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

    def test_old_results_later_steps_made_redundant_keep_only_their_header(self):
        listing = wrap_untrusted('find_files *.py in .: 60 match(es)\n' + '\n'.join(f'pkg/module_{i}.py' for i in range(60)))
        read_a = wrap_untrusted('read_file a.py lines 1-40 of 40.\n' + ''.join(f'{i}: x = {i}\n' for i in range(1, 41)))
        read_b = wrap_untrusted('read_file b.py lines 1-2 of 2.\n1: p\n2: q')
        run = wrap_untrusted('run_command: make\nexited with status 1\n--- stdout (900 bytes) ---\n' + 'FAIL test_x\n' * 60)
        messages = ([{'role': 'user', 'content': 'go'}]
                    + _round('find_files', {'pattern': '*.py'}, listing)
                    + _round('read_file', {'path': 'a.py'}, read_a)
                    + _round('read_file', {'path': 'b.py'}, read_b)
                    + _round('run_command', {'command': 'make'}, run)
                    + _round('edit_file', {'path': 'a.py', 'old_text': 'x', 'new_text': 'z'}, wrap_untrusted('edited'))
                    + _round('run_command', {'command': 'make test'}, run))
        self.assertGreater(compact_old(messages), 0)
        tools = [m['content'] for m in messages if m['role'] == 'tool']
        self.assertIn('find_files *.py in .: 60 match(es)', tools[0])          # the header stays
        self.assertNotIn('module_5.py', tools[0])
        self.assertIn(NOTES['navigation'], tools[0])
        self.assertIn(NOTES['stale'], tools[1])                              # a.py was edited later
        self.assertEqual(tools[2], read_b)                                   # b.py may still be edited from
        self.assertEqual(tools[3], run)          # command output is never shortened by unrelated later calls
        self.assertEqual(tools[4:], [wrap_untrusted('edited'), run])         # the newest rounds stay whole
        for t in tools:
            self.assertTrue(t.startswith('UNTRUSTED') and t.endswith('<<<END WORKSPACE DATA>>>'))

    def test_a_trim_must_pay_for_the_prompt_it_makes_the_server_reprocess(self):
        big = wrap_untrusted('read_file a.py lines 1-400 of 400.\n' + 'line of code\n' * 400)
        tail = wrap_untrusted('read_file b.py lines 1-400 of 400.\n' + 'other code here\n' * 2000)
        base = [{'role': 'user', 'content': 'go'}] + _round('read_file', {'path': 'a.py'}, big)
        cheap = base + _round('read_file', {'path': 'a.py'}, big)
        costly = base + _round('read_file', {'path': 'b.py'}, tail) + _round('read_file', {'path': 'a.py'}, big)
        self.assertGreater(trim(cheap), 0)                 # saves more than half of what follows it
        self.assertEqual(trim(costly), 0)                  # would make far more be read again: wait
        self.assertEqual(costly[2]['content'], big)
        self.assertGreater(trim(costly, 0), 0)             # minimum 0 (over budget): always

    def test_an_unresolved_failure_keeps_its_output_through_unrelated_steps(self):
        fail = wrap_untrusted('run_command: pytest\nexited with status 1 after 2.0 s\n--- stdout (4,000 bytes) ---\n'
                              + 'Traceback (most recent call last):\n  File "x.py", line 3\nAssertionError\n' * 60)
        page = wrap_untrusted('read_file x.py lines 1-300 of 300.\n' + ''.join(f'{i}: v = {i}\n' for i in range(300)))
        messages = ([{'role': 'user', 'content': 'fix the tests'}]
                    + _round('run_command', {'command': 'pytest'}, fail)
                    + _round('read_file', {'path': 'x.py'}, page)
                    + _round('run_command', {'command': 'pwd'}, wrap_untrusted('run_command: pwd\nexited with status 0'))
                    + _round('read_file', {'path': 'x.py'}, page)
                    + _round('list_files', {}, wrap_untrusted('list_files .: 1 entries.\nx.py (9 B)')))
        trim(messages, 0)
        self.assertEqual(messages[2]['content'], fail)                  # no fix, no rerun: the traceback stays
        self.assertEqual(messages[4]['content'], SUPERSEDED)            # the identical later read replaced it
        again = messages + _round('run_command', {'command': 'pytest'}, fail)
        trim(again, 0)
        self.assertEqual(again[2]['content'], SUPERSEDED)               # the same command ran again: now it can go

    def test_running_background_commands_keep_their_ids(self):
        started = wrap_untrusted('run_command: npm run dev\nstill running after 2.0 s (id cmd_aaaa; check it with '
                                 'check_command)\n--- stdout (2,000 bytes) ---\n' + 'ready\n' * 300)
        second = started.replace('cmd_aaaa', 'cmd_bbbb')
        messages = ([{'role': 'user', 'content': 'start it'}]
                    + _round('run_command', {'command': 'npm run dev', 'background': True}, started)
                    + _round('run_command', {'command': 'npm run dev', 'background': True}, second))
        self.assertEqual(supersede(messages), 0)
        self.assertIn('cmd_aaaa', messages[2]['content'])
        run = SimpleNamespace(changes=[], actions={}, commands=[
            {'command': 'npm run dev', 'status': 'running', 'exit': None, 'cmd_id': 'cmd_aaaa'},
            {'command': 'npm run dev', 'status': 'running', 'exit': None, 'cmd_id': 'cmd_bbbb'},
            {'command': 'check', 'status': 'exited', 'exit': 0, 'cmd_id': 'cmd_bbbb', 'check': True}])
        data = json.loads(progress(run)['content'].split('\n', 1)[1])
        self.assertEqual([c['cmd_id'] for c in data['running_commands']], ['cmd_aaaa'])

    def test_the_cache_cost_counts_tool_arguments_and_the_record(self):
        read = wrap_untrusted('read_file a.py lines 1-80 of 80.\n' + ''.join(f'{i}: a = {i}\n' for i in range(80)))
        messages = ([{'role': 'user', 'content': 'go'}]
                    + _round('read_file', {'path': 'a.py'}, read)
                    + _round('write_file', {'path': 'big.py', 'content': 'x' * 40000}, wrap_untrusted('write_file ok'))
                    + _round('read_file', {'path': 'a.py'}, read))
        before = [dict(m) for m in messages]
        self.assertEqual(trim(messages), 0)          # saving a few hundred tokens would resend ~10k of arguments
        self.assertEqual(messages, before)
        small = messages[:3] + _round('read_file', {'path': 'a.py'}, read)
        record = {'role': 'system', 'content': 'Runtime progress record. ' + 'r' * 40000}
        self.assertEqual(trim(small, after=[record]), 0)    # nor when the record that follows is large
        self.assertGreater(trim(small), 0)
