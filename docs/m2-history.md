# M2: Chat history and projects

M2 makes chats last. Every message and reply is saved on your Mac, chats belong
to projects, and a chat can be archived and restored. Nothing can be deleted
yet, on purpose (see "Deleting is not available yet" below). Builds on M1.

## Run it

```sh
bin/umbrous            # chat as before; the sidebar now lists real projects and chats
bin/umbrous test       # 136 tests; no Ollama needed
```

Quit with Ctrl-C, start again, and your projects and chats are still there.

## Words used here

| Word | Means | Where it lives |
|---|---|---|
| **Transcript** | The messages of one chat, exactly as sent and received, in order, with the model name, token counts and how each reply ended | `messages` table |
| **Project** | A named group of chats. Every chat is in exactly one. A fresh install has **General** | `projects` table |
| **Archived chat** | Hidden from the normal list, read-only, **every message kept**. Restore puts it back unchanged | `chats.archived_at` is set; nothing else changes |
| **Deleted chat** | Does not exist yet. No function, endpoint or button deletes | – |
| **Memory** (M6) | Facts the agent keeps across chats. Not built. Will be its own store; archiving a chat will not touch it | – |
| **Task records / receipts** (M3, M4) | What tools did and what they changed. Not built. Their own store, kept even if a chat is later deleted | – |

These are separate stores on purpose: archiving or (one day) deleting a chat
must never silently change memory or receipts, and the other way round.

## How a message is saved

```
New chat ── nothing is saved yet (an empty chat leaves no trace)
  │ first Send
  ▼
POST /api/chats {project_id}            → chat_<24 hex>, made by the runtime
POST /api/chat  {chat_id, model, message}
  runloop/chat.py
    1. chat exists? archived? → refuse before the model is called
    2. earlier turns come from the history store (not from memory)
    3. stream the reply as in M1
    4. history.append_exchange(): message + reply in ONE transaction
       (complete, or stopped / timeout / interrupted / error if partial text arrived)
  ▼
sidebar refreshes; the title is the start of the first message
```

Message and reply are written together, so a crash can never leave a question
saved without its answer. An answer with no text (for example a model that
spent its whole budget thinking) is an error and is never saved; Umbrous also
asks Ollama not to think (`think: false`). As in M1, if no reply text arrived the message is not
kept, so pressing Send again does not duplicate it. If the page closes
mid-reply, the model is stopped and what arrived is kept as "stopped".

The chat's title is made locally from the first message (whitespace squeezed,
cut at 80 characters). No model is asked to write a title: that would be one
more copy of your text in one more place.

## The pieces

| File | Job | Boundary |
|---|---|---|
| `persistence/db.py` | Schema v3: `projects`, `chats`, `messages` | Migration only appends; an M1 database upgrades in place keeping launches and probe results |
| `history/store.py` | The only code that reads or writes chats | Has no delete, remove or purge function (a test checks the names) |
| `runloop/chat.py` | Loads turns from the store, saves the exchange | The "reply is running" lock is the only per-chat state in memory |
| `api/server.py` | History routes below | Same credential, Host and Origin checks as every `/api/` route; DELETE answers 405 |
| `web/chat.js` | Sidebar, transcript, Options menu | Text nodes only, as in M1, including saved replies |

### API

| Route | Does |
|---|---|
| `GET /api/projects` | Projects with counts of active and archived chats |
| `POST /api/projects {name}` | Add a project (1 to 60 characters, unique ignoring case in any language) |
| `GET /api/chats?project_id=…[&archived=1]` | One project's chats, newest first; archived ones only when asked |
| `POST /api/chats {project_id}` | Start a chat |
| `GET /api/chats/<id>` | The chat and its full transcript |
| `POST /api/chats/<id>/archive` · `/unarchive` | Archive or restore. Refused (409) while a reply is running in that chat |

Ids are checked against strict patterns before they reach SQL, and every query
uses `?` parameters, so a crafted id is refused, never interpreted.

### In the page

- **Projects**: the real list. The **+** that appears when you hover the
  Projects header opens a name box (Enter saves, Escape cancels; a duplicate
  name is refused in place). Clicking a project shows its chats and starts a
  new chat in it. Session ▸ Project in the Details panel names it.
- **Sessions**: the chats of the selected project. Click one to load its
  transcript. "Show archived (n)" lists archived chats, marked *archived*.
- **Project names** are compared the way people read them: Unicode-normalised
  and fully case-folded (a separate `name_key` column holds that form), so
  "École", "école" and "STRASSE"/"Straße" count as the same name. SQLite's own
  `NOCASE` only folds A to Z, which is why it is not used.
- **Options** (⋯ in the header): Archive chat, or Restore chat when it is
  archived, and a greyed-out **Delete chat**: "Not available yet".
- An archived chat opens read-only: a notice with a **Restore** button, and the
  message box is disabled. The server enforces this too: sending to an archived
  chat is refused before any model is called.
- Switching chats while a reply is streaming stops it (what arrived is kept,
  marked stopped).
- Archiving while a reply is streaming first stops it and waits until the
  partial reply is saved, then archives. The runtime enforces the order:
  archive takes the same per-chat lock as a reply, so it is refused while a
  reply is running (for example from a second tab) and can never slip in
  between the reply and its save.

## Where copies of your chats exist

| Place | What | Notes |
|---|---|---|
| `~/Library/Application Support/Umbrous/umbrous.db` | All transcripts | Mode 0600 (only your user). Not encrypted: anyone who can read your files as you can read it |
| `umbrous.db-journal` next to it | Pages being changed, during a write | Exists only for the moment of a write. Same permissions |
| Free pages inside `umbrous.db` | Old copies of changed rows | SQLite reuses them but does not wipe them; matters for a future delete (needs `secure_delete` or `VACUUM`) |
| Time Machine and other backups | Snapshots of the whole folder | Outside Umbrous's control. Exclude the folder in Time Machine settings if you don't want this |
| `logs/` | **No content.** Ids, counts, timings, outcomes | Project names and chat titles are not logged either (a test checks) |
| The browser | Nothing kept | The page uses no browser storage, and every API answer is `Cache-Control: no-store` |
| Ollama | Nothing kept by default | The prompt sits in Ollama's memory while the model is loaded. With `OLLAMA_DEBUG=1` its own server log can include prompts |

## Deleting is not available yet

"Delete" has to be decided before it is built, because a quick version would
promise more than it does. The open questions:

1. Undo: a recovery period (e.g. 30 days in a "Recently deleted" place) or
   immediate?
2. The database file: overwrite freed pages (`PRAGMA secure_delete`) and
   `VACUUM`, or accept that deleted text can linger in the file?
3. Backups: Umbrous cannot reach Time Machine copies. Say so in the UI?
4. Derived copies: once memory (M6) and receipts (M3/M4) exist, does deleting
   a chat delete facts learned from it, and the receipts of tools it ran?
5. Projects: can a project with chats be deleted, and where do its chats go?

Until those are answered there is no delete path at all. Tests check that
`DELETE` routes answer 405, that `/delete` and `/purge` paths do not exist,
that the store has no such method, and that the page's Delete item is
disabled. `bin/umbrous purge logs` still only touches logs.

## Changes to existing files

- `prototypes/umbrous-harness.html`: an id on the Options button
  (`#chat-options`), an inner `#sess-project` span, and CSS under
  `/* M2: history */`. The prototype's inline script is unchanged; opened as a
  file it still shows its placeholder sidebar.
- `runloop/chat.py`: the in-memory `ChatStore` is gone; `ChatLocks` keeps only
  the busy lock. Chat ids now come from the runtime (`POST /api/chats`), and an
  unknown id is refused instead of starting a new chat.
- `web/chat.js`: the M1 notice "kept in memory only" is replaced; chats are
  created on the first Send in the selected project.
- `tests/helpers.py`: `RunningRuntime(home=…)` can restart on the same data;
  `new_chat()` and `get_json()` helpers.
- Version 0.2.0 (M2), schema 3.

## Demonstrate

1. `bin/umbrous`. The sidebar shows **General**, with no chats.
2. Hover **Projects**, press **+**, type `Garden`, Enter. Send "When should I
   plant tulips?" and a follow-up. The chat appears under Sessions, titled
   with your first message.
3. Press **New chat**, send something else. Click **General**: its list is
   empty. Click **Garden**: both chats.
4. Ctrl-C the runtime, run `bin/umbrous` again, click **Garden**, open the
   tulip chat. The whole transcript is back, and a new message continues it
   (the model sees the earlier turns).
5. ⋯ ▸ **Archive chat**. It is marked archived, the box is disabled, the
   messages are all there. Delete is greyed out. Press **Restore** and carry on.

## Exercise: see what archive really changes

With the runtime stopped:

```sh
DB="$HOME/Library/Application Support/Umbrous/umbrous.db"
sqlite3 "$DB" "SELECT id, title, archived_at FROM chats;"
sqlite3 "$DB" "SELECT chat_id, seq, role, status, substr(content,1,40) FROM messages;"
```

Archive a chat in the app, stop it, and run both again: one `archived_at`
value changed, and the `messages` rows are identical. Then try
`sqlite3 "$DB" ".schema messages"` and find the `CHECK` constraint that refuses
an unknown status. (Read only: don't edit the file while Umbrous is running.)

## Tests

| File | Covers |
|---|---|
| `tests/test_m2_history.py` | General exists; project names (unique across Unicode case and forms, length); empty replies never saved; archive refused while a reply runs and the reply is saved; stop then archive keeps the partial reply; chats need a real project; order, title, model and token counts saved; long titles cut; projects kept apart (also in what the model is sent); archive round trip with identical row counts; archived chats refused before the model is called; no delete method, route or verb (405/404); unknown ids and injection-shaped ids refused; partial replies saved with their status; history needs the credential and the right Origin; no content or names in logs; an M1 database upgrades keeping its data; **restart persistence in the right project**, and a continued chat sends the saved turns |
| `tests/test_browser_m2.py` | Headless Chromium across a real restart: add a project, duplicate refused, chats listed and titled, projects separate, transcript reload, saved HTML reply still inert, Options menu with disabled Delete, archived chat read-only, restore and continue, archive while a reply streams (stopped, saved, then archived), after restart the archived chat is hidden behind "Show archived (1)". Skipped without Node + Playwright |
