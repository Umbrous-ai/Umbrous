# M0: Runtime skeleton and UI bridge

M0 turns the static prototype into a page served by a real Python process on
your MacBook, with a small protected local API. Nothing an agent does is real
yet; that starts in M1. What *is* real: the runtime, its safety layers, its
database, its logs, and the Runtime section of the Details panel.

## Run it

```sh
bin/umbrous                 # start the runtime and open the app
bin/umbrous test            # run all tests (one command, standard library only)
bin/umbrous serve --no-open # start without a browser; prints a one-time URL
bin/umbrous prototype       # the old behaviour: open the static HTML file
bin/umbrous purge logs      # list log files; add --yes to delete them
```

Needs Python 3.9 or newer (macOS's `python3` works) and nothing else. No
packages, no build step. Data lives in `~/Library/Application Support/Umbrous`
(override with `--home DIR` or `UMBROUS_HOME`).

## How the browser reaches Python

```
bin/umbrous ── python3 -m umbrous ──▶ binds 127.0.0.1:8417
                     │                makes a session credential + a one-time launch code
                     ▼
     Chrome, own profile, extensions off
     opens http://127.0.0.1:8417/#launch=<code>
                     │
   GET /  ───────────┤ page (prototypes/umbrous-harness.html) + strict Content-Security-Policy
   GET /runtime-bridge.js
   POST /api/session/claim  (X-Umbrous-Launch: <code>)  ──▶ returns the session credential, once
   GET /api/health          (X-Umbrous-Token: <credential>) every 5 s ──▶ Details ▸ Runtime
```

1. `bin/umbrous` is a small shell script. It checks the Python version and runs
   `python3 -m umbrous`, which is `umbrous/cli.py`.
2. The CLI loads config, opens and migrates the database, then asks
   `umbrous/api/server.py` to listen on **127.0.0.1 only**.
3. `umbrous/api/security.py` makes two random secrets: the **session
   credential** (lives only in memory, new every launch) and a **launch code**
   (works once, expires after 2 minutes).
4. The browser opens with the launch code in the URL fragment (`#...`).
   Fragments are never sent to the server or logged. The page's
   `runtime-bridge.js` removes it from the address bar and trades it for the
   credential at `/api/session/claim`.
5. The page keeps the credential in `sessionStorage` (this tab only) and sends
   it as `X-Umbrous-Token` on every API call.

Why the extra launch code step? The URL a browser is opened with ends up in
the process list and browser history. Putting the credential itself there
would leave a working copy behind. A used-up launch code is worthless.

## The safety layers, and what each one stops

Every request goes through `evaluate()` in `security.py`, in this order:

| Layer | Stops | Refusal |
|---|---|---|
| Bind to 127.0.0.1 | Other computers on your network | Connection refused |
| Host header must be `127.0.0.1:<port>` or `localhost:<port>` | DNS rebinding: a hostile site that points its own name at 127.0.0.1 | 403 `host_not_allowed` |
| Origin / Sec-Fetch-Site must be this page | Other web pages open in your browser | 403 `origin_not_allowed` |
| No CORS at all (preflights refused) | Cross-site scripts reading responses | 403 |
| Session credential on every `/api/` call | Anything that did not get the launch code, e.g. other local programs guessing | 401 |
| Content-Security-Policy on the page | Injected scripts, loading remote code, framing | Browser blocks it |

Plus: request bodies over 16 KB are refused, only two exact file paths are
served (no directory serving, so no path tricks), sockets time out after 15 s,
and the database, logs and run directory are readable only by you (0600/0700).

**What these do not stop.** Any program already running as your user can read
browser memory or watch the launch, and any browser extension allowed on the
page can read it and use the credential, including approving actions later.
That is why Umbrous opens Chrome (or Brave, Edge, Chromium) with its own
profile and extensions disabled; if none is installed it falls back to your
default browser and prints a warning. Loopback plus a credential is a layer,
not a guarantee.

## Module map

The plan's components already have their place, even where empty:

| Module | Milestone | M0 contents |
|---|---|---|
| `umbrous/api/` | M0 | Server, access checks |
| `umbrous/web/` | M0 | `runtime-bridge.js` |
| `umbrous/config.py` | M0 | Defaults → `config.json` → env → flags; refuses non-loopback hosts |
| `umbrous/logging_setup.py` | M0 | JSON-lines logs, redacted |
| `umbrous/persistence/` | M0 | SQLite, versioned with `PRAGMA user_version` |
| `umbrous/receipts/` | M0 rules, M3 storage | Receipt format, ids, redaction. See [receipts.md](receipts.md) |
| `umbrous/browser.py` | M0 | Dedicated browser profile |
| `umbrous/model/`, `context/`, `runloop/` | M1 | Empty placeholders |
| `umbrous/gate/`, `tools/` | M3 | Empty placeholders |

Database schema v1: `meta` (key/value) and `launches` (each start and stop).
Migrations are append-only and each runs in one transaction; a database newer
than the code is refused.

## Changes to existing files

- `prototypes/umbrous-harness.html`: 10 added lines, nothing removed. A
  Runtime section at the top of the Details panel, two CSS rules for its
  status dot, and a `<script src="/runtime-bridge.js">` tag. Opened as a plain
  file it still works and shows "Not connected".
- `bin/umbrous`: now starts the runtime. The previous behaviour is kept,
  unchanged, as `bin/umbrous prototype`.

## Demonstrate

1. `bin/umbrous`. The Details panel's Runtime section turns green: Connected,
   version, schema v1, uptime, address. The fake agent run in the chat still
   works.
2. In another terminal: `scripts/demo-m0.sh`. It tries the API with no
   credential (401), a made-up one (401), a foreign Origin (403), a rebinding
   Host (403), a CORS preflight (403), and your Wi-Fi address (connection
   refused). From another machine on your network,
   `curl http://<your-mac-ip>:8417/` is refused the same way.
3. Look at `~/Library/Application Support/Umbrous/logs/runtime.log`: one JSON
   line per request, with the refusal reason, and no credential anywhere.
4. Ctrl-C. The panel switches to Unreachable. Run `bin/umbrous` again: the old
   tab shows "Session ended", because each launch has a new credential.

## Exercise: change one harmless status field

1. Open `umbrous/api/server.py`, find `Runtime.health()`, and add a field,
   e.g. `"greeting": "hello from Python",`.
2. Show it: in `prototypes/umbrous-harness.html` copy the `rt-bind` row and
   change its id to `rt-greeting`; in `umbrous/web/runtime-bridge.js` add
   `set('rt-greeting', h.greeting);` next to the other `set(...)` calls.
3. Restart `bin/umbrous` and watch it appear. Then add an assertion for it in
   `tests/test_local_api.py::test_health_with_credential` and run
   `bin/umbrous test`.

Things to notice: the value is written with `textContent`, so even
`"<b>hi</b>"` shows as text. The server reads the page once at launch (and pins
its inline script by hash in the CSP), so restart after editing it.

## Tests (55, `bin/umbrous test`)

| File | Covers |
|---|---|
| `tests/test_local_api.py` | Real server on a random port: page + headers, health, missing/wrong credential, foreign Origin, `null` Origin, cross-site fetch metadata, CORS preflight, bad Host (DNS rebinding), path tricks, body limit, loopback bind, not reachable on non-loopback addresses, launch code single use, credential differs per launch, logs never contain secrets |
| `tests/test_security_policy.py` | Policy unit tests with a fake clock: launch code expiry, Host matching, same-site vs same-origin |
| `tests/test_config_and_db.py` | Non-loopback hosts refused, config precedence, schema migration, newer DB refused, failed migration rolls back, file permissions |
| `tests/test_receipts.py` | Fixture secrets redacted (incl. inside a receipt), field limits, invalid receipts rejected |
| `tests/test_cli.py` | Whole command: start, refuse a second copy, claim, health, clean stop on SIGTERM, purge |
