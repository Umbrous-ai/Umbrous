"""A small stand-in for Ollama's HTTP API, for tests and demos.

It speaks the same endpoints Umbrous uses (/api/version, /api/tags, /api/show,
/api/chat) and can misbehave on purpose: refuse tools, stall, die mid-reply.

    python3 -m tests.fake_ollama --port 11434     # run it by hand for a demo
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional


@dataclass
class FakeModel:
    name: str
    tools: str = "good"          # good | text | unsupported | wrong_tool | ignores_result
    behavior: str = "normal"     # normal | die_midstream | slow_first | stall_mid | not_found
    reply: Optional[str] = None  # fixed reply; default echoes the last user message
    context_length: int = 32768
    chunk_delay: float = 0.0
    digest: str = ""

    def __post_init__(self):
        if not self.digest:
            self.digest = format(abs(hash(self.name)) % (16 ** 12), "012x") + "0" * 52


@dataclass
class FakeOllama:
    models: List[FakeModel] = field(default_factory=lambda: [
        FakeModel("qwen2.5:3b"), FakeModel("tiny-chat:1b", tools="text")])
    host: str = "127.0.0.1"
    port: int = 0
    stall_seconds: float = 3.0
    requests: List[dict] = field(default_factory=list)

    def model(self, name: str) -> Optional[FakeModel]:
        return next((m for m in self.models if m.name == name), None)

    def start(self) -> "FakeOllama":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.0.0-fake"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [
                        {"name": m.name, "model": m.name, "digest": m.digest, "size": 1_900_000_000,
                         "details": {"family": "fake", "parameter_size": "3B", "quantization_level": "Q4_K_M"}}
                        for m in fake.models]})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.requests.append({"path": self.path, "body": body})
                m = fake.model(body.get("model", ""))
                if m is None or m.behavior == "not_found":
                    return self._json(404, {"error": f"model '{body.get('model')}' not found"})
                if self.path == "/api/show":
                    caps = ["completion"] + (["tools"] if m.tools != "unsupported" else [])
                    return self._json(200, {"model_info": {"fake.context_length": m.context_length},
                                            "capabilities": caps})
                if self.path == "/api/chat":
                    if body.get("tools"):
                        return self._tools(m, body)
                    if body.get("stream", True):
                        return self._stream(m, body)
                    return self._json(200, {"model": m.name, "message": {"role": "assistant", "content": self._text(m, body)},
                                            "done": True})
                self._json(404, {"error": "not found"})

            def _text(self, m, body):
                if m.reply is not None:
                    return m.reply
                last = next((x["content"] for x in reversed(body["messages"]) if x["role"] == "user"), "")
                return f"You said: {last}"

            def _tools(self, m, body):
                msgs = body["messages"]
                if m.tools == "unsupported":
                    return self._json(400, {"error": f"registry.ollama.ai/library/{m.name} does not support tools"})
                if m.tools == "text":
                    return self._json(200, {"model": m.name, "done": True,
                                            "message": {"role": "assistant", "content": "The code is probably 1234."}})
                if msgs[-1]["role"] == "tool":
                    if m.tools == "ignores_result":
                        content = "I could not find the code."
                    else:
                        code = json.loads(msgs[-1]["content"])["code"]
                        content = f"The probe code is {code}."
                    return self._json(200, {"model": m.name, "done": True, "message": {"role": "assistant", "content": content}})
                name = "get_weather" if m.tools == "wrong_tool" else body["tools"][0]["function"]["name"]
                return self._json(200, {"model": m.name, "done": True, "message": {
                    "role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": {}}}]}})

            def _stream(self, m, body):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(obj):
                    data = (json.dumps(obj) + "\n").encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                text = self._text(m, body)
                pieces = re.findall(r".{1,6}", text, re.S) or [""]
                try:
                    if m.behavior == "slow_first":
                        time.sleep(fake.stall_seconds)
                    for i, p in enumerate(pieces):
                        if m.behavior == "die_midstream" and i == len(pieces) // 2:
                            self.wfile.flush()
                            self.connection.shutdown(2)  # hang up without a final chunk
                            return
                        if m.behavior == "stall_mid" and i == len(pieces) // 2:
                            time.sleep(fake.stall_seconds)
                        send({"model": m.name, "message": {"role": "assistant", "content": p}, "done": False})
                        if m.chunk_delay:
                            time.sleep(m.chunk_delay)
                    prompt_chars = sum(len(x["content"]) for x in body["messages"])
                    send({"model": m.name, "message": {"role": "assistant", "content": ""}, "done": True,
                          "done_reason": "stop", "prompt_eval_count": prompt_chars // 4, "eval_count": len(pieces)})
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # clients hanging up mid-test are expected

        self.server = QuietServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def demo_models() -> List[FakeModel]:
    """The models the browser check and the demo use."""
    return [
        FakeModel("qwen2.5:3b", chunk_delay=0.05),
        FakeModel("tiny-chat:1b", tools="text", chunk_delay=0.05),
        FakeModel("html-test:1b", tools="unsupported",
                  reply='Here is HTML: <img src=x onerror="alert(1)"><script>alert(2)</script> '
                        '<a href="http://127.0.0.1:8417/api/health">health</a> and https://example.com/docs ok'),
        FakeModel("slow-model:7b", chunk_delay=0.4, reply="This reply streams slowly so you can press stop. " * 4),
    ]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    fake = FakeOllama(host=a.host, port=a.port, models=demo_models()).start()
    print(f"fake Ollama on {a.host}:{fake.port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.stop()
