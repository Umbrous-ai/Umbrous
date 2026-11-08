"""Provider routing and per-request controls through Umbrous's authenticated API."""
import json
import threading
import unittest

from tests.helpers import RunningRuntime
from umbrous.model.base import ChatDelta, ChatDone, ModelInfo, ProviderError
from umbrous.model.settings import GenerationSettings


class Subscription:
    def __init__(self):
        self.calls = []
        self.closed = False

    def status(self):
        return {"available": True, "authenticated": True, "auth_type": "chatgpt"}

    def list_models(self):
        return [ModelInfo("codex/test", "test-digest", provider="codex", display_name="Test model",
                          reasoning_efforts=("low", "high"), default_reasoning_effort="low",
                          speeds=("standard", "fast"))]

    def show(self, model):
        return model

    def chat_stream(self, model, messages, *, options, **kwargs):
        self.calls.append((model, dict(options)))
        yield ChatDelta(options["reasoning_effort"] + "/" + options["speed"])
        yield ChatDone(model)

    def start_login(self):
        return {"auth_url": "https://auth.openai.com/authorize?state=opaque"}

    def close(self):
        self.closed = True


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.rt = RunningRuntime(ollama_url="http://127.0.0.1:1")
        self.rt.__enter__()
        self.codex = Subscription()
        self.rt.runtime.router.codex = self.codex

    def tearDown(self):
        self.rt.__exit__(None, None, None)

    def send(self, **overrides):
        body = {"chat_id": self.rt.new_chat(), "model": "codex/test", "message": "A synthetic greeting",
                "use_memory": False, **overrides}
        status, _, data = self.rt.post_json("/api/chat", body)
        self.assertEqual(status, 200)
        return [json.loads(line) for line in data.splitlines()]

    def test_cloud_catalog_works_when_local_provider_is_offline(self):
        status, result = self.rt.get_json("/api/models")
        self.assertEqual(status, 200)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["providers"]["ollama"]["reachable"])
        model = result["models"][0]
        self.assertEqual(model["name"], "codex/test")
        self.assertEqual(model["reasoning_efforts"], ["low", "high"])
        self.assertEqual(model["speeds"], ["standard", "fast"])
        self.assertFalse(model["tool_capable"])

    def test_selected_settings_reach_exact_provider_and_start_event(self):
        events = self.send(reasoning_effort="high", speed="fast")
        self.assertEqual(events[0]["generation"], {"reasoning_effort": "high", "speed": "fast"})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["model"], "codex/test")
        self.assertEqual(self.codex.calls[0][0], "codex/test")
        self.assertEqual(self.codex.calls[0][1]["speed"], "fast")

    def test_defaults_and_concurrent_requests_do_not_share_settings(self):
        outcomes = []
        threads = [threading.Thread(target=lambda **kw: outcomes.append(self.send(**kw)), kwargs=kw)
                   for kw in ({"reasoning_effort": "high", "speed": "fast"}, {})]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual({(opts["reasoning_effort"], opts["speed"]) for _, opts in self.codex.calls},
                         {("high", "fast"), ("low", "standard")})

    def test_unavailable_models_and_settings_never_fall_back(self):
        for args in ({"model": "codex/missing"}, {"reasoning_effort": "ultra"}, {"speed": "turbo"},
                     {"speed": True}, {"reasoning_effort": []}):
            with self.subTest(args=args):
                events = self.send(**args)
                self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.codex.calls, [])

    def test_login_requires_session_and_same_origin_and_json(self):
        for headers in ({}, {"X-Umbrous-Token": self.rt.policy.session_token, "Origin": "https://evil.invalid"}):
            status, _, _ = self.rt.request("POST", "/api/providers/codex/login", headers=headers, body=b"{}")
            self.assertIn(status, (401, 403))
        status, _, data = self.rt.post_json("/api/providers/codex/login", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["auth_url"].startswith("https://auth.openai.com/"))
        self.assertEqual(self.rt.get_json("/api/providers/codex")[1]["auth_type"], "chatgpt")

    def test_local_models_reject_cloud_only_controls(self):
        local = ModelInfo("local", "digest")
        self.assertEqual(GenerationSettings.parse().for_model(local), {})
        with self.assertRaises(ProviderError):
            GenerationSettings.parse("high", "fast").for_model(local)


if __name__ == "__main__":
    unittest.main()
