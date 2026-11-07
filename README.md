# Umbrous

A local-first agent harness. Status: **M1, local model chat** (see the milestone plan).

```sh
bin/umbrous          # start the local runtime and open the app (Python 3.9+, no dependencies)
bin/umbrous models   # installed Ollama models, tool-call labels, exposure check
bin/umbrous test     # run the tests
```

- [docs/m0-runtime.md](docs/m0-runtime.md): what M0 built, how the page reaches Python, the safety layers, demo and exercise
- [docs/m1-chat.md](docs/m1-chat.md): chat with a local Ollama model, context budget, deadlines, safe rendering, tool-call probe
- [docs/receipts.md](docs/receipts.md): receipt format and redaction rules
- [prototypes/](prototypes/): the front-end prototype the app is built on
