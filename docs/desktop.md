# Umbrous for macOS (native app)

`Umbrous.app` is a native macOS app. Its interface is written in Rust with
[GPUI](https://gpui.rs) (Zed's GPU-rendered UI framework); there is no web view,
Electron, Tauri or Swift. The agent itself stays in Python: the app ships its
own Python 3.13 and the `umbrous` package, starts the runtime, and talks to it
over the same protected loopback API the browser page used. Chat history,
projects, `config.json` and the `bin/umbrous` command line are unchanged and
shared.

Ollama remains a separate app. Install it from ollama.com and pull a model
(`ollama pull qwen2.5:3b`). Umbrous says so in the window when Ollama is not
running or has no models.

## Build

Apple Silicon, macOS 12 or newer. Needs Rust (`rustup`), the Xcode Command Line
Tools (full Xcode is not needed) and [uv](https://docs.astral.sh/uv/) for the
bundled Python.

```sh
desktop/scripts/build-native.sh
```

It runs:

1. `desktop/scripts/build-backend.sh`: copies a relocatable CPython 3.13
   (python-build-standalone, via `uv python install 3.13`) and `umbrous/` plus
   the prototype page into `build/desktop-backend/`, drops what a
   standard-library-only server never imports (tkinter, idlelib, tests, pip,
   headers), and precompiles every `.py` with unchecked-hash `.pyc` files so the
   app never writes into its own bundle. It refuses a Python that is not
   self-contained.
2. `cargo build --release --locked` in `desktop/app/`.
3. Assembles `dist/Umbrous.app`, signs every Mach-O inside out (the bundled
   `python3` and each `.so`, then the app) with the hardened runtime and no
   entitlements, verifies the signature, and makes the `.dmg`.

| Output | Size |
|---|---|
| `dist/Umbrous.app` | 44 MB (Python and runtime: 38 MB of it) |
| `dist/Umbrous_0.2.0_aarch64.dmg` | 19 MB |

Open the `.dmg` and drag Umbrous to Applications. Nothing from the repository is
used at run time.

## Local build versus a build for other people

| | Local build (`build-native.sh`) | Distribution build |
|---|---|---|
| Signature | Ad-hoc (`-`), hardened runtime | Developer ID Application, hardened runtime, secure timestamp |
| Notarized | No | Yes |
| Runs on the Mac that built it | Yes | Yes |
| Runs when downloaded on another Mac | **No.** Gatekeeper rejects it (`spctl -a -vv` says "rejected"); a user would have to override it in System Settings ▸ Privacy & Security | Yes |
| Needs | nothing | Apple Developer Program membership |

A local build never gets the quarantine attribute, which is why it runs here.
Do not hand the local `.dmg` to other people as a release.

To make a distribution build (not done yet: it needs the certificate):

```sh
UMBROUS_SIGN_IDENTITY="Developer ID Application: <Name> (<TEAMID>)" desktop/scripts/build-native.sh
xcrun notarytool submit dist/Umbrous_0.2.0_aarch64.dmg --keychain-profile <profile> --wait
xcrun stapler staple dist/Umbrous_0.2.0_aarch64.dmg
spctl -a -vv -t open --context context:primary-signature dist/Umbrous_0.2.0_aarch64.dmg
```

The script already signs the nested Python binaries with the identity, the
hardened runtime and a timestamp, which notarization requires. Still open:
an Intel or universal build (needs an x86_64 Python and a second target), and
updates (none: a new version is a new `.dmg`).

## How it works

```
Umbrous.app/Contents/
  MacOS/Umbrous                Rust: window, rendering (GPUI/Metal), menus, input, runtime lifecycle
  Resources/backend/python/    CPython 3.13, standard library only
  Resources/backend/app/       umbrous/ (+ prototypes/umbrous-harness.html for the browser flow)

Umbrous ── spawns ── python3 -E -s -B -X utf8 -m umbrous serve --desktop --port 0
   │                    (working dir Resources/backend/app, minimal environment)
   │◀── stdout: {"event":"ready","origin":"http://127.0.0.1:<port>","launch_code":"…"}
   │──▶ stdin:  "stop" · EOF = stop
   │
   │  POST /api/session/claim  (X-Umbrous-Launch)  → session credential, kept in Rust memory only
   └─ GET/POST /api/…          (X-Umbrous-Token)   projects, chats, models, streaming /api/chat
```

`serve --desktop` (`umbrous/desktop.py`) changes only how the runtime talks to
the process that started it. The HTTP API, its Host/Origin/credential checks and
the database are exactly what the browser flow uses.

| File | Job |
|---|---|
| `desktop/app/src/main.rs` | App start, single instance, menus, quit |
| `backend.rs` | Start, watch, restart and stop the runtime |
| `api.rs` | The runtime's HTTP API (blocking, called from background threads) |
| `workspace.rs` | The window: sidebar, transcript, composer, details, error screens |
| `transcript.rs` | Virtualised transcript rows, Markdown rendering, cross-message selection |
| `composer.rs` | The message box: a native text field (macOS text-input protocol, IME) |
| `markdown.rs` | Markdown → styled blocks, on a background thread |
| `actions.rs` | Keyboard shortcuts |
| `e2e.rs` | Test-only driver (`--features e2e`), never in the release bundle |

### Lifecycle

| Situation | What happens |
|---|---|
| Open | Python starts first, in parallel with GPUI's window and Metal setup. "Starting Umbrous…" until the runtime is ready (usually under 0.1 s), then the full window |
| Open again (Finder, Dock, `open -n`) | A lock file in `run/` holds the first copy's pid; the second copy activates exactly that process and exits. One app, one runtime |
| Close the window (red button, Cmd-W) | Umbrous hides and keeps running; the Dock icon brings it back |
| Quit (Cmd-Q, menu, Dock, logout) | `applicationWillTerminate` → the shell sends `stop` and closes the pipe; the runtime records the stop and removes `run/runtime.json`. SIGTERM after 5 s, SIGKILL after 2 more |
| The shell is force-killed | The OS closes the pipe; the runtime stops itself (measured: within 0.4 s) |
| The runtime crashes | "The Umbrous runtime stopped", with its last stderr lines and Restart. Its stale `runtime.json` is removed (only if it names that child) |
| Startup fails | Bad `config.json`, a database from a newer version, no free port, or 45 s without "ready": the window says which, with Retry, Show logs and Quit |
| `bin/umbrous` already running on the same data | "Umbrous is already running … started from a terminal", asking you to stop it there. The app never signals a process it did not start |
| Ollama not running / no models | The model pill and a notice say so (with Retry); Send is disabled. Rechecked when the window becomes active, from the model menu, and from Chat ▸ Check Ollama Again |

Only the child the app spawned is ever signalled. A waiter thread blocks in
`waitid(WNOWAIT)`, which reports the exit without reaping; the child is reaped
under the same lock that guards every kill, so a pid that the OS has already
reused can never be signalled. Nothing polls: exits, output and the startup
deadline are all blocking waits.

### Interface

The original interface, `prototypes/umbrous-harness.html` (what `bin/umbrous`
serves), is the visual source of truth. The native views reproduce it:

- `desktop/app/src/theme.rs` holds that file's `:root` tokens unchanged (canvas
  `#141414`, panels `#1B1B1B`, raised `#222222`, borders `#2A2A2A`/`#353535`,
  text `#7F7F7F`/`#B5B5B5`/`#F2F2F2`, accent `#8B7CF6`, the 4–48 px spacing and
  4–12 px radius scales, SF Pro at 400/500, SF Mono).
- `desktop/app/assets/icons/` are the 41 icons from the page's SVG sprite,
  extracted unchanged (1 px outlines on a 16 px grid).
- `desktop/app/src/ui.rs` has one helper per CSS class (`.nav`, `.icon-btn`,
  `.pill`, `.notice`, `.panel`, `.kv`, `.rsec`, `.row`, `.tg`), and the views name
  the class they mirror: the three floating panels (300 / flexible / 300, gap 6,
  radius 12), the sidebar with workspace switcher, New chat ⌘N, collapsible
  Projects (+ on hover) and Sessions, the 56 px header, the 696 px thread column,
  `.msg-u` bubbles, `.answer` text at 16/1.6, `.reply-meta`, the composer with
  attach, model pill, Tools · 4 and the round send button.
- The right sidebar shows only Changes and Sources (Freddy, 2026-11-07; it
  replaces the page's Details list of Runtime, Session, Tools, Files and Steps).
  The header's panel button shows and hides it; it starts shown and hides below
  1180 px wide, as the page's media query did. Its rows follow Freddy's reference
  screenshot: the project's name with a ⋯ menu (Show data folder), Changes with
  added and removed line counts, a divider, then Sources (+, Web search, View
  all); 20 pt side padding, 30 pt rows, 13 pt text. Changes reads +0 -0 because
  nothing in Umbrous can change files until tools arrive (M3); adding sources
  and View all wait for the same. Runtime facts that used to be there are
  elsewhere: Ollama state in the model pill and notices, the version in About.

`desktop/e2e/scripts/` and `stress.py seed-reference` reproduce the page's sample
content (projects, sessions, the "pass rate" reply) as real data so both can be
screenshotted at the same window size.

Where the native view differs, and why:

| Difference | Reason |
|---|---|
| A 28 px strip above the panels | macOS draws the window controls there. In the browser the title bar played that role; putting the controls inside the sidebar would move its contents |
| Inline code (`tool router v2`) is a flat highlight, without the page's rounded, padded chip and branch icon | GPUI styles runs inside wrapped text with colour, weight and background only; padding, radius and inline icons are not possible inside a text run |
| "Worked for 34s", the metric cards and the action pills from the sample are not shown | They were the prototype's demo content; the runtime does not produce them (the browser page did not show them for real replies either) |
| The right sidebar holds Changes and Sources instead of the page's Details list | Freddy's request (2026-11-07) |

Hover states follow the page's CSS: `.nav:hover`, `.icon-btn:hover` (and
`[aria-pressed]` keeping its background), `.pill:hover`, `.section:hover` with
its `+`, `.mm-row`, `.opt-menu` and `.row`. In CSS an icon inherits the colour
`:hover` sets; GPUI icons take an explicit colour, so each icon declares the same
colour change against its row's hover group (`ui::hover_icon`).

Replies are still rendered as Markdown, in the page's styles: paragraphs as
`.answer`, headings as `h3`, lists as `.hl`, inline code as the chip colours,
code blocks as raised cards in SF Mono.

macOS integration: menu bar (Umbrous, File, Edit, View, Chat, Window, Help),
standard shortcuts (⌘N new chat, ⌘W close, ⌘Q quit, ⌘H hide, ⌘C/⌘X/⌘V/⌘A,
⌘Z/⇧⌘Z in the composer, ⌘. stop reply, ⌃⌘F full screen, ⌘\ sidebar), the
Services menu, Emoji & Symbols (⌃⌘Space), system clipboard, minimum window size
900×600.

Text: the composer is a native text field built on GPUI's platform input
handler: typing, IME marked text and candidate window placement, word/line
movement and deletion, undo/redo, double/triple click. Enter sends, ⇧Enter or
⌥Enter adds a line. Transcript text is selectable across blocks and messages by
dragging (double-click selects a word, triple-click a block, ⇧-click extends),
and ⌘C copies it as plain text. Model output stays inert: HTML is shown
literally and links are styled but never opened.

### Navigation while a reply streams

Every reply, Markdown parse and transcript load remembers the view it was made
for (`Workspace::view`, bumped by New chat, opening a chat or changing project)
and is dropped if you have moved on. Leaving a chat while its reply streams
detaches the reply: the app stops reading it, the runtime stops the model and
saves what arrived as "stopped", and the lists refresh when it is saved (the
transcript too, if you went back to that chat). Lists and model checks keep a
request number, so an older response never overwrites a newer one. Going back to
the chat still on screen while another one loads (A → B → A) cancels that load
and keeps A: "already showing" only counts when nothing else is loading.

Before this, a reply's late updates were applied to whatever transcript was on
screen: into another chat's last message, or, in an empty new chat, an
out-of-range index that crashed the app (panic in `apply_stream`). Panics are
now written to `desktop.log` as `app.panic` with their source location (no
message text beyond its first words).

### Keeping the UI thread free

- Every API call runs on GPUI's background executor; a streaming reply runs on
  its own thread. The UI thread only applies results.
- Markdown is parsed on the background executor, for stored chats when they
  load and for a streaming reply whenever new text has arrived (latest wins,
  never two parses at once).
- Streaming deltas are batched: the UI wakes for the first queued event, takes
  everything queued, then sleeps 33 ms. A 100 KB reply of 17,138 deltas arrived
  in 345 UI updates.
- The transcript is a GPUI `list` whose rows are single blocks, not whole
  messages, so a 200 KB reply costs only its visible blocks per frame. During
  streaming only the rows from the first changed block onward are re-measured;
  the rest of the transcript is never rebuilt.
- No timers: no health poll, no caret blink, no Ollama poll.

## Security

The Python runtime still enforces everything from M0–M2: loopback-only bind,
Host check (DNS rebinding), Origin and fetch-metadata checks, no CORS, the
session credential on every `/api/` call, no delete route. The app adds:

| Layer | Stops |
|---|---|
| No web content at all | No page, scripts, browser extensions, navigation or new windows to restrict |
| Launch code on a private pipe | Never on a command line (`ps`) or in a file or history |
| Credential in Rust memory only | Never written anywhere; not in logs (`Api`'s debug output omits it) |
| Minimal child environment | `HOME`, `USER`, `TMPDIR`, a fixed `PATH`, and `UMBROUS_HOME`/`OLLAMA_HOST` if set; `-E -s` ignore `PYTHON*` variables and user site-packages |
| Model output inert | Text only; HTML literal; links never opened |
| Hardened runtime, no entitlements | No JIT, unsigned memory, library-validation exception, camera, microphone, location, contacts or Apple Events |

Not sandboxed (App Sandbox): the runtime runs a bundled interpreter and uses
`~/Library/Application Support/Umbrous`. What the M0 notes say about other
programs running as you still holds.

## Data and the command line

Same data as `bin/umbrous`: `~/Library/Application Support/Umbrous` (or
`UMBROUS_HOME`). Only one runtime can use it at a time; whichever starts second
says so. `runtime.json` records `"mode": "desktop"` or `"cli"`. The app's own
events go to `logs/desktop.log` (starts, stops, exit codes, timings; no chat
content, codes or credentials; the runtime's stderr is kept only in memory for
the error screen). `bin/umbrous purge logs` removes `desktop.log*` too.

## Validation

`desktop/e2e/validate-native.sh WORKDIR` copies the app out of the `.dmg` to a
folder outside the repository and runs everything below with throwaway data
folders. UI checks use `dist/Umbrous-e2e.app` (`build-native.sh --e2e`): the
same code plus a driver that runs scripted steps in the real window
(`desktop/e2e/scripts/`) and can capture its own window. The release binary
does not contain the driver (checked with `strings`).

Run on a MacBook Pro (Apple Silicon, macOS 26.6.2, 120 Hz display), Ollama
0.40.15 with `qwen3.5:9b`:

| Check | Result |
|---|---|
| Signature | `codesign --verify --deep --strict`: valid (ad-hoc, hardened runtime). `spctl`: rejected, as expected for a local build |
| Opens outside the repo | Runtime runs from `Umbrous.app/Contents/Resources/backend` |
| Chat and streaming | Real model, Markdown list and code block rendered while streaming |
| Projects and persistence | Added project "Garden", two chats, archived one; after Quit and relaunch: Garden listed with 1 active and 1 archived chat, transcript reopened with all 4 messages, a follow-up continued it with the earlier turns sent (prompt tokens 59 → 112 → 164) |
| Quit and cleanup | After every quit (12): no Umbrous or runtime processes left, `runtime.json` removed, runtime stopped by its own clean path |
| Duplicate launch (`open -n`) | 1 shell, 1 runtime; the second copy activated the first and exited |
| Runtime crash (`kill -9`) | Error screen ("crashed"), Restart brought the runtime back; stale `runtime.json` removed |
| App force-killed | Runtime exited on its own within 0.4 s |
| Ollama not running | Pill "Ollama not running", notice with Retry, Send disabled |
| No models installed | Pill "No models installed", menu tells you `ollama pull`, Send disabled |
| Leave a streaming reply (regression, `nav-stream.txt`) | 10 scenarios with a slow fake model, 26 assertions: New chat, another chat, another project, archive while streaming; a chat clicked then abandoned; two quick clicks; A → B → A from a chat on screen, from a new chat, and while A's reply streams. All pass, no panic logged. The build before the first fix crashed on scenario 1 (`panicked at src/workspace.rs:613`, SIGABRT); the build before the A → B → A fix showed B in scenario 8 |
| Already running, after the startup deadline (regression, `already-running.txt`) | Still "already running" after 50 s (previously replaced by "took too long to start" at 45 s); the terminal runtime kept running |
| Bad config (`host: 0.0.0.0`) | Error screen "Umbrous settings are invalid", no runtime left |
| Already running from a terminal | With your real `bin/umbrous` (pid 89154) on your real data: "Umbrous is already running … started from a terminal"; that runtime kept running and `umbrous.db` was unchanged (checksum) |
| Existing data | A copy of real M2 data opened: General with its saved chat listed |
| Select all + copy | 550,458 characters copied from a 2,000-message chat |
| Python tests | `bin/umbrous test`: 143 pass on Python 3.9 and 3.13 |
| Rust tests | `cargo test` in `desktop/app`: Markdown, selection, row splitting |

Not automated, please check by hand: dragging the window by its title bar,
typing with a Japanese or Chinese input source (IME), dictation, and VoiceOver
(see limitations).

## Measurements

Release build, launched with `open` like Finder does.

**Startup**

| | First launch after install | Later launches (n=8) |
|---|---|---|
| `open` → full window with data | 859 ms | 243 to 309 ms |
| process start → full window | 400 ms | 175 to 212 ms |
| runtime spawn → "ready" | 86 to 113 ms | 82 to 133 ms |

The first launch includes macOS checking the new signature. "Full window" is
the first frame rendered after projects arrived (`ui.first_full_frame` in
`desktop.log`); a window that starts hidden or covered renders later.

**Idle** (60 s after start, no reply running; `footprint` phys_footprint, CPU
time over the interval)

| Process | Memory | CPU, window visible | CPU, window covered |
|---|---|---|---|
| Umbrous (Rust/GPUI) | 49 to 99 MB | 0.6 to 1.9 % | 0.03 % |
| python3 (runtime) | 19 to 25 MB | 0.03 % | 0.03 % |

Ranges are over five runs with chats from empty to 2,000 messages open. The
visible idle CPU is GPUI's display link waking the main thread every frame (see
limitations).

**Responsiveness** (`desktop/e2e/scripts/stress.txt`: 2,000 stored messages with
headings, lists and 10-line code blocks; one 200 KB Markdown message; a fake
Ollama streaming 100 KB in 6-character pieces). Frame time = scroll step to
next presented frame; 8.3 ms is one frame at 120 Hz.

| Workload | Result |
|---|---|
| Open the 2,000-message chat | 56 to 58 ms to painted |
| Scroll it end to top (80 steps of 600 px) | p50 8.3 ms, p95 10.3 ms, max 11.2 ms |
| Open the 200 KB message | 60 ms |
| Scroll it (40 steps) | p50 8.4 ms, p95 9.9 ms, max 10.4 ms (53 ms per frame before rows were split per block) |
| Stream 100 KB (17,138 deltas) | 345 UI updates; Markdown parse max 1.2 ms off the UI thread; applying a batch max 0.21 ms on the UI thread |
| Scroll while it streams | p50 8.4 ms, p95 9.2 ms |

**Ollama**, separate: `ollama serve` 17 MB, the Ollama menu-bar app 33 MB, the
model runner with `qwen3.5:9b` loaded 8,597 MB.

No comparison with Zed or Ghostty was made, so no claim is made about matching
them. For reference only, the earlier Tauri version of this app (branch
`m3-desktop-tauri`, different feature set) measured on the same Mac: about
540 ms to the page, about 129 MB across seven processes, about 4 % idle CPU.

## GPUI: what worked and what did not

Worked well: rendering speed (every workload above at the display's frame
rate), the virtualised `list`, background executors, the platform text-input
protocol for IME, menus and key bindings, and building with only the Command
Line Tools.

Limitations found, none worked around by changing the architecture:

1. **No accessibility.** GPUI 0.2.2 exposes nothing to the macOS accessibility
   API, so VoiceOver, Voice Control and other assistive tools cannot read or
   operate the window (Zed has the same gap). Fixing it means implementing an
   NSAccessibility bridge for GPUI, or waiting for upstream. This is the
   biggest gap against the brief.
2. **No text widgets.** GPUI has no text field and no selectable text; both were
   written here (`composer.rs`, `transcript.rs`). Not available: spell check,
   autocorrect, the text context menu (right-click Look Up, Services on a
   selection), drag-and-drop of text, and auto-scroll while drag-selecting past
   the edge of the transcript.
3. **Idle CPU while visible.** GPUI keeps a display link running while the
   window is on screen and wakes the main thread every frame, about 1 % CPU
   when nothing changes. It stops when the window is hidden or covered.
   Changing that means patching GPUI.
4. **Shaders without Xcode.** GPUI normally precompiles its Metal shaders with
   Xcode's `metal` tool. This build uses its `runtime_shaders` option, which
   compiles them at launch, so building needs only the Command Line Tools. A
   build machine with Xcode can drop that option.
5. **API stability.** The crates.io release (0.2.2, pinned exactly) trails
   Zed's own copy, and its API changes between releases.

Not ported from the browser page yet: M3's tool steps, approval cards, Sources
and Access view (the app ignores those stream events; see
[m3-gate.md](m3-gate.md#the-desktop-app)), M4's task trace (the app shows the
running step and a one-line report; see [m4-tasks.md](m4-tasks.md#the-desktop-app); M5's interrupted-task notice and Resume are ported, see [m5-resume.md](m5-resume.md#the-desktop-app)), the tool-call probe button in the model
menu, and the prototype's placeholder panels (tool toggles, files, steps) that
had no runtime behind them.
