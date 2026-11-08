import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fake_ollama import FakeModel, FakeOllama, tool_results
from tests.helpers import RunningRuntime
from umbrous.workspace_resolution import MARKER, discover, resolve
from umbrous.model.base import ChatDelta, ChatToolCalls
from umbrous.context.assemble import assemble


class ResolutionTests(unittest.TestCase):
    def test_followup_selects_source_and_edits_in_same_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Umbrous'
            root.mkdir()
            (root / 'note.txt').write_text('old')
            def agent(messages):
                if MARKER in messages[0]['content']:
                    self.assertIn('improve Umbrous', str(messages))
                    self.assertIn('do all of them', str(messages))
                    return {'tool_calls': [('select_workspace', {'candidate': 0, 'intent': 'edit'})]}
                results = tool_results(messages)
                if not results:
                    return {'tool_calls': [('read_file', {'path': 'note.txt'})]}
                if len(results) == 1:
                    return {'tool_calls': [('edit_file', {'path': 'note.txt', 'old_text': 'old', 'new_text': 'improved'})]}
                return {'content': 'Updated note.txt.'}
            fake = FakeOllama(models=[FakeModel('resolver:3b', agent=agent)]).start()
            self.addCleanup(fake.stop)
            with RunningRuntime(ollama_url=fake.url) as rt:
                rt.post_json('/api/models/probe', {'model': 'resolver:3b'})
                cid = rt.new_chat()
                from umbrous.history.store import HistoryStore
                HistoryStore(rt.home / 'umbrous.db').append_exchange(cid, 'How would you improve Umbrous?',
                    'I would improve its status and permissions.', 'resolver:3b', 'complete')
                with patch('umbrous.workspace_resolution.discover', return_value=[{
                    'root': str(root), 'name': 'Umbrous', 'umbrous_source': True}]):
                    events = rt.chat(cid, 'resolver:3b', 'do all of them')
                self.assertEqual((root / 'note.txt').read_text(), 'improved')
                self.assertTrue(next(e for e in events if e['type'] == 'start')['tools']['selected']['inferred'])
                self.assertEqual(next(e for e in events if e['type'] == 'start')['tools']['profile'], 'code_ask_commands')
                self.assertIsNotNone(events[-1]['task_id'])

    def test_stop_during_resolution_does_not_attach(self):
        fake = FakeOllama(models=[FakeModel('resolver:3b')]).start()
        self.addCleanup(fake.stop)
        with tempfile.TemporaryDirectory() as tmp, RunningRuntime(ollama_url=fake.url) as rt:
            rt.post_json('/api/models/probe', {'model': 'resolver:3b'})
            def stopped(adapter, model, plan, candidates, options, cancel, deadlines):
                cancel.cancel()
                return (tmp, 'edit'), None
            with patch('umbrous.workspace_resolution.resolve', side_effect=stopped):
                events = rt.chat(rt.new_chat(), 'resolver:3b', 'work on yourself')
            self.assertEqual(events[0]['type'], 'workspace_resolving')
            self.assertTrue(events[0]['run_id'])
            self.assertEqual(events[-1]['code'], 'cancelled')
            self.assertIsNone(rt.runtime.access.workspace('prj_general'))

    def test_invalid_or_ambiguous_selection_does_not_choose(self):
        class Adapter:
            def chat_stream(self, *args, **kwargs):
                return iter(self.pieces)
        adapter = Adapter()
        plan = assemble('system', [], 'work on it', recent_turns=4, context_tokens=4096, reply_tokens=512)
        for calls in [(('select_workspace', {'candidate': -1, 'intent': 'edit'}),),
                      (('select_workspace', {'candidate': 0, 'intent': 'full_access'}),),
                      (('select_workspace', {'candidate': True, 'intent': 'edit'}),),
                      (('run_command', {'command': 'bad'}),)]:
            adapter.pieces = [ChatToolCalls(calls)]
            choice, pieces = resolve(adapter, 'test', plan, [{'root': '/tmp/a'}], {}, None, None)
            self.assertIsNone(choice)
            self.assertIn('Which project', pieces[0].text)
        adapter.pieces = [ChatDelta('Which of the two projects do you mean?')]
        choice, pieces = resolve(adapter, 'test', plan, [], {}, None, None)
        self.assertIsNone(choice)
        self.assertEqual(pieces, adapter.pieces)

    def test_discovery_finds_nested_repo_and_deduplicates(self):
        from umbrous.persistence import db
        from tests.helpers import project_fixture_db
        from umbrous.gate.access import AccessStore
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / 'data'; data.mkdir()
            path = data / 'umbrous.db'; project_fixture_db(path).close()
            root = home / 'workspace/github.com/me/Umbrous'
            (root / '.git').mkdir(parents=True)
            access = AccessStore(path, data)
            access.attach('prj_general', str(root))
            with patch('umbrous.selfsource.RUNNING_FROM', home / 'installed'):
                found = discover(access, home)
            self.assertEqual([x['root'] for x in found], [str(root.resolve())])
