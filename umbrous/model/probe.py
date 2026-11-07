"""Tool-call probe: does this model really do a tool-call round trip?

Ollama can *advertise* that a model supports tools, but M3 needs proof before
it lets a model drive the read-only tools. The probe runs a real round trip
with a harmless test tool:

  1. ask: offer one tool, `get_probe_code`, and ask the model to use it.
     Pass if the reply contains a tool call with exactly that name.
  2. answer: send back a random code as the tool's result and ask for it.
     Pass if the final answer contains that exact code.

The code is random each time, so a model cannot pass by guessing or by having
seen the probe before. Nothing runs on your computer: the "tool" is just this
function returning the code.

A model that fails stays usable for plain chat. It is labelled, and M3 will not
let it start tool work.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from umbrous.model.base import ModelError, ToolsUnsupported
from umbrous.model.ollama import OllamaAdapter

TOOL_NAME = "get_probe_code"
TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Returns the secret probe code. Call this to learn the code.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
SYSTEM = "You can call tools. When a tool can answer the question, call it instead of guessing."
QUESTION = f"What is the probe code? Use the {TOOL_NAME} tool to find out, then reply with the code only."


@dataclass(frozen=True)
class ProbeResult:
    model: str
    passed: bool
    stage: str      # "ask", "answer" or "done": how far the model got
    detail: str     # one sentence, our words (never the model's raw output)
    ms: int


def run_probe(adapter: OllamaAdapter, model: str, timeout: float = 180.0) -> ProbeResult:
    started = time.monotonic()
    code = "UMB-" + secrets.token_hex(3).upper()
    options = {"temperature": 0}

    def done(passed: bool, stage: str, detail: str) -> ProbeResult:
        return ProbeResult(model, passed, stage, detail, int((time.monotonic() - started) * 1000))

    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": QUESTION}]
    try:
        first = adapter.chat_once(model, messages, tools=[TOOL], options=options, timeout=timeout)
    except ToolsUnsupported:
        return done(False, "ask", "Ollama says this model does not support tool calls.")
    except ModelError as exc:
        return done(False, "ask", f"The probe could not run: {exc.message}")

    msg = first.get("message") or {}
    calls = msg.get("tool_calls") or []
    names = [((c.get("function") or {}).get("name")) for c in calls if isinstance(c, dict)]
    if TOOL_NAME not in names:
        if names:
            return done(False, "ask", f"The model called a tool that does not exist ({len(names)} call(s)), not {TOOL_NAME}.")
        return done(False, "ask", "The model answered in text instead of calling the tool.")

    call = next(c for c in calls if (c.get("function") or {}).get("name") == TOOL_NAME)
    messages += [
        {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [call]},
        {"role": "tool", "content": json.dumps({"code": code}), "tool_name": TOOL_NAME},
    ]
    try:
        second = adapter.chat_once(model, messages, tools=[TOOL], options=options, timeout=timeout)
    except ModelError as exc:
        return done(False, "answer", f"The model asked for the tool but the follow-up failed: {exc.message}")
    answer = (second.get("message") or {}).get("content") or ""
    if code in answer:
        return done(True, "done", "Called the test tool and used its result in the answer.")
    return done(False, "answer", "Called the test tool but did not use its result in the answer.")
