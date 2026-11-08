"""Measure what Umbrous sends to the model on four representative tasks.

    python3 -m tests.measure_tokens [--workspace DIR] [--json OUT]

Runs the real runtime in a throwaway home against tests/fake_ollama.py with
scripted agents: every request is exactly what Umbrous builds, only the model's
choices are fixed, so two versions can be compared request for request. The
workspace is a copy of DIR (default: this checkout); compare two versions with
the same --workspace. Counts tokens with tiktoken's o200k_base when it is
installed, otherwise with Umbrous's own estimate (about 4 characters a token).

input_tokens    everything sent, summed over every model request
uncached_tokens what differs from the previous request's start (what a server
                with prompt caching has to process again)
See docs/tokens.md.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))

from tests.fake_ollama import FakeModel, FakeOllama, coder, scripted  # noqa: E402
from tests.helpers import RunningRuntime  # noqa: E402
from tests.test_m9_coding import CALC, TESTS  # noqa: E402

try:
    import tiktoken
    _enc = tiktoken.get_encoding("o200k_base")
    TOKENIZER, encode = "o200k_base", _enc.encode
except ImportError:      # no third-party dependency needed: fall back to the runtime's estimate
    TOKENIZER = "estimate (4 characters per token)"

    def encode(text):
        return [text[i:i + 4] for i in range(0, len(text), 4)]
SOURCE = CHECKOUT


def render(body):
    """One request as the text a chat template would roughly see: tools, then messages."""
    parts = []
    if body.get("tools"):
        parts.append("TOOLS " + json.dumps(body["tools"], ensure_ascii=False, separators=(",", ":")))
    for m in body["messages"]:
        s = f"<{m['role']}>" + (m.get("content") or "")
        if m.get("tool_calls"):
            s += json.dumps(m["tool_calls"], ensure_ascii=False, separators=(",", ":"))
        parts.append(s + "</>")
    return "\n".join(parts)


def stats(requests):
    total = uncached = 0
    prev = []
    per = []
    for r in requests:
        toks = encode(render(r))
        n = 0
        for a, b in zip(prev, toks):
            if a != b:
                break
            n += 1
        total += len(toks)
        uncached += len(toks) - n
        per.append(len(toks))
        prev = toks
    return {"requests": len(requests), "input_tokens": total, "uncached_tokens": uncached, "per_request": per}


def workspace(tmp):
    root = Path(tmp) / "ws"
    shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns(".git", "desktop", "dist", "build", "brand",
                                                                 "prototypes", "__pycache__"))
    (root / "calc.py").write_text(CALC)
    (root / "test_calc.py").write_text(TESTS)
    return root


QA_ANSWER = ("Umbrous estimates tokens at about four characters per token plus four per message "
             "(umbrous/context/assemble.py:36-41). assemble() keeps the system prompt and the new message, then adds "
             "earlier turns newest first until the budget (window minus reply room) is used, and reports what it "
             "left out. In coding tasks, coding_context.fit drops whole old tool rounds and keeps a runtime progress "
             "record (umbrous/runloop/coding_context.py:38-75).")

LONG = scripted(
    ("read_file", {"path": "umbrous/context/assemble.py"}),
    ("read_file", {"path": "umbrous/runloop/coding_context.py"}),
    ("search_files", {"query": "estimate_tokens"}),
    ("read_file", {"path": "umbrous/context/assemble.py"}),
    ("edit_file", {"path": "umbrous/context/assemble.py", "old_text": "TOKENS_PER_MESSAGE = 4\n",
                   "new_text": "TOKENS_PER_MESSAGE = 4   # formatting overhead per message\n"}),
    ("run_command", {"command": "python3 -B -c 'import umbrous.context.assemble as a; print(a.estimate_tokens(\"abcd\"))'"}),
    ("read_file", {"path": "umbrous/context/assemble.py", "start_line": 30, "max_lines": 20}),
    ("run_command", {"command": "python3 -B -c 'import umbrous.context.assemble as a; print(a.estimate_tokens(\"abcd\"))'"}),
    final=lambda m: "Done. I documented TOKENS_PER_MESSAGE in umbrous/context/assemble.py and the import check "
                    "passes (exit 0).")

SCENARIOS = {
    "chat_no_folder": dict(profile=None, model="chat:3b", messages=[
        "What is the difference between a list and a tuple in Python?",
        "And when should I prefer a dataclass?",
        "Thanks. Summarize that in two lines."],
        agent=scripted(final=lambda m: "A list is mutable and a tuple is not; prefer tuples for fixed records "
                                       "and dataclasses when fields have names and behavior. " * 3)),
    "read_only_question": dict(profile="workspace_read", model="reader:3b", messages=[
        "How does Umbrous decide which earlier messages fit in the context?"],
        agent=scripted(("list_files", {}), ("search_files", {"query": "estimate_tokens"}),
                       ("read_file", {"path": "umbrous/context/assemble.py"}),
                       ("read_file", {"path": "umbrous/runloop/coding_context.py"}),
                       final=lambda m: QA_ANSWER)),
    "coding_fix_tests": dict(profile="workspace_code", model="coder:3b", messages=[
        "Add a multiply function to calc.py and make the tests pass."], agent=coder()),
    "coding_long": dict(profile="workspace_code", model="long:3b", messages=[
        "Document TOKENS_PER_MESSAGE in assemble.py and check the module still imports."], agent=LONG),
}


def run(name, sc):
    fake = FakeOllama(models=[FakeModel(sc["model"], agent=sc["agent"])]).start()
    try:
        with tempfile.TemporaryDirectory() as tmp, RunningRuntime(ollama_url=fake.url) as rt:
            if sc["profile"]:
                rt.runtime.access.attach("prj_general", str(workspace(tmp)), sc["profile"])
            assert rt.post_json("/api/models/probe", {"model": sc["model"]})[0] == 200
            fake.requests.clear()
            cid = rt.new_chat()
            for msg in sc["messages"]:
                ev = rt.chat(cid, sc["model"], msg)
                assert ev[-1]["type"] == "done", ev[-3:]
            reqs = [r["body"] for r in fake.requests if r["path"] == "/api/chat"]
            calls = [e["tool"] for e in ev if e["type"] == "tool_call"]
            return dict(stats(reqs), tools_called=calls)
    finally:
        fake.stop()


if __name__ == "__main__":
    if "--workspace" in sys.argv:
        SOURCE = Path(sys.argv[sys.argv.index("--workspace") + 1]).resolve()
    print("tokenizer:", TOKENIZER)
    out = {name: run(name, sc) for name, sc in SCENARIOS.items()}
    for name, s in out.items():
        print(f"{name:20} requests={s['requests']:2} input={s['input_tokens']:6} uncached={s['uncached_tokens']:6} "
              f"per={s['per_request']} tools={s['tools_called']}")
    print("TOTAL input", sum(s["input_tokens"] for s in out.values()),
          "uncached", sum(s["uncached_tokens"] for s in out.values()))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(out, indent=1))
