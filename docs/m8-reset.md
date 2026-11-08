# M8: Selective start-fresh reset

M6 gave Umbrous memory and M7 let tasks suggest additions to it. M8 lets you
take things away again, on purpose and visibly: choose chats, personal memory
and/or project memory, see **exactly** which items would go and what would
stay, and only a confirmation of that preview deletes anything. Builds on M7
(`m7-review`).

```
you tick chats and memory ──▶ Preview ──▶ the exact items, what stays, a preview id     (stores nothing)
                                 │
                 Cancel ◀────────┤  (nothing to undo: nothing was sent that deletes)
                                 │
                 Delete N items ─┴─▶ the runtime resolves the selection again, inside the write transaction
                                     ├─ the same items, unchanged ──▶ deleted, secure_delete on; the result says what stays
                                     └─ anything different ─────────▶ refused (preview_changed) with the new preview; nothing deleted
```

## Run it

```sh
bin/umbrous                                   # Details › Start fresh…, or Options (⋯) › Delete chat…
bin/umbrous reset --chat chat_… --personal mem_…          # preview: the exact items and what stays
bin/umbrous reset --all-chats General --project-memory General --personal all
bin/umbrous reset --chat chat_… --confirm rst_…           # delete exactly that preview (quit the app first)
bin/umbrous test                              # 329 tests; no Ollama needed
```

In the browser page: Details › **Start fresh** lists this project's chats
(archived too), personal memory and this project's memory as tick boxes, with
"All …" boxes per store and **Start a new chat afterwards** (on by default).
**Preview** shows the list in a red frame with **What stays** under it, then
**Delete N items** or **Cancel**. Options › **Delete chat…** opens the same
panel with the open chat ticked and its preview shown.
In Umbrous.app: the right panel's **Start fresh…** button (under Memory) and
Options › **Delete chat…**, the same steps (below).

## Words used here

| Word | Means |
|---|---|
| **Selection** | What you ticked. Each part names one store: `chats` (ids), `chats_in` (all chats of one project, archived too), `personal` (`"all"` or ids), `project` (`{project id: "all" or ids}`). There is no key for "everything" |
| **Preview** | The runtime's answer for a selection: the exact items (chats with their message counts, memory items with their text, suggestions waiting from those chats), counts, what stays, and a **preview id**. Making one changes nothing |
| **Preview id** | `rst_` + a fingerprint of the selection and of each item's state (a chat's message count, last message, archive state and pending turn; a memory item's text, kind, scope and edit time; a waiting suggestion's text, quote, kind, store, scope and edit mark). Same state, same id |
| **Confirm** | `POST /api/reset {selection, preview_id}`: deletes exactly that preview, or nothing |
| **Changed preview** | Anything that would make the id differ: a new message in a selected chat, a new chat or item now matched by "all", an edit, an archive, an item already gone. Confirm is refused and the new preview comes back to be checked |
| **What stays** (`kept`) | Records that refer to the selection but are not part of it, with counts, shown in every preview and every result |

## What a reset deletes

| You select | Deleted, in one transaction | Not deleted with it |
|---|---|---|
| a chat | its row, all its messages, its pending turn (M5), and the suggestions from it still **waiting** for review (M7: they quote it) | its task records and receipts; memory items accepted from it; accepted and rejected suggestions (no text, a hash) |
| a personal memory item | its row | replies that were given it (in chats you keep) |
| a project memory item | its row | the same |
| "all chats in P" / "all personal" / "all of P's memory" | every item of that one store as listed in the preview | everything outside that store |

Nothing else is ever deleted by a reset: not projects, workspaces, grants,
settings, receipts, task records, logs or another project's anything.
Settings and skills are outside reset scope (the plan keeps them out until a
separate decision). A chat whose reply is still running is refused
(`chat_busy`); stop it first.

## A preview, then exactly that preview

The roadmap's rule is "items added or changed after preview require
revalidation rather than a broader deletion". Here is how that is enforced,
not just intended:

1. **Preview** resolves the selection to items and hashes their state into
   the preview id. It writes nothing: there is no stored preview, so Cancel
   needs no request and leaves nothing behind (the browser test checks that
   Cancel sent no request besides the preview).
2. **Confirm** sends the selection and the id. The runtime takes the chats'
   reply locks (refusing a chat that is busy), opens `BEGIN IMMEDIATE`, which
   keeps every other writer out until it ends, and **resolves the selection
   again on that connection**. Only if the id matches does it delete, by the
   ids it just resolved, and commit.
3. If the id does not match, it rolls back and answers `409 preview_changed`
   with the new preview. Nothing was deleted, not even the unchanged items.
   The page and the app show the new list with "What this selection covers
   changed since the preview"; Delete then confirms the new one.

So "All personal memory" means the items you saw, not whatever matches at the
moment of the click. If a new item appears in between, you see it before it
can go. Two confirms at the same time delete once; the second gets
`preview_changed` (tested with two threads).

The page and the app also never let a preview outlive its selection. Any
change to the ticks clears the preview and its Delete, and a preview answer
that arrives after the ticks changed is thrown away, so Delete only ever
sends the selection that is ticked now. In the app the ticks fold away while
a preview is shown ("Change selection" brings them back and drops the
preview), and Delete sits right under the items with "What stays" below it, so
Delete is in view at the default window size; the page scrolls Delete into view.
The app keeps Start fresh's generation on the workspace, not in the panel, and
bumps it on open, close, every tick change and every preview, so a reply to a
panel that was closed (or reopened for another chat) is never shown. The e2e
run checks this with a held preview for one chat, cancelled, then Delete chat…
on another.

## What stays, and why the result says so

Every preview and every result carries `kept`, written by the runtime so the
page, the app and the CLI say the same thing:

- **Task records and receipts** of reset chats: kept until you purge them
  (`bin/umbrous purge tasks`, `purge receipts`). They hold ids, statuses,
  file paths and line numbers, never message or reply text (M3, M4). After a
  reset they name chat ids that no longer exist. This is the M0/M4
  retention rule unchanged: records are purged on their own, never as a side
  effect.
- **Memory items accepted from a reset chat**, unless you tick them too:
  their source names a chat that no longer exists.
- **Review history**: accepted and rejected suggestions from those chats keep
  their id and hash (no text, M7). A rejected one is still not offered again.
- **Replies in chats you keep that were given a reset memory item.** They
  stay as written and may repeat its text, and that text goes back to the
  model as history in its own chat (the M6 rule: deleting stops retrieval,
  it does not rewrite transcripts). The preview counts them and says how to
  get a context without them: reset those chats too, or start a new chat.
- **Not touched**: projects, workspace folders, grants, settings; logs, which
  never held the text and keep ids, including those of what was reset.
- **Backups**: Umbrous keeps no backup or undo of its own, so a confirmed
  reset cannot be reversed in Umbrous. A copy of the data folder made before
  the reset (Time Machine, another backup tool, a copied folder) still holds
  the items, and Umbrous cannot reach it.

This is the deletion contract M2 left open, settled for M8 on the
recommended terms: permanent, no recovery copy, records kept and named.
Nothing pretends to be complete erasure while a copy remains.

## Derived caches and the fresh context

The roadmap asks to invalidate retrieval indexes and caches. M6 chose to have
none: retrieval reads the memory tables on every reply, so a deleted item is
simply not in the next read. What M8 does clear:

- **In the database file**: deletion runs with `secure_delete` on, so freed
  pages are overwritten, and the database uses a rollback journal (no WAL
  with old pages). A test reads the raw file before and after: the marker
  text of a reset chat, memory item and waiting suggestion is gone.
- **In the runtime**: the per-chat reply lock of each reset chat is dropped.
- **In the page and the app**: the chat list, the Memory section and the
  "last reply used" note are reloaded; if the open chat was reset, or
  **Start a new chat afterwards** was ticked, a new chat opens. That is the
  "fresh active context": no history, and memory retrieval that no longer
  finds the reset items. A test asks the fake model in that new chat and
  checks its prompt does not contain the reset item's text.

## The API

```
POST /api/reset/preview {selection}             200 {preview_id, items, counts, kept, not_touched, backups, blocked, empty}
POST /api/reset {selection, preview_id}         200 {done, deleted: {chats, messages, pending_turns, personal, project,
                                                     suggestions}, items, kept, not_touched, backups, start_fresh}
```

Errors: `nothing_selected`, `bad_selection` (400); `no_such_chat`,
`no_such_memory`, `no_such_project` (404); `preview_changed` and `chat_busy`
(409, with `preview`). Same session credential, Host and Origin checks as every
`/api/` route; `GET` answers 405. The log line `reset.done` has counts and up
to 50 ids, never a title or memory text.

## The desktop app

The right panel's Memory section ends with **Start fresh…**. It replaces the
section's contents while open:

- tick boxes: **All chats in P (n, archived too)** and each chat; **All
  personal memory** and each item; **All of P's memory** and each item;
  **Start a new chat afterwards** (on);
- **Preview**: the exact items in a red frame, each with its kind and message
  count, then What stays and the backup note; **Delete N items** and
  **Cancel**. Changing any tick drops the preview, so only a preview you saw
  can be confirmed;
- a refused confirm shows the new preview with the warning in yellow;
- done: the counts, what stays, then the lists reload and, as above, a new
  chat opens.

Options (⋯) › **Delete chat…** opens it with the open chat ticked and its
preview shown. `desktop/e2e/validate-m8.sh` (in `validate-native.sh`) drives
this in the real window with a fake Ollama that repeats the memory it is
given: Delete chat… previews one chat and Cancel keeps it; a mixed selection
(the chat, one personal item, all project memory) is refused after a project
item is added, the new preview (4 items) is confirmed, a new chat opens and
its reply contains the item that stayed and not the one reset. From outside
the app it then checks the database (only the kept chat's reply still holds
the reset text, as the preview said; no other copy in the file), the logs
(ids only, `reset.done` present) and that a CLI preview deletes nothing.

## The pieces

| File | Role |
|---|---|
| `umbrous/reset.py` | `normalize`, `Reset.preview`, `Reset.confirm`: the only code that deletes chats; memory and suggestion rows go in the same transaction |
| `umbrous/api/server.py` | the two routes; `ChatLocks.forget` (`runloop/chat.py`) drops a reset chat's lock |
| `umbrous/cli.py` | `reset`: preview by default, `--confirm PREVIEW_ID`, refused while the runtime runs |
| `umbrous/web/chat.js`, `prototypes/umbrous-harness.html` | Details › Start fresh, Options › Delete chat… |
| `desktop/app/src/api.rs` | `ResetChoice` (the selection), `ResetPreview`, `ResetDone` |
| `desktop/app/src/workspace.rs` | `open_reset`, `toggle_reset`, `preview_reset`, `confirm_reset`, `render_reset` |
| `desktop/app/src/e2e.rs`, `desktop/e2e/scripts/m8-reset.txt`, `desktop/e2e/validate-m8.sh` | the native check |

`history/store.py` still has no delete function (the M2 test that checks its
names still passes): deleting chats lives in one module that always previews
first.

## Demonstrate

Use disposable data: `bin/umbrous --home /tmp/umbrous-m8` (a separate data
folder) with a model you have, or the fake one from the tests.

1. Make two chats in General and add two personal items and one project item
   (Details › Memory). Ask something in each chat so the replies use memory.
2. Start fresh…: tick one chat, one personal item and **All of General's
   memory**. Preview. Read What stays: the other chat's reply was given these
   items and keeps their text.
3. Cancel. Everything is still there; `bin/umbrous --home /tmp/umbrous-m8
   memory` shows it.
4. Start fresh… again with the same ticks, Preview, then add a project item in
   another tab (or `bin/umbrous … memory add project "x" --project General`).
   Delete: refused, with a 4-item preview. Delete again: done, and a new chat
   opens.
5. Ask "What do you remember?" in the new chat. Then quit and run
   `bin/umbrous --home /tmp/umbrous-m8 tasks --last 5` and `receipts`: records
   of the reset chat, if it ran tasks, are still listed with its id.

## Exercise: catch a too-broad confirm

`ConfirmTests.test_a_changed_preview_needs_revalidation` covers five kinds of
change. Add a sixth: preview `{"chats_in": [project]}`, archive one of that
project's chats, confirm with the old id, and assert `preview_changed` with
nothing deleted. Then remove `archived` from the chat fingerprint in
`reset.py` (`_fp` for chats) and watch your test fail: an archive alone would
no longer count as a change. Put it back.

## Tests

| Test | Checks |
|---|---|
| `test_m8_reset.PreviewTests` (4) | the exact items and counts; a preview stores nothing and is stable; all chats of one project includes archived ones and no others; no implicit delete-all and every bad selection; what stays (task records and receipts, memory from the chat, review history, replies using the memory) |
| `ConfirmTests` (11) | cancel is a no-op; a mixed selection deletes exactly the preview (a diff of every table); reset memory never enters a fresh context; five kinds of change need revalidation and nothing is deleted; a waiting suggestion edited in review (text, kind, store) needs revalidation; an item gone since the preview; a wrong or foreign preview id; a busy chat refuses the whole reset and the lock is forgotten after; a chat takes its pending turn and waiting suggestions only; the freed space holds no trace; two confirms at once delete once |
| `ApiTests` (2) | preview, cancel, confirm, a fresh chat's prompt lacks the reset text, the log has no text; a changed preview over HTTP, errors, 405, the session credential |
| `CliTests` (2) | preview then `--confirm`; refused while the runtime runs |
| `test_browser_m8` | Delete chat… previews the open chat; Cancel sends nothing; unticking after a preview removes its Delete, and a preview answer for older ticks is discarded; a changed preview is shown, not silently widened; confirm, new chat, the next prompt lacks the reset memory |
| `test_validate_scripts` (M8 cases) | `validate-m8.sh` fails on any app failure and on wrong records: nothing deleted, too much deleted, memory left, text left in the freed space, the fresh reply given reset memory, memory text in a log, no `reset.done` |
| `api.rs` `reset_choice_names_each_store_and_never_everything`, `reset_preview_and_result_parse` | the app's selection and its reading of previews and results |
| `test_browser_m2` | Options now offers **Delete chat…** (it opens the preview; it deletes nothing itself) |

## Not in M8

- An undo or a trash. A recovery copy would be a copy of exactly what you
  asked to remove; if one is wanted later it needs its own decision and a
  retention period.
- Purging task records and receipts as part of a reset: they stay separate,
  as in M4; purge them with their own commands.
- Resetting settings, workspace grants or skills, and deleting projects.
- Rewriting kept replies that repeat a reset item: reset those chats too.
