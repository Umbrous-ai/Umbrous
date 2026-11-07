"""M1 acceptance: local model chat through a stand-in Ollama (tests/fake_ollama.py)."""

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama
from tests.helpers import RunningRuntime
from umbrous.config import ConfigError, load_config
from umbrous.context.assemble import MessageTooLong, Turn, assemble, estimate_tokens
from umbrous.model.base import (CancelToken, Cancelled, Deadlines, ModelNotInstalled, ModelTimeout,
                                ProviderUnavailable, StreamInterrupted)
from umbrous.model.exposure import check_exposure
from umbrous.model.ollama import OllamaAdapter
from umbrous.model.probe import run_probe
from umbrous.model.router import ModelRouter
from umbrous.persistence import db


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def types(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------- context
class ContextTests(unittest.TestCase):
    def history(self, n, size=40):
        return [Turn("user" if i % 2 == 0 else "assistant", f"m{i} " + "x" * size) for i in range(n)]

    def test_everything_fits(self):
        plan = assemble("sys", self.history(4), "hi", recent_turns=20, context_tokens=8192, reply_tokens=1024)
        self.assertEqual([m["role"] for m in plan.messages], ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(plan.turns_included, 4)
        self.assertEqual(plan.notices, [])
        self.assertEqual((plan.window, plan.budget), (8192, 7168))

    def test_recent_turn_limit_keeps_newest_and_says_so(self):
        plan = assemble("sys", self.history(10), "hi", recent_turns=4, context_tokens=8192, reply_tokens=1024)
        self.assertEqual(plan.turns_included, 4)
        self.assertEqual(plan.dropped_by_turn_limit, 6)
        self.assertTrue(plan.messages[1]["content"].startswith("m6 "))
        self.assertIn("6 earlier message(s) left out", plan.notices[0])

    def test_budget_drops_oldest_first_and_says_so(self):
        hist = self.history(10, size=400)  # ~104 tokens each
        plan = assemble("sys", hist, "hi", recent_turns=50, context_tokens=1024, reply_tokens=512)
        self.assertLess(plan.turns_included, 10)
        self.assertGreater(plan.dropped_by_budget, 0)
        self.assertLessEqual(plan.estimated_tokens, plan.budget)
        self.assertTrue(plan.messages[-2]["content"].startswith("m9 "))  # newest earlier turn kept
        self.assertIn("token budget", plan.notices[-1])

    def test_window_is_capped_by_the_model(self):
        plan = assemble("sys", [], "hi", recent_turns=5, context_tokens=32768, reply_tokens=1024, model_context=4096)
        self.assertEqual(plan.window, 4096)

    def test_message_too_long_is_refused_before_calling_the_model(self):
        with self.assertRaises(MessageTooLong) as err:
            assemble("sys", [], "y" * 20000, recent_turns=5, context_tokens=2048, reply_tokens=512)
        self.assertIn("too long", err.exception.message)

    def test_estimate(self):
        self.assertEqual(estimate_tokens(""), 4)
        self.assertEqual(estimate_tokens("abcd" * 10), 14)


# ---------------------------------------------------------------- config
class ConfigM1Tests(unittest.TestCase):
    def test_ollama_must_be_local(self):
        with tempfile.TemporaryDirectory() as d:
            for url in ("http://192.168.1.4:11434", "http://localhost:11434", "https://127.0.0.1:11434",
                        "http://api.example.com", "http://127.0.0.1:11434/v1"):
                with self.subTest(url=url), self.assertRaises(ConfigError):
                    load_config(Path(d), {"ollama_url": url})
            self.assertEqual(load_config(Path(d), {"ollama_url": "http://127.0.0.1:11500/"}).ollama_url,
                             "http://127.0.0.1:11500")

    def test_limits_are_validated(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"recent_turns": 0}, {"context_tokens": 10}, {"reply_tokens": "big"}, {"idle_timeout_s": 0}):
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    load_config(Path(d), bad)


# ---------------------------------------------------------------- adapter
class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(stall_seconds=2.0, models=[
            FakeModel("ok:1b"), FakeModel("dies:1b", behavior="die_midstream", reply="z" * 60),
            FakeModel("slow:1b", behavior="slow_first"), FakeModel("stall:1b", behavior="stall_mid", reply="w" * 60),
            FakeModel("drip:1b", chunk_delay=0.3, reply="d" * 120),
        ]).start()
        self.a = OllamaAdapter(self.fake.url)
        self.short = Deadlines(first_token=0.5, idle=0.5, total=30)

    def tearDown(self):
        self.fake.stop()

    def msgs(self):
        return [{"role": "user", "content": "hello"}]

    def test_stream_reports_actual_model(self):
        pieces = list(self.a.chat_stream("ok:1b", self.msgs()))
        self.assertEqual("".join(p.text for p in pieces[:-1]), "You said: hello")
        self.assertEqual(pieces[-1].model, "ok:1b")

    def test_ollama_down(self):
        a = OllamaAdapter(f"http://127.0.0.1:{free_port()}")
        with self.assertRaises(ProviderUnavailable):
            a.version()
        with self.assertRaises(ProviderUnavailable):
            list(a.chat_stream("ok:1b", self.msgs()))

    def test_model_not_installed(self):
        with self.assertRaises(ModelNotInstalled):
            list(self.a.chat_stream("nope:1b", self.msgs()))

    def test_interrupted_stream(self):
        with self.assertRaises(StreamInterrupted):
            list(self.a.chat_stream("dies:1b", self.msgs()))

    def test_first_token_timeout(self):
        with self.assertRaises(ModelTimeout) as err:
            list(self.a.chat_stream("slow:1b", self.msgs(), deadlines=self.short))
        self.assertIn("did not start", err.exception.message)

    def test_idle_timeout_mid_reply(self):
        with self.assertRaises(ModelTimeout) as err:
            list(self.a.chat_stream("stall:1b", self.msgs(), deadlines=self.short))
        self.assertIn("silent", err.exception.message)

    def test_total_timeout(self):
        with self.assertRaises(ModelTimeout) as err:
            list(self.a.chat_stream("drip:1b", self.msgs(), deadlines=Deadlines(total=0.5)))
        self.assertIn("limit", err.exception.message)

    def test_cancel_interrupts_a_blocked_read_quickly(self):
        token = CancelToken()
        threading.Timer(0.3, token.cancel).start()
        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            list(self.a.chat_stream("slow:1b", self.msgs(), cancel=token))  # would stall 2 s
        self.assertLess(time.monotonic() - t0, 1.5)


# ---------------------------------------------------------------- probe, router, exposure
class ProbeAndRouterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(models=[
            FakeModel("good:3b"), FakeModel("texty:1b", tools="text"), FakeModel("notools:1b", tools="unsupported"),
            FakeModel("wrongtool:1b", tools="wrong_tool"), FakeModel("forgetful:1b", tools="ignores_result"),
        ]).start()
        self.a = OllamaAdapter(self.fake.url)
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "u.db"
        db.connect(self.db_path).close()
        self.router = ModelRouter(self.a, self.db_path)

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def test_probe_outcomes(self):
        expected = {"good:3b": (True, "done"), "texty:1b": (False, "ask"), "notools:1b": (False, "ask"),
                    "wrongtool:1b": (False, "ask"), "forgetful:1b": (False, "answer")}
        for name, (passed, stage) in expected.items():
            with self.subTest(model=name):
                r = run_probe(self.a, name)
                self.assertEqual((r.passed, r.stage), (passed, stage), r.detail)

    def test_probe_uses_a_fresh_random_code(self):
        run_probe(self.a, "good:3b")
        run_probe(self.a, "good:3b")
        codes = [json.loads(r["body"]["messages"][-1]["content"])["code"]
                 for r in self.fake.requests if r["body"].get("messages", [{}])[-1].get("role") == "tool"]
        self.assertEqual(len(codes), 2)
        self.assertNotEqual(codes[0], codes[1])

    def test_probe_results_are_stored_per_digest(self):
        self.router.probe("good:3b")
        self.router.probe("texty:1b")
        self.assertTrue(self.router.tool_capable("good:3b"))
        self.assertFalse(self.router.tool_capable("texty:1b"))
        self.assertFalse(self.router.tool_capable("forgetful:1b"))  # never probed
        # Re-pulling a model changes its digest: the old result no longer counts.
        self.fake.model("good:3b").digest = "f" * 64
        self.assertFalse(self.router.tool_capable("good:3b"))

    def test_status_labels(self):
        self.router.probe("good:3b")
        by_name = {m["name"]: m for m in self.router.status()["models"]}
        self.assertTrue(by_name["good:3b"]["tool_capable"])
        self.assertIsNone(by_name["texty:1b"]["probe"])
        self.assertFalse(by_name["notools:1b"]["advertises_tools"])
        self.assertEqual(by_name["good:3b"]["context_length"], 32768)

    def test_no_fallback_when_the_chosen_model_is_missing(self):
        with self.assertRaises(ModelNotInstalled) as err:
            self.router.resolve("qwen2.5:7b")
        self.assertIn("will not switch", err.exception.message)

    def test_exposure_loopback_only(self):
        report = check_exposure(self.fake.port)
        self.assertEqual(report.status, "loopback_only")

    def test_exposure_not_running(self):
        self.assertEqual(check_exposure(free_port()).status, "not_running")

    def test_exposure_detects_ollama_listening_on_all_interfaces(self):
        exposed = FakeOllama(host="0.0.0.0").start()
        try:
            report = check_exposure(exposed.port)
            if not report.checked:
                self.skipTest("this machine has no non-loopback IPv4 address to try")
            self.assertEqual(report.status, "exposed")
            self.assertIn("Exposed", report.summary)
        finally:
            exposed.stop()

    def test_exposure_logic_with_a_stubbed_network(self):
        listening = {("127.0.0.1", 11434), ("192.168.1.20", 11434)}
        accepts = lambda addr, port, timeout: (addr, port) in listening  # noqa: E731
        r = check_exposure(11434, addresses=["192.168.1.20", "10.0.0.5"], accepts=accepts)
        self.assertEqual((r.status, r.reachable_on), ("exposed", ["192.168.1.20"]))
        self.assertIn("firewall", r.limits)


# ---------------------------------------------------------------- end to end through the API
class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(stall_seconds=3.0, models=[
            FakeModel("qwen2.5:3b", context_length=4096), FakeModel("tiny-chat:1b", tools="text"),
            FakeModel("dies:1b", behavior="die_midstream", reply="partial " * 10),
            FakeModel("slow:1b", behavior="slow_first"),
            FakeModel("drip:1b", chunk_delay=0.15, reply="drip " * 40),
            FakeModel("html:1b", reply='<img src=x onerror="alert(1)"><script>alert(2)</script>'),
        ]).start()

    def tearDown(self):
        self.fake.stop()

    def rt(self, **overrides):
        return RunningRuntime(ollama_url=self.fake.url, **overrides)

    def chat_requests(self):
        return [r["body"] for r in self.fake.requests if r["path"] == "/api/chat"]

    def test_models_endpoint(self):
        with self.rt() as rt:
            status, _, body = rt.request("GET", "/api/models", token=rt.policy.session_token)
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(data["reachable"])
            self.assertIn("qwen2.5:3b", [m["name"] for m in data["models"]])
            self.assertIn(data["exposure"]["status"], ("loopback_only", "exposed"))

    def test_chat_streams_and_reports_the_exact_model(self):
        with self.rt(context_tokens=8192) as rt:
            ev = rt.chat("chat_testtest01", "qwen2.5:3b", "hello there")
            self.assertEqual(ev[0]["type"], "start")
            self.assertEqual(ev[-1]["type"], "done")
            self.assertEqual(ev[-1]["model"], "qwen2.5:3b")
            self.assertEqual("".join(e["text"] for e in ev if e["type"] == "delta"), "You said: hello there")
            sent = self.chat_requests()[-1]
            self.assertEqual(sent["options"]["num_ctx"], 4096)  # min(8192, model's 4096): no silent truncation by Ollama
            self.assertEqual(sent["messages"][0]["role"], "system")

    def test_history_is_sent_on_the_next_turn(self):
        with self.rt() as rt:
            rt.chat("chat_testtest02", "qwen2.5:3b", "first")
            rt.chat("chat_testtest02", "qwen2.5:3b", "second")
            contents = [m["content"] for m in self.chat_requests()[-1]["messages"]]
            self.assertEqual(contents[1:], ["first", "You said: first", "second"])

    def test_recent_turn_limit_is_visible(self):
        with self.rt(recent_turns=2) as rt:
            for word in ("one", "two", "three"):
                ev = rt.chat("chat_testtest03", "qwen2.5:3b", word)
            ctx = ev[0]["context"]
            self.assertEqual((ctx["turns_available"], ctx["turns_included"], ctx["dropped_by_turn_limit"]), (4, 2, 2))
            self.assertIn("left out", ctx["notices"][0])
            self.assertEqual(len(self.chat_requests()[-1]["messages"]), 4)  # system + 2 turns + new message

    def test_ollama_down_is_reported_not_rerouted(self):
        with RunningRuntime(ollama_url=f"http://127.0.0.1:{free_port()}") as rt:
            ev = rt.chat("chat_testtest04", "qwen2.5:3b", "hi")
            self.assertEqual(types(ev), ["error"])
            self.assertEqual(ev[0]["code"], "ollama_unreachable")
            status, _, body = rt.request("GET", "/api/health", token=rt.policy.session_token)
            self.assertFalse(json.loads(body)["ollama"]["reachable"])

    def test_missing_model_is_not_replaced_by_another(self):
        with self.rt() as rt:
            ev = rt.chat("chat_testtest05", "llama3:70b", "hi")
            self.assertEqual(ev[0]["code"], "model_not_installed")
            self.assertEqual(self.chat_requests(), [])  # nothing was sent to any other model

    def test_interrupted_stream_ends_clearly_and_keeps_partial_text(self):
        with self.rt() as rt:
            ev = rt.chat("chat_testtest06", "dies:1b", "hi")
            self.assertEqual(ev[-1]["type"], "error")
            self.assertEqual(ev[-1]["code"], "stream_interrupted")
            self.assertTrue(ev[-1]["partial"])

    def test_timeout_ends_clearly(self):
        with self.rt(first_token_timeout_s=1) as rt:
            ev = rt.chat("chat_testtest07", "slow:1b", "hi")
            self.assertEqual(ev[-1]["code"], "timeout")
            self.assertFalse(ev[-1]["partial"])

    def test_oversized_message_is_refused_before_the_model(self):
        with self.rt(context_tokens=1024, reply_tokens=256) as rt:
            ev = rt.chat("chat_testtest08", "qwen2.5:3b", "z" * 8000)
            self.assertEqual(ev[0]["code"], "message_too_long")
            self.assertEqual(self.chat_requests(), [])

    def test_stop_button_cancels_the_reply(self):
        with self.rt() as rt:
            result = {}

            def send():
                result["events"] = rt.chat("chat_testtest09", "drip:1b", "go")

            t = threading.Thread(target=send)
            t.start()
            run_id = None
            for _ in range(100):
                time.sleep(0.05)
                runs = rt.runtime.runs._runs
                if runs:
                    run_id = next(iter(runs))
                    break
            time.sleep(0.4)  # let some text arrive
            status, _, body = rt.post_json("/api/chat/cancel", {"run_id": run_id})
            self.assertEqual(status, 200)
            t.join(5)
            ev = result["events"]
            self.assertEqual(ev[-1]["code"], "cancelled")
            self.assertTrue(ev[-1]["partial"])
            self.assertLess(len("".join(e["text"] for e in ev if e["type"] == "delta")), len("drip " * 40))

    def test_one_reply_at_a_time_per_chat(self):
        with self.rt() as rt:
            results = []
            ts = [threading.Thread(target=lambda: results.append(rt.chat("chat_testtest10", "drip:1b", "x")))
                  for _ in range(2)]
            for t in ts:
                t.start()
                time.sleep(0.1)
            for t in ts:
                t.join(15)
            codes = sorted(r[-1].get("code", "done") for r in results)
            self.assertEqual(codes, ["chat_busy", "done"])

    def test_html_reply_is_passed_on_as_plain_text(self):
        with self.rt() as rt:
            ev = rt.chat("chat_testtest11", "html:1b", "show html")
            text = "".join(e["text"] for e in ev if e["type"] == "delta")
            self.assertEqual(text, '<img src=x onerror="alert(1)"><script>alert(2)</script>')
            # Rendering it inert is the page's job: see test_page_bridge and the browser demo.

    def test_probe_endpoint_and_label(self):
        with self.rt() as rt:
            status, _, body = rt.post_json("/api/models/probe", {"model": "qwen2.5:3b"})
            self.assertEqual((status, json.loads(body)["passed"]), (200, True))
            status, _, body = rt.post_json("/api/models/probe", {"model": "tiny-chat:1b"})
            self.assertEqual((status, json.loads(body)["passed"]), (200, False))
            models = json.loads(rt.request("GET", "/api/models", token=rt.policy.session_token)[2])["models"]
            labels = {m["name"]: m["tool_capable"] for m in models}
            self.assertEqual((labels["qwen2.5:3b"], labels["tiny-chat:1b"]), (True, False))

    def test_api_requires_json_and_credential(self):
        with self.rt() as rt:
            self.assertEqual(rt.request("POST", "/api/chat", body=b"{}", token=rt.policy.session_token,
                                        headers={"Content-Type": "text/plain"})[0], 400)
            self.assertEqual(rt.request("POST", "/api/chat", body=b"{}", headers={"Content-Type": "application/json"})[0], 401)
            self.assertEqual(rt.post_json("/api/chat", {"chat_id": "chat_x", "model": "qwen2.5:3b", "message": "hi"},
                                          headers={"Origin": "https://evil.example"})[0], 403)

    def test_chat_text_is_never_logged(self):
        with self.rt() as rt:
            rt.chat("chat_testtest12", "qwen2.5:3b", "MY-PRIVATE-MESSAGE-4711")
            time.sleep(0.1)
            log = (rt.config.log_dir / "runtime.log").read_text()
            self.assertNotIn("MY-PRIVATE-MESSAGE-4711", log)
            self.assertIn('"run.end"', log)


if __name__ == "__main__":
    unittest.main()
