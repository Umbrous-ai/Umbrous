# Umbrous

A local-first agent harness. Status: **M3, the tool gate and read-only workspace access** (see the milestone plan).

```sh
bin/umbrous          # start the local runtime and open the app (Python 3.9+, no dependencies)
bin/umbrous models   # installed Ollama models, tool-call labels, exposure check
bin/umbrous workspace PROJECT FOLDER   # give a project's chats read-only access to one folder
bin/umbrous receipts --last 10         # what tools did
bin/umbrous test     # run the tests
desktop/scripts/build-native.sh   # build dist/Umbrous.app and its .dmg (docs/desktop.md)
```

- [docs/m0-runtime.md](docs/m0-runtime.md): what M0 built, how the page reaches Python, the safety layers, demo and exercise
- [docs/m1-chat.md](docs/m1-chat.md): chat with a local Ollama model, context budget, deadlines, safe rendering, tool-call probe
- [docs/m2-history.md](docs/m2-history.md): saved chats and projects, archive vs delete, where copies of your chats live
- [docs/m3-gate.md](docs/m3-gate.md): the tool gate, read-only workspace access, approvals and grants, receipts, confinement limits
- [docs/desktop.md](docs/desktop.md): Umbrous.app for macOS (native GPUI interface, bundled Python): build, validation, measurements, limits
- [docs/receipts.md](docs/receipts.md): receipt format and redaction rules
- [prototypes/](prototypes/): the front-end prototype the app is built on
