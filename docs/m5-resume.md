# M5: Recover and resume

M4 recorded each task as it ran. M5 deals with the case where the runtime
stops in the middle of one: a crash, a forced quit, the app killed, the Mac
restarted. After the restart Umbrous says plainly which task was cut off and
what is known about it, keeps what had completed, and offers to **resume**:
run the task again, under the settings as they are now. Builds on M4
(`m4-task-state`).

Everything is still read-only, and still a permission check inside Umbrous,
**not an operating-system sandbox.** There is no promise of "exactly once":
a resumed read happens twice, and that is safe only because reads change
nothing.

## Run it

```sh
bin/umbrous tasks                    # an interrupted task shows as: unknown  interrupted
bin/umbrous tasks --task tsk_…       # its trace, the rebuilt report, and whether it can be resumed
bin/umbrous test                     # 247 tests; no Ollama needed
```

In the browser page and in Umbrous.app, opening a chat whose last task was
cut off shows your message, **Task interrupted**, what was recorded, and a
**Resume** button.

## Words used here

| Word | Means |
|---|---|
| **Interrupted** | The runtime stopped while the task ran. The task's status is `unknown`, its code `interrupted` |
| **Unknown** (outcome) | Nobody knows whether it did its work. Not the same as failed (below) |
| **Recovery** | What the runtime does once at startup, before serving anything: it closes every task left open and writes what is known |
| **Reconcile** | Settle an action that was still open, from the evidence available: its receipt, or its absence |
| **Retry class** | Per tool, what may happen to a run of it whose outcome is unknown (below) |
| **Resume** | Run the interrupted task again as a **new** task, from its waiting message, under the current gate. Nothing is replayed |
| **Pending turn** | The message of a task that is running, kept until its exchange is saved, so a crash cannot lose it |

## Unknown versus failed

A **failed** action is known not to have done its work: the tool reported an
error (a file too big, binary, gone), or it was cancelled before it ran.
Doing it again is a new attempt at something that did not happen.

An **unknown** action may or may not have done its work. It started, and then
nothing was recorded: the runtime died, or the tool hung past its time limit.
The only way to know is to **check its effect**. For a read, there is no
effect to check, so the question does not matter. For a write, it is the
whole question: if "append a line" ran and the record was lost, running it
again appends a second line.

So Umbrous never turns unknown into failed to make a retry look safe, and
never treats showing a warning as permission to repeat something blindly.

## What recovery does

At startup (`umbrous/tasks/recover.py`, called from `make_server` before the
server answers any request; `runtime.json` makes sure only one runtime uses a
data folder), for each task still `requested` or `started`:

| What the record shows | Becomes | Why |
|---|---|---|
| an open action **with a receipt** | the receipt's outcome: allowed → completed, denied, failed, uncertain → unknown | the gate writes the receipt after the tool returns, so the receipt is what happened; the crash only cut off the task's own note of it |
| an action that **started**, no receipt | **unknown**, code `interrupted`, with its retry class | it was running; nothing says whether it finished |
| an action only **requested**, no receipt | **cancelled**, code `interrupted` | it never ran (waiting for its turn, or for your approval) |
| the task, if you had pressed **Stop** | **cancelled** (`cancelled`, or `task_time_limit`) | you had asked for it to end; it is not offered for resuming. A `stop.requested` event is written the moment Stop is pressed, for this |
| the task otherwise | **unknown**, code `interrupted` | |

Each reconciled action gets an `action.reconciled` event saying what it was,
what it became and why (`reconciled_from: receipt`, or its `retry` class). The
task gets a `task.interrupted` event and a report rebuilt from the record:
the same keys as an M4 report, evidence taken from the receipts of completed
actions, a failure line for every action that did not complete, and in
`uncertainty` the plain statement that Umbrous stopped mid-task, one line per
unknown action, and whether any reply text was lost. `recovered: true` marks
it.

Recovery runs once per start and touches only open tasks; a second restart
changes nothing. If one task's record cannot be read, it is logged (ids only)
and the runtime still starts.

M4's checkpoints are not needed for this: they summarise counts after each
step, while the action rows, events and receipts say exactly which action was
where. They stay in the trace.

### A reply saved just before the crash

The exchange is now stored **before** the task's report is written. If the
runtime dies between the two, the reply is in the chat, and the task is
recovered as interrupted with "The reply was saved before Umbrous stopped, but
the task's own report was not". It is not offered for resuming: there is
nothing to resume.

### The pending turn

Until M4, your message was stored together with the reply, at the end. A
crash mid-task lost both, and you had to type the message again. M5 keeps the
message of a running task in its own table, `pending_turns` (schema 6), from
the moment the task starts:

- stored with its reply at the end, in the same transaction that removes it;
- removed if the reply ends with nothing to keep (the M2 rule: an empty reply
  is not saved, and neither is its message);
- removed if you send a new message in that chat instead of resuming (the old
  task gets `task.superseded`; resuming it after a newer message would put the
  conversation out of order);
- removed with its task by `bin/umbrous purge tasks` (foreign-key cascade,
  `secure_delete` on).

It is chat content, written by the history store, never sent to a model as
context, and never in a task table. It lives only as long as the task is
running or interrupted: nothing is retained indefinitely.

## Retry classes

Each tool in `gate/gate.py` `TOOLS` declares `retry`:

| Class | Tools | When its outcome is unknown |
|---|---|---|
| `repeat` | `list_files`, `read_file`, `search_files` (all of M5's tools) | may run again under the current gate, with a **new** receipt, and a warning that the data may have changed since |
| `verify` | none yet; every future write tool, and any tool that declares nothing | its effect must be checked first. Resume is refused (`needs_verification`) until that tool has a check |

### What a future write tool would need

Before any write tool is enabled, it needs, at least:

1. **An intent record before it acts.** The action row and an event with what
   it is about to do (path, operation, the hash of the content to write, the
   file's hash before), so a crash leaves enough to check against.
2. **A way to check its effect.** After a crash: read the target and compare.
   For "write file X with content C", the file's hash is either the old hash
   (did not happen), the hash of C (happened), or something else (changed by
   someone, or partly written): three answers, and the third is not "retry".
3. **An idempotency strategy where possible.** Write to a temporary file and
   rename it into place (atomic: the file is old or new, never half); make the
   operation "set to C" rather than "append"; or carry an idempotency key the
   target remembers.
4. **A rule for the third answer.** If the check is inconclusive, the action
   stays unknown and the task is not resumed automatically; you are shown what
   is known and decide.

None of this is promised for tools in general. Each write tool has to bring
its own check before it can be resumed.

## Resume

`POST /api/chat {chat_id, model, resume_task}` (the page and the app use this
when you press Resume). Under the chat's lock, the runtime checks:

| Check | Refusal |
|---|---|
| the task exists, in **this** chat (a purged task is gone) | `no_such_task` |
| the chat is not archived | `chat_archived` |
| the task is `unknown` / `interrupted`, not already resumed, its message still waiting, and every unknown action has retry class `repeat` | `not_resumable` (with the reason) |
| the project still has a workspace and the model passed the tool check | `resume_scope_changed` |
| every read the interrupted task made (completed or unknown) is still allowed **now**: checked by `gate.evaluate` against the current workspace, profile and grants | `resume_scope_changed` (blocked now: a detached workspace, an excluded path), or `resume_needs_approval` (needs approval and this app cannot ask) |

A refused resume leaves the task interrupted, with a `resume.refused` event,
and replays nothing. Fix the cause (grant again, switch the profile, resume
from the browser page) and resume again.

Then it runs as a **new task** from the waiting message:

- every tool call goes through the gate as usual: current profile, current
  grants, new approval requests (an approval answered before the crash died
  with the old process and is never reused), new receipts;
- the old task gets `task.resumed` (by: the new id), the new one
  `task.resume_of`, and the report `resume_of`;
- the report notes that the reads were repeated and nothing was reused, and,
  for each file both attempts read, whether it **changed** since (by SHA-256);
- Stop, the limits and the task time limit work as for any task. A resume that
  ends with nothing to keep drops the message, as any reply does (M2).

## The desktop app

When you open a chat whose last task was interrupted, Umbrous.app shows a
notice above the composer: **Task interrupted**, your message, the recovered
report in one line, what resuming does, and a **Resume** button. Resume
streams like a send ("answering again (resumed)…") and the notice goes away.
The trace is in the browser page and `bin/umbrous tasks --task`.

Validation (`desktop/e2e/validate-m5.sh WORKDIR`, also part of
`validate-native.sh`) runs the e2e app against a fake Ollama:

1. a finished exchange, so the chat is saved;
2. a slow task: its first step lists the workspace (receipt saved), then its
   answer streams; **the runtime is killed with SIGKILL** mid-answer;
3. Retry restarts it; the reopened chat shows the interrupted task and its
   message;
4. Resume runs it again; it completes, and the chat holds four messages.

Then it checks the database from outside: statuses completed, unknown
(`interrupted`), completed; the interrupted task kept its completed action and
has `task.interrupted` and `task.resumed`; the resumed one has `resume_of` and
its own receipt; no message is left waiting; no message text in any task
table or log.

## Privacy and retention

M4's rules hold: task tables hold ids, statuses, paths, codes, hashes and
counts, never message, reply or file text, and every value is redacted and
clipped. The new pieces:

- `action.reconciled`, `task.interrupted`, `task.resumed`, `task.resume_of`,
  `task.superseded`, `stop.requested`, `resume.refused`: ids and codes only.
- The pending turn holds your message, for as long as the task runs or waits
  to be resumed (above), and is purged with its task.
- Logs: `task.recovered` (counts), `task.recover_failed` (ids), never text.

## The pieces

| File | What |
|---|---|
| `umbrous/persistence/db.py` | schema 6: `pending_turns` |
| `umbrous/tasks/recover.py` | `recover()`, the rebuilt report, `resume_info()`, `scope_check()`, `retry_class()` |
| `umbrous/tasks/store.py` | `recover_action`, `recover_task`, `open_tasks` |
| `umbrous/history/store.py` | `begin_pending`, `pending`, `move_pending`, `drop_pending`, `has_reply`; `append_exchange` clears the pending turn |
| `umbrous/runloop/chat.py` | `resume_task`, the pending turn, `stop.requested`, the exchange stored before the report |
| `umbrous/tasks/run.py` | `resume_of`, the changed/unchanged file notes |
| `umbrous/gate/gate.py` | `retry` per tool |
| `umbrous/api/server.py` | recovery at startup; `resume` in `/api/tasks`; `resume_task` on `/api/chat` |
| `umbrous/web/chat.js` | the interrupted card and Resume |
| `desktop/app/src/workspace.rs`, `api.rs`, `e2e.rs` | the notice, Resume, `resume` / `expect interrupted` |

## Demonstrate

```sh
python3 -m tests.fake_ollama --port 11434 &          # includes multi-step:3b
bin/umbrous --home /tmp/u5 probe multi-step:3b
bin/umbrous --home /tmp/u5 workspace General "$(python3 desktop/e2e/stress.py seed-workspace /tmp/u5-ws)"
bin/umbrous --home /tmp/u5 serve --no-open &         # note the URL; open it
```

1. **Before a result is saved.** Ask something; kill the runtime
   (`kill -9 %2`) while the first step is still streaming. Start it again.
   `tasks --task` shows `task.interrupted`, no actions, and "Reply text that
   had arrived before the stop was not saved".
2. **After a result is saved.** Ask again; kill it after the first tool result
   (the step line has moved on). After the restart the action is `completed`
   and its evidence is in the report; resume, and the new report says whether
   the file changed. Edit the file in between to see "changed since".
3. **Compare the two** with `tasks --task` for each: the reconciled events,
   which actions completed, and the `resume:` line at the end.

The automatic version is `tests/test_m5_resume.py` `CrashTests`.

## Exercise: write the crash-recovery test

Try it before reading `test_kill_while_a_read_is_running`:

1. Start the runtime in a child process (`serve --desktop`) with `read_file`
   patched to hang, a fake Ollama whose model reads one file.
2. Send a message; when the `tool_call` event arrives, wait until the read has
   started, then SIGKILL the child.
3. Start a new child on the same data folder. Check the task is `unknown` /
   `interrupted`, its action is `unknown` (not failed) with no receipt and
   `retry: repeat` on its `action.reconciled` event, and resume is offered.
4. Resume, and check the new task completed with its own receipt, the message
   is stored once, and the old task cannot be resumed a second time.

## Tests

| Test | Checks |
|---|---|
| `test_m5_resume.CrashTests` (4) | real SIGKILL of the runtime: mid-stream before any action; after a read's receipt was saved (and the file then changed); while a read was running; while an approval was pending. Each restarts, shows the interrupted task, and resumes once |
| `test_each_open_action_is_reconciled…`, `test_unknown_is_not_failed` | every reconcile rule; the rebuilt report; a second restart changes nothing |
| `test_stop_before_the_crash…`, `test_a_reply_saved_before_the_crash…`, `test_an_action_that_cannot_be_repeated…` | Stop wins; a saved reply is not resumed; a `verify` tool blocks resume |
| `test_resume_runs_under_the_current_gate…`, `test_revoked_grant_stops_the_resume`, `test_resume_where_approval_can_be_asked_asks_again`, `test_detached_workspace_stops_the_resume` | new receipts; old approvals and revoked grants are never replayed |
| `test_archived_chat…`, `test_purged_task…`, `test_a_task_from_another_chat…`, `test_a_new_message_replaces…`, `test_stop_during_a_resume…` | archive, retention, chat scope, supersede, cancellation |
| `PendingTurnTests` | no pending message after a task; none reaches the model; `drop_pending` never touches the transcript; schema 5 → 6 keeps data |
| `test_browser_m5` | the interrupted card, Resume, and the reopened chat in the page |
| `test_validate_scripts` (M5 cases) | `validate-m5.sh` fails on any app failure or wrong records |
| `api.rs` `interrupted_task_parses_with_its_resume_info` | the app reads `resume` |

## Not in M5

- Write tools, and so any `verify` check: the class exists so that the first
  write tool has to bring one.
- Resuming from the middle (reusing completed results). A resume repeats the
  reads; the old results may be stale and are not trusted.
- Recovering the part of a reply that had streamed before a crash: it was
  never stored (M2 stores replies at the end) and is not now.
- A retention period: still kept until purge, pending the decision.
