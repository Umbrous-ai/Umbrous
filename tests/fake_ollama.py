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
from typing import Callable, List, Optional


@dataclass
class FakeModel:
    name: str
    tools: str = "good"          # good | text | unsupported | wrong_tool | ignores_result
    behavior: str = "normal"     # normal | die_midstream | slow_first | stall_mid | not_found
    reply: Optional[str] = None  # fixed reply; default echoes the last user message
    context_length: int = 32768
    chunk_delay: float = 0.0
    digest: str = ""
    # M3: when tools are offered in a streamed chat, this decides the next step:
    # agent(messages) -> {"tool_calls": [(name, args), ...]} or {"content": "..."}
    agent: Optional[Callable[[list], dict]] = None

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
    down: bool = False                      # hang up on every request, like a stopped Ollama
    hidden: List[str] = field(default_factory=list)  # installed-model names to hide from /api/tags

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

            def _hang_up(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass

            def do_GET(self):
                if fake.down:
                    return self._hang_up()
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.0.0-fake"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [
                        {"name": m.name, "model": m.name, "digest": m.digest, "size": 1_900_000_000,
                         "details": {"family": "fake", "parameter_size": "3B", "quantization_level": "Q4_K_M"}}
                        for m in fake.models if m.name not in fake.hidden]})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/_control":  # test hook: {"down": bool, "hidden": [names]}
                    fake.down = bool(body.get("down", fake.down))
                    fake.hidden = list(body.get("hidden", fake.hidden))
                    return self._json(200, {"down": fake.down, "hidden": fake.hidden})
                if fake.down:
                    return self._hang_up()
                fake.requests.append({"path": self.path, "body": body})
                m = fake.model(body.get("model", ""))
                if m is not None and m.name in fake.hidden:
                    m = None
                if m is None or m.behavior == "not_found":
                    return self._json(404, {"error": f"model '{body.get('model')}' not found"})
                if self.path == "/api/show":
                    caps = ["completion"] + (["tools"] if m.tools != "unsupported" else [])
                    return self._json(200, {"model_info": {"fake.context_length": m.context_length},
                                            "capabilities": caps})
                if self.path == "/api/chat":
                    if m.agent is not None and body.get("stream", True):
                        return self._agent_stream(m, body)
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

            def _agent_stream(self, m, body):
                if body.get("tools"):
                    step = m.agent(body["messages"])
                else:  # no tools offered: the agent must answer in text
                    step = m.agent(body["messages"] + [{"role": "system", "content": "__no_tools__"}])
                    if "tool_calls" in step:
                        step = {"content": "(I wanted another tool but none are available.)"}
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(obj):
                    data = (json.dumps(obj) + "\n").encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                try:
                    if "tool_calls" in step:
                        send({"model": m.name, "done": False, "message": {"role": "assistant", "content": "",
                              "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in step["tool_calls"]]}})
                    else:
                        parts = re.findall(r".{1,12}", step["content"], re.S) or [""]
                        for i, p in enumerate(parts):
                            if m.behavior == "stall_mid" and i == len(parts) // 2:
                                time.sleep(fake.stall_seconds)   # a model that hangs mid-answer
                            send({"model": m.name, "message": {"role": "assistant", "content": p}, "done": False})
                            if m.chunk_delay:
                                time.sleep(m.chunk_delay)
                    prompt_chars = sum(len(x.get("content") or "") for x in body["messages"])
                    send({"model": m.name, "message": {"role": "assistant", "content": ""}, "done": True,
                          "done_reason": "stop", "prompt_eval_count": prompt_chars // 4, "eval_count": 5})
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

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


def tool_results(messages: list) -> List[str]:
    return [x["content"] for x in messages if x.get("role") == "tool"]


def data_lines(tool_text: str) -> List[str]:
    """The lines between the wrapper markers, without the header line."""
    if "<<<BEGIN WORKSPACE DATA>>>" not in tool_text:
        return [tool_text]
    inner = tool_text.split("<<<BEGIN WORKSPACE DATA>>>\n", 1)[1].split("\n<<<END WORKSPACE DATA>>>", 1)[0]
    return inner.split("\n")[1:]


def scripted(*steps, final: Optional[Callable[[list], str]] = None) -> Callable[[list], dict]:
    """An agent that makes the given tool calls, one per model turn, then answers.
    A step is (name, args) or a list of them (several calls in one turn)."""
    def agent(messages):
        done = sum(1 for x in messages if x.get("role") == "assistant" and x.get("tool_calls"))
        no_tools = messages and messages[-1].get("content") == "__no_tools__"
        if done < len(steps) and not no_tools:
            step = steps[done]
            return {"tool_calls": step if isinstance(step, list) else [step]}
        if final:
            return {"content": final(messages)}
        last = tool_results(messages)[-1] if tool_results(messages) else ""
        return {"content": "From the workspace: " + " | ".join(data_lines(last)[:3])}
    return agent


def workspace_agent(messages: list) -> dict:
    """The demo's stand-in for a real tool-using model. "read <path>", "list
    [path]" or anything else (searched for its longest word), then it answers
    with what the tool returned and names the source."""
    if messages and messages[-1].get("content") == "__no_tools__" or tool_results(messages):
        results = tool_results(messages)
        last = results[-1] if results else ""
        if not last.startswith("UNTRUSTED"):
            return {"content": f"I could not do that: {last}"}
        head = last.split("<<<BEGIN WORKSPACE DATA>>>\n", 1)[1].split("\n", 1)[0]
        lines = [l for l in data_lines(last) if l.strip()][:4]
        return {"content": "Here is what I found (" + head + "):\n" + "\n".join(lines)}
    q = next((x["content"] for x in reversed(messages) if x.get("role") == "user"), "")
    words = q.split()
    if words and words[0].lower() == "read" and len(words) > 1:
        return {"tool_calls": [("read_file", {"path": words[1]})]}
    if words and words[0].lower() == "list":
        return {"tool_calls": [("list_files", {"path": words[1] if len(words) > 1 else "."})]}
    word = max((w.strip("?.,!") for w in words), key=len, default="")
    return {"tool_calls": [("search_files", {"query": word})]}


def demo_models() -> List[FakeModel]:
    """The models the browser check and the demo use."""
    return [
        FakeModel("qwen2.5:3b", chunk_delay=0.05),
        FakeModel("tiny-chat:1b", tools="text", chunk_delay=0.05),
        FakeModel("html-test:1b", tools="unsupported",
                  reply='Here is HTML: <img src=x onerror="alert(1)"><script>alert(2)</script> '
                        '<a href="http://127.0.0.1:8417/api/health">health</a> and https://example.com/docs ok'),
        FakeModel("slow-model:7b", chunk_delay=0.4, reply="This reply streams slowly so you can press stop. " * 4),
        FakeModel("workspace-reader:3b", agent=workspace_agent, chunk_delay=0.02),
        # M4: a three-step task (list, search, read), then an answer with one
        # citation that was read and one that was not, for the report's Verify step.
        FakeModel("multi-step:3b", chunk_delay=0.02, agent=scripted(
            ("list_files", {}), ("search_files", {"query": "Ship"}), ("read_file", {"path": "notes/plan.md"}),
            final=lambda m: "The plan is to ship on Friday (notes/plan.md:2). Owner details are in notes/team.md:4.")),
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
