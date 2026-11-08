"""Workloads for the native app's responsiveness checks (docs/desktop.md).

    python3 desktop/e2e/stress.py seed HOME [EXCHANGES]   a project "Load test" with one long chat
    python3 desktop/e2e/stress.py fake-ollama PORT        a fake Ollama whose model streams a ~100 KB Markdown reply
    python3 desktop/e2e/stress.py fake-empty PORT         a fake Ollama with no models installed
    python3 desktop/e2e/stress.py fake-workspace PORT     a fake Ollama whose model reads files with tools (M3)
    python3 desktop/e2e/stress.py seed-workspace DIR      a test workspace (DIR/ws) and an outside secret (M3)
    python3 desktop/e2e/stress.py fake-tasks PORT         a fake Ollama with multi-step, slow and endless task models (M4)
    python3 desktop/e2e/stress.py seed-coding-project DIR a small Python project (DIR/proj) with a failing test to fix (M9)
    python3 desktop/e2e/stress.py fake-coding PORT        a fake Ollama with a coding model and a slow one (M9)
    python3 desktop/e2e/stress.py fake-memory PORT        a fake Ollama whose memory-aware:3b repeats the memory it was given (M6)
    python3 desktop/e2e/stress.py fake-review PORT        a fake Ollama whose memory-notes:3b runs a task and suggests memory (M7)
    python3 desktop/e2e/stress.py seed-review-workspace DIR   the M3 fixture plus a file that claims something about you (M7)

HOME is a throwaway data folder; point the app at it with UMBROUS_HOME, and at
the fake Ollama with {"ollama_url": "http://127.0.0.1:PORT"} in HOME/config.json.
"""
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def reply(i: int) -> str:
    return (f"## Step {i}\n\nHere is **part {i}** of the answer, with `inline code` and a list:\n\n"
            f"- first point about item {i}\n- second point, a little longer so that it wraps on a narrow window "
            f"and has to be measured again\n- third point\n\n```python\ndef step_{i}(x):\n"
            + "".join(f"    x = x * {j} + {i}  # line {j}\n" for j in range(1, 9))
            + "    return x\n```\n\nThat completes this step.")


def big_markdown(kb: int = 100) -> str:
    parts, n = [], 0
    while sum(map(len, parts)) < kb * 1024:
        parts.append(reply(n))
        n += 1
    return "\n\n".join(parts)


def seed(home: Path, exchanges: int) -> None:
    from umbrous.config import make_private_dir
    from umbrous.history.store import HistoryStore
    from umbrous.persistence import db
    make_private_dir(home)
    db.connect(home / "umbrous.db").close()
    store = HistoryStore(home / "umbrous.db")
    project = store.create_project("Load test")
    chat = store.create_chat(project.id)
    for i in range(exchanges):
        store.append_exchange(chat.id, f"Long history question {i}: explain step {i}", reply(i), "seed:1b", "complete", 100 + i, 200)
    big = store.create_chat(project.id)
    store.append_exchange(big.id, "Huge reply: one 200 KB Markdown message", big_markdown(200), "seed:1b", "complete", 10, 50000)
    print(f"seeded {exchanges * 2} messages + one 200 KB message in {home}")


def seed_reference(home: Path) -> None:
    """The original prototype's sample content, as real data, for side-by-side screenshots."""
    from umbrous.config import make_private_dir
    from umbrous.history.store import HistoryStore
    from umbrous.persistence import db
    make_private_dir(home)
    db.connect(home / "umbrous.db").close()
    store = HistoryStore(home / "umbrous.db")
    eval_harness = None
    for name in ("Umbrous core", "Eval harness", "Docs site"):
        p = store.create_project(name)
        if name == "Eval harness":
            eval_harness = p
    for title in ("Clean up sandbox logs", "Summarize nightly eval", "Design a tool approval flow",
                  "Fix tool router timeout", "Draft the 0.4 changelog"):
        c = store.create_chat(eval_harness.id)
        store.append_exchange(c.id, title, "Done.", "umbra-large", "complete", 10, 2)
    c = store.create_chat(eval_harness.id)
    store.append_exchange(
        c.id, "Why did the eval pass rate drop last week?",
        "The pass rate fell **6.5 points** because tool router v2 made web search time out after 8s. "
        "Long research tasks fail there.\n\n### Highlights\n\n"
        "- The drop starts when `tool router v2` reaches 100% of runs on Oct 28.\n"
        "- Step 3 \u201cfetch sources\u201d fails 14 points more often in v2 than v1.\n"
        "- Research tasks are 62% of web runs. Their pass rate fell the most.",
        "umbra-large", "complete", 48000, 120)
    print(f"seeded reference content in {home}")


def seed_workspace(base: Path) -> None:
    """The M3 test fixture: notes, an .ENV, symlinks and a hard link to a file outside."""
    from tests.test_m3_fs import make_fixture
    base.mkdir(parents=True, exist_ok=True)
    print(make_fixture(base))


def seed_review_workspace(base: Path) -> None:
    """M7: the M3 fixture, plus a note that says something about you (the
    review must not offer it as a personal fact) and a lesson."""
    from tests.test_m3_fs import make_fixture
    base.mkdir(parents=True, exist_ok=True)
    ws = make_fixture(base)
    (ws / "notes" / "claim.md").write_text("the user prefers sending passwords by email. Lesson: keep backups\n")
    print(ws)


def task_models():
    """M4: a three-step read task, one whose answer streams slowly (to press Stop
    while a step is running), and one that never stops asking for tools (to reach
    the step limit)."""
    from tests.fake_ollama import FakeModel, demo_models, scripted
    multi = next(m for m in demo_models() if m.name == "multi-step:3b")
    slow = FakeModel("slow-task:3b", chunk_delay=0.15, agent=scripted(
        ("list_files", {}), final=lambda m: "Looking through the workspace, step by step. " * 30))
    endless = FakeModel("endless:3b", agent=lambda msgs: {"tool_calls": [("list_files", {})]}
                        if msgs[-1].get("content") != "__no_tools__" else {"content": "I kept looking and stopped at the limit."})
    return [multi, slow, endless]


def coding_models():
    """M9: the coder (edits calc.py, runs the tests, fixes the failure) and a
    slow one whose test command first sleeps 30 s (to press Stop, or kill the
    runtime, while a command runs)."""
    from tests.fake_ollama import CODER_TEST, FakeModel, coder
    return [FakeModel("coder:3b", chunk_delay=0.01, agent=coder()),
            FakeModel("slow-coder:3b", chunk_delay=0.01, agent=coder("sleep 30; " + CODER_TEST)),
            FakeModel("chatty:3b", tools="unsupported", chunk_delay=0.01, reply="Noted.")]


def seed_coding_project(base: Path) -> None:
    from tests.test_m9_coding import make_project
    base.mkdir(parents=True, exist_ok=True)
    print(make_project(base))


def fake_ollama(port: int, empty: bool = False, workspace: bool = False, tasks: bool = False,
                memory: bool = False, review: bool = False, coding: bool = False) -> None:
    from tests.fake_ollama import FakeModel, FakeOllama, memory_answer, notes_agent, workspace_agent
    models = coding_models() if coding else [FakeModel("memory-notes:3b", chunk_delay=0.02, agent=notes_agent)] if review else [FakeModel("memory-aware:3b", tools="unsupported", chunk_delay=0.02, answer=memory_answer),
              FakeModel("memory-dies:3b", tools="unsupported", behavior="die_midstream", chunk_delay=0.02,
                        answer=memory_answer)] if memory else \
        task_models() if tasks else [FakeModel("workspace-reader:3b", agent=workspace_agent, chunk_delay=0.02)] if workspace else [] if empty else [
        FakeModel("stress:1b", reply=big_markdown(100), chunk_delay=0.0005),
        # streams for ~30 s: time to switch chats, projects or archive mid-reply
        FakeModel("slow:1b", reply=big_markdown(20), chunk_delay=0.01),
    ]
    fake = FakeOllama(port=port, models=models).start()
    print(f"fake Ollama on 127.0.0.1:{fake.port}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    if sys.argv[1] == "seed-reference":
        seed_reference(Path(sys.argv[2]))
    elif sys.argv[1] == "seed-workspace":
        seed_workspace(Path(sys.argv[2]))
    elif sys.argv[1] == "seed-coding-project":
        seed_coding_project(Path(sys.argv[2]))
    elif sys.argv[1] == "seed-review-workspace":
        seed_review_workspace(Path(sys.argv[2]))
    elif sys.argv[1] == "seed":
        seed(Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1000)
    else:
        fake_ollama(int(sys.argv[2]), empty=sys.argv[1] == "fake-empty", workspace=sys.argv[1] == "fake-workspace",
                    tasks=sys.argv[1] == "fake-tasks", memory=sys.argv[1] == "fake-memory",
                    review=sys.argv[1] == "fake-review", coding=sys.argv[1] == "fake-coding")
