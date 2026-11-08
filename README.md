# Umbrous

A local-first agent harness. Status: **M8, selective start-fresh reset: choose chats and memory, see exactly what goes and what stays, and only that is deleted** (built on M6's memory and M7's write review; this completes the controlled-memory stage of the milestone plan).

```sh
bin/umbrous          # start the local runtime and open the app (Python 3.9+, no dependencies)
bin/umbrous models   # installed Ollama models, tool-call labels, exposure check
bin/umbrous workspace PROJECT FOLDER   # give a project's chats read-only access to one folder
bin/umbrous receipts --last 10         # what tools did
bin/umbrous tasks --last 10            # tasks: steps, actions, how each ended (--task ID for the trace)
bin/umbrous memory                     # personal and project memory (add, edit, delete)
bin/umbrous memory suggestions         # what tasks suggested remembering (accept, reject)
bin/umbrous reset --chat ID --personal all   # preview a selective reset; --confirm PREVIEW_ID deletes exactly that
bin/umbrous test     # run the tests
desktop/scripts/build-native.sh   # build dist/Umbrous.app and its .dmg (docs/desktop.md)
```

- [docs/m0-runtime.md](docs/m0-runtime.md): what M0 built, how the page reaches Python, the safety layers, demo and exercise
- [docs/m1-chat.md](docs/m1-chat.md): chat with a local Ollama model, context budget, deadlines, safe rendering, tool-call probe
- [docs/m2-history.md](docs/m2-history.md): saved chats and projects, archive vs delete, where copies of your chats live
- [docs/m3-gate.md](docs/m3-gate.md): the tool gate, read-only workspace access, approvals and grants, receipts, confinement limits
- [docs/m4-tasks.md](docs/m4-tasks.md): task state, the event log and report, limits, Stop, evidence and uncertainty, privacy of task records
- [docs/m5-resume.md](docs/m5-resume.md): recovery after a crash, unknown vs failed, retry classes, resume under current scope, what a future write needs
- [docs/m6-memory.md](docs/m6-memory.md): personal and project memory, retrieval vs training, project isolation, why deleting memory does not rewrite old chats
- [docs/m7-review.md](docs/m7-review.md): suggestions after a task, the review queue, provenance (your words vs the reply), accept, edit, reject, duplicates
- [docs/m8-reset.md](docs/m8-reset.md): Start fresh: preview, cancel, a changed preview, what stays (task records, receipts, backups), derived caches
- [docs/desktop.md](docs/desktop.md): Umbrous.app for macOS (native GPUI interface, bundled Python): build, validation, measurements, limits
- [docs/receipts.md](docs/receipts.md): receipt format and redaction rules
- [prototypes/](prototypes/): the front-end prototype the app is built on
