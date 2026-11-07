"""Workloads for the native app's responsiveness checks (docs/desktop.md).

    python3 desktop/e2e/stress.py seed HOME [EXCHANGES]   a project "Load test" with one long chat
    python3 desktop/e2e/stress.py fake-ollama PORT        a fake Ollama whose model streams a ~100 KB Markdown reply
    python3 desktop/e2e/stress.py fake-empty PORT         a fake Ollama with no models installed

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


def fake_ollama(port: int, empty: bool = False) -> None:
    from tests.fake_ollama import FakeModel, FakeOllama
    models = [] if empty else [FakeModel("stress:1b", reply=big_markdown(100), chunk_delay=0.0005)]
    fake = FakeOllama(port=port, models=models).start()
    print(f"fake Ollama on 127.0.0.1:{fake.port}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed(Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1000)
    else:
        fake_ollama(int(sys.argv[2]), empty=sys.argv[1] == "fake-empty")
