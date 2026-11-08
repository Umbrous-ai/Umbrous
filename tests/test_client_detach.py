"""A lost observer must not cancel accepted work or prevent explicit deletion."""
import http.client
import json
import socket
import time
import unittest

from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime


class ClientDetachTests(unittest.TestCase):
    def detach(self, rt, cid):
        conn = http.client.HTTPConnection('127.0.0.1', rt.port, timeout=5)
        conn.request('POST', '/api/chat', json.dumps({
            'chat_id': cid, 'model': 'slow:3b', 'message': 'Keep working'}), {
            'Content-Type': 'application/json', 'Origin': rt.origin,
            'X-Umbrous-Token': rt.policy.session_token})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        run_id = None
        while True:
            event = json.loads(response.readline())
            run_id = event.get('run_id', run_id)
            if event['type'] == 'delta':
                break
        # HTTP/1.0 transfers socket ownership to the response.
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        conn.close()
        return run_id

    def test_detached_reply_finishes_and_is_saved_once(self):
        reply = 'background work ' * 35
        fake = FakeOllama(models=[FakeModel('slow:3b', reply=reply, chunk_delay=0.01)]).start()
        self.addCleanup(fake.stop)
        with RunningRuntime(ollama_url=fake.url) as rt:
            cid = rt.new_chat()
            self.detach(rt, cid)
            until = time.monotonic() + 10
            while time.monotonic() < until:
                messages = rt.get_json('/api/chats/' + cid)[1]['messages']
                if len(messages) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[1]['status'], 'complete')
            self.assertEqual(messages[1]['content'], reply)
            self.assertEqual(len([r for r in fake.requests if r['path'] == '/api/chat']), 1)

    def test_detached_reply_can_still_be_deleted(self):
        fake = FakeOllama(models=[FakeModel('slow:3b', reply='work ' * 1000, chunk_delay=0.01)]).start()
        self.addCleanup(fake.stop)
        with RunningRuntime(ollama_url=fake.url) as rt:
            cid = rt.new_chat()
            self.detach(rt, cid)
            status, _, body = rt.post_json('/api/delete', {'kind': 'chat', 'id': cid})
            self.assertEqual(status, 200, body)
            self.assertEqual(rt.get_json('/api/chats/' + cid)[0], 404)
            self.assertFalse(rt.runtime.runs._runs)

    def test_detached_reply_obeys_explicit_stop(self):
        fake = FakeOllama(models=[FakeModel('slow:3b', reply='work ' * 1000, chunk_delay=0.01)]).start()
        self.addCleanup(fake.stop)
        with RunningRuntime(ollama_url=fake.url) as rt:
            cid = rt.new_chat()
            run_id = self.detach(rt, cid)
            self.assertIsNotNone(run_id)
            self.assertEqual(rt.post_json('/api/chat/cancel', {'run_id': run_id})[0], 200)
            until = time.monotonic() + 5
            while time.monotonic() < until:
                messages = rt.get_json('/api/chats/' + cid)[1]['messages']
                if len(messages) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[1]['status'], 'stopped')
