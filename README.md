# Umbrous

A local-first agent harness. Status: **M9, coding in a project folder: Umbrous edits files, runs the project's commands, fixes what fails, and continues an interrupted coding task without losing or repeating its changes** (built on M5's recovery; commands run as you and are not sandboxed, see docs/m9-coding.md).

Use local Ollama models or connect your ChatGPT subscription through the official Codex CLI. Choose a model, its advertised reasoning level, and Standard or Fast in the model menu. ChatGPT is an explicit cloud choice: conversation context, selected memories, and approved tool results are sent to OpenAI. See [ChatGPT setup and limits](docs/chatgpt-provider.md).

## Open the macOS app

If you already built Umbrous, run these commands from the repository folder:

```sh
open dist/Umbrous.app
```

Or open `dist/Umbrous.app` in Finder. The app starts its bundled runtime automatically; you do not need to run a separate Umbrous server.

To install it in Applications, open `dist/Umbrous_0.2.0_aarch64.dmg`, drag **Umbrous** into **Applications**, then open it from Applications or Spotlight. Once installed, you can also launch it with:

```sh
open -a Umbrous
```

### Build it for the first time

The native app requires an **Apple Silicon Mac with macOS 12 or newer**. To build it, install the Xcode Command Line Tools (`xcode-select --install`), Rust through `rustup`, and `uv`. Make sure `cargo` and `uv` are available in your terminal.

```sh
git clone https://github.com/Umbrous-ai/Umbrous.git
cd Umbrous
desktop/scripts/build-native.sh
open dist/Umbrous.app
```

If you already have the repository, use its folder instead of cloning it again. The build creates the app and installer in `dist/` and bundles Python for you. See [the desktop build guide](docs/desktop.md#build) for details.

### Start your first chat

1. **Choose a model.** For local models, start Ollama and install a model with `ollama pull qwen2.5:3b`, then select it in Umbrous's model menu. For ChatGPT, choose **Connect ChatGPT**, complete sign-in, refresh the models, and select one. See [ChatGPT setup](docs/chatgpt-provider.md#setup) for the Codex CLI requirement and provider limits.
2. **Send a message.** Open a new chat, type in the composer, and press Enter.
3. **To work with files**, open the shield pill on the composer's left, choose an access level, and pick your project folder when prompted. **Read only** allows reading; **Workspace write** also allows edits and asks before commands; **Full access** also runs commands without asking and requires confirmation. Commands run as you and are not sandboxed.
4. If the shield says **no tools**, open its menu and click **Check tools**. After the model passes, send your task. Answer command approval cards with **Approve once**, **Allow in this project**, or **Deny**.

Quit with **⌘Q** when finished. Your chats are saved for the next launch.

### Update your local app

Quit Umbrous, then run these commands from the repository folder:

```sh
git switch main
git pull --ff-only
desktop/scripts/build-native.sh
open dist/Umbrous.app
```

If you launch Umbrous from Applications, replace that installed copy using the newly built `.dmg`. Pulling source changes alone does not update the installed app.

## Run the browser interface

With Python 3.9 or newer, run this from the repository folder:

```sh
bin/umbrous
```

This starts the local runtime and opens the browser interface. Keep the terminal running; press **Ctrl+C** there to stop it. Use the same model setup described above.

## Command-line reference

```sh
bin/umbrous          # start the runtime and open the browser interface (Python 3.9+, no dependencies)
bin/umbrous models   # available models, tool-call labels, local exposure check
bin/umbrous workspace PROJECT FOLDER   # give a project's chats read-only access to one folder
bin/umbrous workspace PROJECT FOLDER --profile workspace_code   # or let them edit files and run commands there (M9)
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
- [docs/m9-coding.md](docs/m9-coding.md): coding in a folder: "work on /path", the edit and command tools, conflicts, the coding loop and report, recovery and resume of changes and commands, working on Umbrous itself, what is not sandboxed
- [docs/desktop.md](docs/desktop.md): Umbrous.app for macOS (native GPUI interface, bundled Python): build, validation, measurements, limits
- [docs/chatgpt-provider.md](docs/chatgpt-provider.md): ChatGPT sign-in, model/reasoning/speed controls, cloud context, and provider isolation
- [docs/architecture.md](docs/architecture.md): runtime boundaries and the repository-wide refactor
- [docs/receipts.md](docs/receipts.md): receipt format and redaction rules
- [prototypes/](prototypes/): the front-end prototype the app is built on
