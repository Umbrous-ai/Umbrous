"""Compatibility stages/cancellation and truthful native context data."""
import http.server
import json
import threading
import time
import unittest
from tests.fake_ollama import FakeOllama, FakeModel
from tests.helpers import RunningRuntime
from umbrous.model.base import CancelToken, ModelError
from umbrous.model.ollama import OllamaAdapter
from umbrous.model.probe import run_probe


class SetupTests(unittest.TestCase):
    def test_probe_stages_and_authenticated_jobs(self):
        fake = FakeOllama(models=[FakeModel('good:3b'), FakeModel('bad:3b', tools='text')]).start()
        try:
            stages = []
            result = run_probe(OllamaAdapter(fake.url), 'good:3b', progress=lambda stage, _: stages.append(stage), cancel=CancelToken())
            self.assertTrue(result.passed)
            self.assertEqual(stages, ['ask', 'answer'])
            with RunningRuntime(ollama_url=fake.url) as rt:
                token = rt.policy.session_token
                def post(path, body):
                    status, _, data = rt.post_json(path, body, token=token)
                    return status, json.loads(data)
                for model, passed in [('good:3b', True), ('bad:3b', False)]:
                    status, job = post('/api/models/probe/start', {'model':model})
                    self.assertEqual(status, 200)
                    until = time.monotonic() + 5
                    while not job['done'] and time.monotonic() < until:
                        time.sleep(.02)
                        _, job = post('/api/models/probe/status', {'id':job['id']})
                    self.assertTrue(job['done'])
                    self.assertEqual(job['passed'], passed)
                status, _ = post('/api/models/probe/status', {'id':'missing'})
                self.assertEqual(status, 404)
        finally:
            fake.stop()

    def test_cancel_interrupts_a_blocked_provider_read(self):
        entered, release = threading.Event(), threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                entered.set()
                release.wait(3)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        token, errors = CancelToken(), []
        def call():
            try:
                OllamaAdapter('http://127.0.0.1:%s' % server.server_port).chat_once('m', [], timeout=180, cancel=token)
            except ModelError as e:
                errors.append(e)
        worker = threading.Thread(target=call)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            token.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_cancelled_job_does_not_overwrite_verified_compatibility(self):
        from umbrous.model.probe_jobs import ProbeJobs
        from umbrous.model.base import Cancelled
        entered = threading.Event()
        class Router:
            def probe(self, model, cancel, progress):
                progress('ask', 'Waiting for tool call')
                entered.set()
                cancel._event.wait(2)
                raise Cancelled('Stopped')
        jobs = ProbeJobs(Router())
        job = jobs.start('test')
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ValueError): jobs.start('other')
        jobs.cancel(job['id'])
        until = time.monotonic() + 1
        while not jobs.view(job['id'])['done'] and time.monotonic() < until: time.sleep(.01)
        self.assertEqual(jobs.view(job['id'])['stage'], 'cancelled')
        self.assertFalse(jobs.view(job['id'])['passed'])
        jobs.close()

    def test_context_shows_only_successful_actions_in_selected_chat(self):
        with RunningRuntime() as rt:
            cid, other = rt.new_chat(), rt.new_chat()
            store = rt.runtime.tasks
            for index, (chat, tool, status, path) in enumerate([
                (cid, 'edit_file', 'completed', 'changed.py'),
                (cid, 'write_file', 'failed', 'failed.py'),
                (cid, 'read_file', 'completed', 'source.py'),
                (other, 'edit_file', 'completed', 'other.py'),
            ]):
                tid, aid = 'tsk_%016x' % index, 'act_%016x' % index
                store.create(tid, 'prj_general', chat, 'model', {}, workspace='/workspace')
                store.add_action(tid, aid, 1, tool, path)
                store.action_status(aid, 'started')
                store.action_status(aid, status)
            status, data = rt.get_json('/api/tasks?chat_id=' + cid)
            self.assertEqual(status, 200)
            self.assertEqual([x['path'] for x in data['activity']['changes']], ['changed.py'])
            self.assertEqual([x['path'] for x in data['activity']['sources']], ['source.py'])
