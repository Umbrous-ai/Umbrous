# M4: Durable task state

M3 gave each tool attempt a receipt. M4 adds the **task** those attempts
belong to: what the model did, step by step, how it ended, and a report of what
was actually checked. It also puts limits on a task, and makes Stop reach into
whatever is running. Builds on M3 (`m3-gate-read`).

Everything is still read-only, and still a permission check inside Umbrous,
**not an operating-system sandbox.**

## Run it

```sh
bin/umbrous workspace General ~/code/umbrous-test-ws
bin/umbrous                          # ask something that needs a few files
bin/umbrous tasks                    # recent tasks: status, steps, actions
bin/umbrous tasks --task tsk_…       # one task's trace (every event) and report
bin/umbrous purge tasks --all --yes  # delete task state (receipts are separate)
bin/umbrous test                     # 222 tests; no Ollama needed
```

In the browser page, each reply that used tools ends with a **report**. Its
**Trace** button shows every event in Details. In Umbrous.app the report is one
line under the reply.

## Words used here

| Word | Means |
|---|---|
| **Task** | One message that may use tools, from your send to its ending. Same `tsk_` id as its receipts and its stored reply |
| **Step** | One model turn. A step either asks for tools or writes the answer |
| **Action** | One tool call the model proposed. Same `act_` id as its receipt |
| **Event log** | The task's timeline, in order (`seq` 1, 2, 3…): requested, started, each step and action, limits, the end |
| **Checkpoint** | A small snapshot after each step: counts, tokens used, limits reached. M5 will use these to explain an interrupted task |
| **Artifact** | Something a task produced. In M4 only one kind: the **report** |
| **Evidence** | A pointer to what a tool actually returned (see below) |
| **Uncertainty** | Anything that makes the answer less than fully checked |

## The state model

```
task      requested ──▶ started ──▶ completed     the model answered (maybe with things not fully checked)
                            ├─────▶ failed        a model error, a timeout, the task time limit, no answer
                            └─────▶ cancelled     you pressed Stop, or the page went away
          (unknown: a task found mid-run after a crash or restart, since M5: docs/m5-resume.md)

action    requested ──▶ started ──▶ completed     the tool ran and returned
              │             ├─────▶ denied        refused while running (it turned out to be a link)
              │             ├─────▶ failed        the tool reported an error (file too big, binary…)
              │             ├─────▶ cancelled     Stop arrived while it ran
              │             └─────▶ unknown       it did not report back within tool_timeout_s
              ├──▶ denied                         the gate refused it (outside, excluded, a limit)
              └──▶ cancelled                      Stop arrived before it ran: never run, no receipt
```

The store refuses any other move (`umbrous/tasks/store.py`, `TASK_NEXT`,
`ACTION_NEXT`), so an ended task or action cannot change again. Each move is
also an event.

A plain chat with no workspace, or with a model that has not passed the tool
check, is not a task. It is stored in the chat history only, as before.

## Task state, chat history, memory

| | Chat history (M2) | Task state (M4) | Memory (M6) |
|---|---|---|---|
| Holds | what was said: your message, the reply | what was **done**: steps, actions, statuses, limits, the report | what should be remembered across chats |
| Content | full text | no message, answer or file text | short notes you write (no secrets) |
| Goes back to the model | yes, as recent turns | **no**: a task never feeds a later chat | yes, by design |
| Lifetime | until you archive (never deleted by the app) | until you purge (`purge tasks`) | until you delete the item |
| Linked by | `task_id` on the reply row | `chat_id` on the task row | `memory_ids` on the reply row (a reference only) |

Keeping them apart means a task's trace can never leak into a prompt, and
purging tasks never touches a conversation.

## What counts as evidence

Evidence is only what a tool returned, recorded as a pointer:

| Tool | Evidence |
|---|---|
| read_file | the file's path, its SHA-256, its size, and the line range read |
| list_files | the folder's path |
| search_files | the folder plus `query#<hash>` (never the words), then each matching `path:line` |

The model's own text is **never** evidence, and neither is anything from a
denied, failed, cancelled or unknown action. A file the model mentions but no
tool read is not evidence that it says what the model claims.

## Observe + Verify

While the task runs, each completed action reports how complete it was
(`Outcome.observed` in `gate/gate.py`): a search that hit its time or size
limit or skipped files, a read that returned only part of a file. Results are
also cut to fit the context budget, as in M3. All of this goes back to the
model as part of the tool result (a bounded amount of text), and is noted as
**uncertainty** in the report.

When the answer is done, Verify (`tasks/run.py`, `citations()`) finds the
file citations in it: `name:12`, `name:12-30` or `name:12:5`, with or without
folders or an extension, and digit-first names (`README:1`, `src/Makefile:3`,
`migrations/001_init.sql:3`), and `name line 12` for
path-like or well-known names. Each is checked against the lines that a
completed **read_file** returned. A line that only appeared in **search
results** does not count as read, because a search shows each line cut to 200
characters; it is reported as "only seen in search results". A cited file
that was never read, or a line outside the ranges read, is reported too. The
report says how many citations it checked (`citations_checked`); when there
were none, the page says that nothing in the answer was matched to a read,
rather than calling it verified. Only the path and line numbers are kept,
never the answer.

Verify only understands those citation forms. A claim made without one
("the readme says…") is not checked, and the report does not pretend it was.

The report has `status`, `code`, `steps`, `tool_calls`, `actions` (counts by
status), `evidence`, `failures` (each action that did not complete, with its
code and reason), `uncertainty`, `limits_hit`, `limits` and `ms`. It is sent
with the reply's `done` or `error` event and stored as the task's artifact.

## Limits and Stop

Settings in `config.json` (`umbrous/config.py`):

| Setting | Default | What happens at the limit |
|---|---|---|
| `task_max_steps` | 6 | Tools are no longer offered; the model is told to answer from what it has. Report: `steps` limit, uncertainty |
| `task_max_tool_calls` | 24 | Further calls are refused (`tool_call_limit`, with receipts) and tools stop being offered |
| `tool_timeout_s` | 30 | Counted from when the tool starts (after any approval). The action becomes **unknown**; the model is told; the task goes on |
| `task_timeout_s` | 600 | The whole task is stopped like Stop, and ends **failed** with `task_time_limit` |
| `first_token_timeout_s`, `idle_timeout_s`, `total_timeout_s` | 180, 60, 600 | Each model step (from M1). A hung model ends the task **failed** with `timeout` |
| (per step) | 8 calls | From M3: more calls in one step are refused |

**Stop**, the task time limit, and the page going away all cancel the same
token (`model/base.py`, `CancelToken`):

- a model step: the connection to Ollama is closed at once;
- a search: it checks before each file, so it stops within one file read;
- any other running tool: the loop waits `STOP_GRACE_S` (0.5 s) for it, then
  abandons it (the action is **cancelled**);
- calls the model proposed but that have not run yet: recorded **cancelled**,
  never run;
- no new step starts.

**Documented bound: from Stop to the end of the reply, at most 1 second.** It
is tested in `tests/test_m4_tasks.py` for a streaming step, a tool that
ignores Stop, and a search, and in the app by `validate-m4.sh`.

A Python thread cannot be killed. An abandoned tool keeps running until it
returns on its own. Its result never reaches the model, the gate still writes
its receipt, and the task gets an `action.late_result` event. The tools M4 has
are bounded anyway: reads are at most 256 KB, and a search stops at 5 s.

## Privacy of task records

Task state follows the M0 rules (`docs/receipts.md`):

- **Stored:** ids, statuses, times, tool names, workspace-relative paths,
  decision codes and reasons, counts, line ranges, hashes, limits, the report.
- **Never stored:** your message, the reply, prompts, file contents, search
  words (a hash only, as in receipts).
- Every value goes through the M0 redactor (key- and token-shaped strings,
  sensitive key names), then is cut to 300 characters
  (`tasks/store.py`, `clean()`). This applies to events, checkpoints and
  artifacts alike.
- Logs get ids, counts and timings only (`task.end`).
- **Retention:** kept until you purge, the same as receipts, until the
  retention decision is made. `bin/umbrous purge tasks --all|--before DATE
  --yes` deletes tasks with their actions, events, checkpoints and artifacts
  (by foreign-key cascade), with `secure_delete` on so the freed pages are
  overwritten. Receipts are kept until `purge receipts`.
- **Backups:** `umbrous.db` is an ordinary file in your data folder. A backup
  (Time Machine, a copied folder) keeps whatever it held at that moment.
  Purging does not reach backups; delete or expire those yourself. Since task
  records hold no content, an old backup holds paths and statuses, not text.
- **Diagnostic capture:** none. There is no setting that records prompts or
  file contents. If one is ever added, it must be off by default and say what
  it captures and for how long.

The redaction test plants a fixture string in a file, the message and the
answer, plus a token-shaped file name, and checks that none of it reaches any
task table or the log.

## The pieces

| File | What |
|---|---|
| `umbrous/persistence/db.py` | schema 5: `tasks`, `task_actions`, `task_events`, `task_checkpoints`, `task_artifacts` |
| `umbrous/tasks/store.py` | `TaskStore`: transitions, events, checkpoints, artifacts, reads, purge |
| `umbrous/tasks/run.py` | `TaskRun`: records a running task; Observe + Verify; the report |
| `umbrous/runloop/chat.py` | the limits, the watchdog, Stop, `_run_gated` abandoning a tool |
| `umbrous/gate/gate.py` | `run(..., action_id, observer)`: shared ids, the "started" signal, `observed` |
| `umbrous/tools/fs.py` | `search(..., cancelled)` |

### API

```
GET /api/tasks?chat_id=chat_…   a chat's tasks with their reports (to redraw a chat)
GET /api/tasks/tsk_…            one task: task, actions, events, checkpoints, artifacts, report
```

Read-only, same credential as every other route. There is no route that
deletes tasks.

## The desktop app

Umbrous.app shows, under a reply:

- while a tool runs: `Step 2: reading notes/plan.md…`;
- at the end: the report in one line, for example
  `Task completed · 4 steps · 3 of 3 actions completed · 2 things not fully checked`.
  It is amber when something failed, a limit was reached, or something was not
  fully checked.

Stop works as in the page. Reopening a chat brings the line back
(`/api/tasks?chat_id=`). The full trace and the list of uncertainties are in
the browser page and `bin/umbrous tasks --task`.

Validation (`desktop/e2e/validate-m4.sh WORKDIR`, also part of
`validate-native.sh`) runs the e2e app against a fake Ollama:

1. a three-step read task, and its report line;
2. Stop while a step streams: the reply ends, the line says `Task stopped`,
   and Stop-to-idle is measured against the 1 s bound;
3. a model that never stops asking for tools reaches the step limit;
4. the reopened chat shows its report line again.

Then it checks the database from outside the app: statuses completed,
cancelled, completed, a `steps` limit event, and no message, answer or file
text in any task table or log.

## Demonstrate

```sh
python3 -m tests.fake_ollama --port 11434 &        # stand-in models, including multi-step:3b
bin/umbrous --home /tmp/u4 probe multi-step:3b
bin/umbrous --home /tmp/u4 workspace General "$(python3 desktop/e2e/stress.py seed-workspace /tmp/u4-ws)"
bin/umbrous --home /tmp/u4                          # pick multi-step:3b; ask anything
bin/umbrous --home /tmp/u4 tasks --task tsk_…       # each state transition, in order
```

1. **A multi-file question.** `multi-step:3b` lists, searches, then reads, and
   answers citing `notes/plan.md:2` (read) and `notes/team.md:4` (never read).
   The report says so.
2. **Each transition.** `tasks --task` prints the events: `task.requested`,
   `task.started`, then per step `step.started`, `action.requested`,
   `action.started`, `action.completed`, `checkpoint`, … `artifact`,
   `task.completed`.
3. **Stop a slow operation.** Pick `slow-model:7b` (no tools) or, in the app's
   validation, `slow-task:3b`, and press Stop while it streams.
4. **Force a limit.** Put `{"task_max_steps": 2}` in `/tmp/u4/config.json`,
   restart, ask again: the report shows `steps` in `limits_hit`.

## Exercise: write the two tests

The tests below exist; try writing them yourself first, then compare.

1. **Cancellation.** Patch `fs.read_file` to sleep 5 s and ignore Stop. Send a
   message whose model reads two files; press Stop (POST `/api/chat/cancel`)
   once the first read has started. Check the reply ends within 1 s, the first
   action is `cancelled`, and the second never ran
   (`test_stop_interrupts_a_running_tool_within_the_bound`).
2. **Log redaction.** Put a unique string in a workspace file, the message and
   the answer. After the task, dump every `task_*` row and the log and check
   the string is in none of them
   (`test_sensitive_fixture_strings_stay_out_of_task_state_and_logs`).

## Tests

| Test | Checks |
|---|---|
| `test_m4_tasks.TaskStoreTests` | schema 5; only valid transitions; ordered events; purge removes every table's rows |
| `test_a_search_hit_is_not_a_read`, `test_extensionless_citations_are_checked`, `test_digit_leading_names_are_checked_and_times_are_not` | only completed reads verify a citation; `README:1`, `Makefile:3` are checked; host:port and times are not citations; digit-first names like `001_init.sql:3` are |
| `test_multi_step_read_task_has_a_trace_and_a_report` | 4 steps, 3 actions, each requested → started → completed; checkpoints; evidence; Verify flags an unread file and an unread line; same ids as the receipts; the chat transcript unchanged |
| `test_step_limit…`, `test_total_tool_call_limit`, `test_task_time_limit` | each limit ends with a clear report |
| `test_hung_tool_is_abandoned_and_reported_unknown` | a stuck tool becomes unknown, the task goes on, its late result is logged but never used |
| `test_hung_model_ends_with_a_report` | a model that stalls mid-answer ends failed (`timeout`) with a report |
| `test_stop_during_streaming…`, `test_stop_interrupts_a_running_tool…`, `test_stop_during_a_search…` | Stop ends the reply within 1 s, and no model call or tool runs after it |
| `test_sensitive_fixture_strings_stay_out_of_task_state_and_logs` | redaction and no-content, over the whole database dump and the log |
| `test_cli_tasks_and_purge` | `tasks`, `tasks --task`, `purge tasks` needs `--yes`, receipts survive |
| `test_browser_m4` | the report under a reply, Trace in Details, the report after reopening |
| `test_validate_scripts` (M4 cases) | `validate-m4.sh` fails on any app failure, wrong records, a leak, or a slow Stop |
| `api.rs` `report_line_sums_up_a_task` | the app's one-line report |

## Not in M4

- Resuming a task after a restart, or marking one `unknown` because the
  runtime died mid-task: done in M5 (docs/m5-resume.md), from the action
  rows, events and receipts.
- Tools that write, run or reach the network.
- A trace view in the native app (it is in the page and the CLI).
- A retention period: kept until purge, pending the decision.
