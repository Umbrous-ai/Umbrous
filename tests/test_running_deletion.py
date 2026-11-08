"""Deleting an active container stops its work, then removes its data atomically."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.history.store import HistoryError, HistoryStore
from umbrous.persistence import db
from umbrous.reset import Reset
from umbrous.runloop.coordination import ChatLocks, RunRegistry


class RunningDeletionTests(unittest.TestCase):
    def test_delete_chat_stops_only_its_reply_and_cannot_resurrect_it(self):
        fake = FakeOllama(models=[FakeModel('slow:3b', reply='streaming ' * 1000, chunk_delay=0.01)]).start()
        self.addCleanup(fake.stop)
        with RunningRuntime(ollama_url=fake.url) as rt:
            a, b = rt.new_chat(), rt.new_chat()
            results = {}
            def reply(cid):
                results[cid] = rt.chat(cid, 'slow:3b', 'Keep working')
            threads = [threading.Thread(target=reply, args=(cid,)) for cid in (a, b)]
            for thread in threads: thread.start()
            until = time.monotonic() + 5
            while len([r for r in fake.requests if r['path'] == '/api/chat']) < 2 and time.monotonic() < until:
                time.sleep(0.01)
            status, _, body = rt.post_json('/api/delete', {'kind': 'chat', 'id': a})
            self.assertEqual(status, 200, body)
            threads[0].join(5)
            self.assertFalse(threads[0].is_alive())
            self.assertTrue(threads[1].is_alive(), 'deleting A must not stop B')
            self.assertEqual(rt.get_json('/api/chats/' + a)[0], 404)
            self.assertEqual(rt.get_json('/api/chats/' + b)[0], 200)
            self.assertTrue(any(e.get('code') == 'cancelled' for e in results[a]))
            # The same one-step deletion can remove the last project and its running chat.
            status, _, body = rt.post_json('/api/delete', {'kind': 'project', 'id': 'prj_general'})
            self.assertEqual(status, 200, body)
            threads[1].join(5)
            self.assertFalse(threads[1].is_alive())
            self.assertEqual(rt.get_json('/api/projects')[1]['projects'], [])
            self.assertEqual(rt.get_json('/api/chats/' + b)[0], 404)
            with rt.runtime.history._db() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM pending_turns').fetchone()[0], 0)
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_delete_running_coding_project_stops_its_command(self):
        from tests.test_m9_coding import make_project, models
        fake = FakeOllama(models=models()).start()
        self.addCleanup(fake.stop)
        with tempfile.TemporaryDirectory() as tmp, RunningRuntime(ollama_url=fake.url) as rt:
            source = make_project(Path(tmp))
            rt.runtime.access.attach('prj_general', str(source), 'workspace_code')
            rt.post_json('/api/models/probe', {'model': 'slow-coder:3b'})
            cid = rt.new_chat()
            results = []
            thread = threading.Thread(target=lambda: results.extend(rt.chat(cid, 'slow-coder:3b', 'add multiply')))
            thread.start()
            until = time.monotonic() + 10
            while not rt.runtime.commands.live() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(rt.runtime.commands.live(), 'coding task never started its command')
            status, _, body = rt.post_json('/api/delete', {'kind': 'project', 'id': 'prj_general'})
            self.assertEqual(status, 200, body)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(rt.runtime.commands.live())
            self.assertTrue((source / 'calc.py').exists())
            self.assertEqual(rt.get_json('/api/chats/' + cid)[0], 404)
            self.assertTrue(any(e.get('code') == 'cancelled' for e in results))

    def test_cancel_covers_run_registered_after_deletion_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'u.db'; db.connect(path).close()
            history = HistoryStore(path)
            cid = history.create_chat().id
            locks, runs = ChatLocks(), RunRegistry()
            entered = threading.Event()
            def late_run():
                with locks.get(cid):
                    entered.set()
                    time.sleep(0.15)
                    rid, token = runs.start(cid)
                    until = time.monotonic() + 3
                    while not token.cancelled and time.monotonic() < until:
                        time.sleep(0.01)
                    history.append_exchange(cid, 'request', 'partial', 'test', 'stopped')
                    runs.finish(rid)
            thread = threading.Thread(target=late_run)
            thread.start(); self.assertTrue(entered.wait(1))
            done = Reset(path).delete_target('chat', cid, locks, runs)
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(done['deleted']['chats'], 1)
            with self.assertRaises(HistoryError): history.get_chat(cid)

    def test_delete_project_preserves_source_and_other_projects(self):
        with tempfile.TemporaryDirectory() as tmp, RunningRuntime() as rt:
            source = Path(tmp); (source / 'keep.txt').write_text('keep')
            rt.runtime.access.attach('prj_general', str(source), 'workspace_code')
            other = rt.runtime.history.create_project('Keep')
            chat = rt.runtime.history.create_chat(other.id)
            status, _, body = rt.post_json('/api/delete', {'kind': 'project', 'id': 'prj_general'})
            self.assertEqual(status, 200, body)
            self.assertEqual((source / 'keep.txt').read_text(), 'keep')
            self.assertEqual(rt.get_json('/api/chats/' + chat.id)[0], 200)
            self.assertEqual(rt.post_json('/api/delete', {'kind': 'file', 'id': str(source)})[0], 400)
