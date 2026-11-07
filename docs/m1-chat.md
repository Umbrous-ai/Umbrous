# M1: Local model chat

M1 replaces the prototype's fake agent run with a real chat against a model
running in **Ollama on your Mac**. It adds no tools: the model can only talk.
Builds on M0 (the loopback runtime, session credential, Host/Origin checks).

## Run it

```sh
ollama pull qwen2.5:3b        # any chat model works; a few GB
bin/umbrous                   # start, open the app, pick a model in the composer, chat
bin/umbrous models            # installed models, tool-call labels, exposure check
bin/umbrous probe --all       # run the tool-call probe on every installed model
bin/umbrous test              # 109 tests; no Ollama needed (a stand-in is used)
```

## How a message travels

```
composer (chat.js)
  │ POST /api/chat {chat_id, model, message}        + X-Umbrous-Token (added by runtime-bridge.js)
  ▼
api/server.py ── evaluate(): Host, Origin, credential, JSON only
  ▼
runloop/chat.py  run_chat()
  1. model/router.py   resolve(model): installed right now? exact name? → else error, no fallback
  2. context/assemble.py  system + recent turns + message, inside the token budget
  3. model/ollama.py   chat_stream(): POST 127.0.0.1:11434/api/chat, stream=true
  4. events go back to the page as JSON lines: start · delta · delta · … · done | error
  ▼
chat.js appends each delta as a text node, then shows "model · tokens · time"
```

## The pieces

| Module | Job | Boundary |
|---|---|---|
| `model/ollama.py` | The only code that talks to Ollama | Loopback URL only (config refuses anything else). Never retries |
| `model/router.py` | Picks the model | Only installed models; the one you picked or an error. **No silent switching** |
| `model/base.py` | Error types, deadlines, cancel token | Every failure has a code and a sentence you can act on |
| `context/assemble.py` | Decides exactly what the model sees | Explicit budget, visible truncation |
| `runloop/chat.py` | One message in, one streamed reply out | Chats in memory only (history is M2); content never logged |
| `model/probe.py` | Tool-call round trip test | Uses a harmless fake tool; nothing runs on your Mac |
| `model/exposure.py` | Is Ollama reachable from the network? | Can't see firewall/router/IPv6 |
| `web/chat.js` | Composer, model picker, stop, rendering | Only text nodes; links show their real address |

### No switching, ever

The page picks a default model once, on its very first load, and shows it in
the pill before anything is sent. After that it never chooses for you: if the
chosen model disappears, the pill says "Pick a model" and Send stays disabled
until you pick one; if Ollama goes down, your choice is kept and a Retry
button appears (at start or mid-chat). Opening the picker refreshes the model
list and Ollama's status.

### Context and its budget

```
window = min(context_tokens [8192], the model's own context length)
budget = window − reply_tokens [1024]
sent   = system prompt + last recent_turns [20] messages that fit + new message
```

Older messages are dropped first, and every drop is reported: a small notice
above the reply ("4 earlier message(s) left out…") and the Context bar in the
Details panel (hover it for the numbers). A message too long to fit even alone
is refused before anything is sent. The window is passed to Ollama as
`num_ctx`; without it Ollama silently cuts long prompts to its own default.

Token counts before sending are estimates (≈4 characters per token). After
the reply, the footer shows Ollama's real count next to the estimate, e.g.
`qwen2.5:3b · 412 in (est. 398) · 87 out · 3.1s`.

### Deadlines and stopping

| Limit | Default | Ends as |
|---|---|---|
| Connect to Ollama | 5 s | "Ollama is not answering… Start the Ollama app" |
| First token (includes loading the model) | 180 s | "did not start answering within…" |
| Silence mid-reply | 60 s | "went silent for more than…" |
| Whole reply | 600 s | "took longer than the … limit" |

The whole-reply limit is enforced by a timer that shuts the Ollama socket the
moment time runs out, and every read's own timeout is capped by the time left,
so a read blocked inside the socket cannot overrun it.

The send button becomes a stop button while a reply streams. Stop calls
`/api/chat/cancel`, which shuts the Ollama socket down at once (a blocked read
wakes immediately) and the reply ends "Stopped by you". If any text arrived
before a stop, timeout or broken stream, it stays on screen marked
incomplete and is kept in the chat; if nothing arrived, the message is not
kept, so sending it again does not duplicate it.

### Safe rendering

The page holds the session credential, so model output is treated as hostile
text. `chat.js` only ever creates text nodes; there is no `innerHTML`
anywhere (a test enforces this). A reply of
`<img src=x onerror="alert(1)"><script>alert(2)</script>` shows exactly those
characters. After a reply ends, `http(s)://` addresses become links whose
visible text is the full address, opening in a new tab with no referrer.
Addresses on this machine (127.x, localhost, the runtime itself) are never
linked, so a reply cannot steer a click at the Umbrous API. The CSP also blocks
remote images, so nothing loads automatically.

### Tool-call probe

Ollama can say a model "supports tools", but M3 needs proof. The probe offers
the model one fake tool, `get_probe_code`, and passes only if the model (1)
calls it and (2) uses the random code it returns in its answer. Results are
stored per model **digest**, so a re-pulled model must pass again. Models that
fail still chat; the picker labels them "Plain chat only" and M3 will refuse
tool work for them.

### Ollama exposure check

Ollama has no login. At startup (and whenever the model list refreshes) the
runtime tries to connect to Ollama's port on your Mac's own network addresses
and reads `OLLAMA_HOST` (environment and `launchctl getenv`). Runtime ▸
Exposure shows "Loopback only" or, in red, "EXPOSED on network", and the
terminal prints a warning with the fix (`launchctl unsetenv OLLAMA_HOST`, then
restart Ollama). It cannot see the macOS firewall, your router, VPNs or IPv6.

## Changes to existing files

- `prototypes/umbrous-harness.html`: 27 lines added, 3 changed. Ids on the
  model pill, Session ▸ Model and the Context bar so chat.js can fill them;
  Ollama and Exposure rows in Runtime; CSS for replies, notices and the model
  menu; the `/chat.js` script tag. Opened as a file it still plays the fake run.
- `runtime-bridge.js`: exposes `window.umbrousApi.fetch()`, which adds the
  credential to `/api/` calls without ever handing it out; shows Ollama status.
- Database schema v2 adds `model_probes`. Version is now 0.1.0 (M1).
- Still prototype placeholders: the sidebar sessions/projects and the Tools,
  Files and Steps sections of the Details panel. History is M2, tools are M3.

## Demonstrate

1. `bin/umbrous`, pick a model, chat. The footer and Session ▸ Model name the
   exact model Ollama used.
2. Ask: *"Reply with exactly this: `<b>bold</b><script>alert(1)</script>`"*.
   It shows as text; no alert.
3. Ask for something long ("write 600 words about tides") and press stop.
4. Quit Ollama (menu bar ▸ Quit) and send: "Ollama is not answering…" and a
   Retry button. Nothing is sent anywhere else. Start Ollama, press Retry.
5. Open the model picker, press **Check tools** on two models (or
   `bin/umbrous probe --all`) and compare the labels.
6. Runtime ▸ Exposure reads "Loopback only". To see the warning, run
   `OLLAMA_HOST=0.0.0.0:11500 ollama serve` in a terminal and
   `bin/umbrous models` with `{"ollama_url": "http://127.0.0.1:11500"}` in
   a scratch `--home`; stop that server afterwards.

## Exercise: change the recent-turn limit

1. Create `~/Library/Application Support/Umbrous/config.json` containing
   `{"recent_turns": 2}` and restart `bin/umbrous`.
2. Send four short messages in one chat. From the third on, a notice says
   earlier messages were left out, and the Context bar's tooltip says "2 of 4".
3. Ask "what was my first message?". The model cannot know: it never saw it.
4. Set it back to 20 (or delete the file), restart, repeat, compare.

## Tests

| File | Covers |
|---|---|
| `tests/test_m1_chat.py` | Context budget and truncation notices, message too long, loopback-only Ollama URL, streaming, exact model per reply, `num_ctx` sent, two names sharing a digest keep their identity, history on the next turn, Ollama down, missing model with **no fallback**, interrupted stream, first-token/idle/total timeouts (total interrupts a blocked read), stop mid-reply, one reply per chat at a time, probe pass/fail stages, fresh probe code, probe per digest, exposure (loopback, not running, listening on all interfaces), JSON-only API, chat text never logged |
| `tests/test_browser_m1.py` | Headless Chromium: chat, HTML/script reply inert, no remote loads, stop button, model labels, Exposure row; a vanished model is not replaced; Retry after Ollama goes down keeps the same model. Skipped without Node + Playwright |
| `tests/test_page_bridge.py` | No HTML sinks or browser storage in either page script; the API helper never hands out the credential |
| `tests/fake_ollama.py` | The stand-in Ollama: same endpoints, can refuse tools, stall or die mid-reply |
