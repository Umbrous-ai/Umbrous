# M9: Coding in a project folder

Until M8, Umbrous could only look at a workspace. M9 lets a task **change** it:
create and edit files, run the project's commands, read what failed, fix it
and run them again, until the work is done or it hits a concrete blocker. If
Umbrous stops in the middle (a crash, a forced quit, a restart), the task can
be resumed: changes that were made are kept and not made twice, and a command
whose effects are unknown is never just run again. Builds on M5's recovery
(`m5-resume`) and M8 (`m8-reset`).

```
"work on ~/code/app: add X and make the tests pass"
      │
      ▼  the folder is resolved and attached (Code in workspace), or the attached one is used
 inspect ── list_files · find_files · search_files · read_file   (each read remembers the file's hash)
      │
 change ─── edit_file / write_file:  intent recorded ─▶ checked against the hash it read ─▶ temp file ─▶ rename
      │
 check ──── run_command (tests, build) ─▶ stdout, stderr, exit status;  check_command for long ones
      │
      └── failed? read the output, change again, run again ...  until done, a blocker, a limit or Stop
      ▼
 report: what changed (files, +/− lines), what ran (exit status), what is unresolved
```

## Run it

```sh
bin/umbrous                                    # then, in a chat: work on /path/to/project: <what to do>
bin/umbrous workspace General ~/code/app --profile workspace_code      # or attach it yourself
bin/umbrous workspace General --profile code_ask_commands               # ask before each command
bin/umbrous tasks --task tsk_…                 # the trace and report: changes, commands, exit statuses
bin/umbrous test                               # 373 tests; no Ollama needed
python3 -m tests.prove_m9                      # the regression tests fail with each M9 behaviour removed
desktop/e2e/validate-m9.sh /tmp/m9             # the native app (after desktop/scripts/build-native.sh --e2e)
```

A model needs tool calling (the M3 tool check) to code. Pick one in the model
menu as before.

## Words used here

| Word | Means |
|---|---|
| **workspace** | the one folder a project's tasks may work in (M3). Every file tool path is relative to it |
| **Code profiles** | `workspace_code` (*Code in workspace*: read, edit and run commands without asking) and `code_ask_commands` (*Code, ask before commands*) |
| **change** | one `write_file` or `edit_file` call. It has a hash *before* (or none: a new file) and a hash *after* |
| **intent** | the task's record of a change or command, written **before** it happens (`write.intent`, `command.intent`) |
| **conflict** | the file is not the version the change was based on: someone or something else changed it. Nothing is written |
| **unknown** | M5's word: Umbrous stopped while an action ran, and its outcome was not recorded |

## Choosing the folder

Two ways, both shown at the top of the reply (*Working in /path*):

1. **The attached workspace.** Attach a folder in Details › Access (browser
   page) or with `bin/umbrous workspace PROJECT FOLDER --profile …`. A task in
   that project works there.
2. **Name it in your message.** "work on /path/to/project", "working in
   ~/code/x", "work on the repo \`/My Projects/app\`" (quote a path with spaces).
   Only your own message is read for this, never a file or the model's text.
   Umbrous resolves the path (`~`, symlinks) and checks it like any workspace:
   it must exist, be a folder, and not be your home folder, `/`, or inside
   Umbrous's own data folder. Then:
   - if the project has no workspace, or a different one, the folder is
     attached with **Code in workspace**, and the reply says so ("attached
     now");
   - if that folder is already the workspace, it keeps the profile you chose
     for it (a read-only folder stays read-only: the task can only read);
   - if the folder is refused, the reply is an error and the model is not
     called.

You chose this rule (option A when M9 started): naming a folder means "code
here", so it gets the profile that can edit and run commands without asking.

Every file operation stays inside the folder. The task records the folder
(`tasks.workspace`, schema 9) and a resume only runs in the same folder (below).

## The tools

| Tool | Does | Gate action | Retry class |
|---|---|---|---|
| `list_files` | one folder's entries (M3) | `fs.list` | repeat |
| `read_file` | a file or a range of lines, numbered, at most 400 lines and 256 KB; the result names the file's sha256 | `fs.read` | repeat |
| `search_files` | lines containing words, with path:line (M3) | `fs.search` | repeat |
| `find_files` | files and folders by name: a glob (`*.py`, `src/**/test_*`) or a part of the name; at most 200 | `fs.find` | repeat |
| `write_file` | create a file (missing folders are made), or replace a whole file (`expected_sha256`, or the hash this task last read) | `fs.write` | verify |
| `edit_file` | replace one exact occurrence of `old_text` with `new_text` | `fs.edit` | verify |
| `run_command` | `/bin/sh -c COMMAND` in the workspace folder: stdout, stderr, exit status, time; `timeout_s`; `background` | `cmd.run` | command |
| `check_command` | a background command: its output so far, wait up to `wait_s`, or `stop` | `cmd.check` | repeat |

Only the tools a profile can use are offered to the model: the read profiles
are offered the four reading tools and nothing else.

Every call goes through the gate as in M3: it is validated (types, sizes, no
NUL characters, ids), its path is confined (no `..`, absolute paths, links,
hard links, excluded names such as `.env`, `.git`, keys), the profile decides
allow, ask or block, and **every attempt gets one receipt**. A change's receipt
holds the path, the hash after and the hash before; a command's holds the
command (one line, redacted, at most 200 characters) and a hash and size of
its output. Neither holds file contents or output.

### How a change is made

`umbrous/tools/edit.py`, for both write tools:

1. The folder and file are opened through descriptors, without following
   links (as M3 reads do), and the current file is hashed.
2. **The expected version.** An existing file must be the version the task
   last read or wrote (or `expected_sha256`). If it is not, the change is
   refused as a **conflict** and nothing is written: the model is told to read
   it again. The version is remembered under the path as the disk spells it
   (`canonical_path`), not as the model typed it: on a case-insensitive disk
   (the macOS default) `calc.py` and `CALC.py` are one file, as are the two
   Unicode forms of an accented name, and an edit through either spelling is
   checked against the version the task saw. `write_file` without any expectation only creates: it never
   overwrites a file that exists (`exists`). `edit_file` without a read still
   works, because its exact `old_text` is the check.
3. The new content is computed. `edit_file` needs `old_text` exactly once:
   none is `no_match`, more than one is `ambiguous`. If `old_text` is gone and
   `new_text` is there exactly once, the edit was **already made** (by an
   interrupted attempt, say): nothing is written and the result says so.
4. **The intent** is recorded in the task (`write.intent`: path, hash before,
   hash after, the temporary name). If it cannot be recorded, nothing is
   written.
5. The content goes to a temporary file next to the target (`.umbrous-tmp-…`,
   the old file's permissions, fsync). A new file is linked into place, which
   refuses to replace anything that appeared meanwhile. An existing file is
   hashed once more and only then renamed over (the rename is atomic: the file
   is the old or the new version, never half). The folder is fsynced.
6. The result: the operation, `+added −removed` lines, a unified diff (shown
   under the step; at most 4,000 characters), and the new hash.

Text files only (UTF-8, at most 256 KB written).

### Commands

`umbrous/tools/run.py`:

- **Where and as whom.** In the workspace folder, through `/bin/sh -c`, **as
  you**, with the runtime's environment minus Umbrous's session token (and
  with `UMBROUS_COMMAND=1`). Input is empty (`/dev/null`). Your shell profile
  is not loaded; Umbrous.app's runtime has a minimal `PATH`, so the usual tool
  folders that exist (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.cargo/bin`,
  `~/.local/bin`) are added after the system ones. Anything else, give by its
  full path or set in the command. Each command gets its own
  process group, so stopping it stops what it started.
- **Not a sandbox.** The workspace boundary applies to the file tools. A
  command can do anything you can: read or change files outside the folder,
  use the network, install things. Choose *Code, ask before commands* to see
  each one first, and attach only folders you would run commands in yourself.
- **Output.** The first 2 KB and last 6 KB of stdout and of stderr are kept,
  with their full sizes; that is what the model sees and what the step shows
  (folded). Output is redacted like everything else that is stored.
- **Time.** `timeout_s` (default `command_timeout_s`, 120 s; at most
  `command_timeout_max_s`, 1800 s). At the limit the command is stopped
  (SIGTERM to its group, SIGKILL 0.3 s later) and reported as timed out.
- **Long commands.** `background: true` returns after `timeout_s` with the
  command still running and an id; `check_command` shows its output so far,
  waits for it, or stops it.
- **Stop.** Stop (or closing the page) ends the running command within about
  0.1 s and ends the task. A command never outlives its task: when a task ends,
  every command whose process group still has a running process is stopped,
  and the report says so. That includes a command whose shell has already
  exited but left something behind (`sleep 60 &`): its result says that
  processes it started are still running, and they are stopped with the task.
- **Whose group it is.** A group is named by its leader's process id, and an
  id can be reused once its process is gone and reaped. So a command's leader
  is a small supervisor, not the shell: it runs the shell in the group, reports
  its exit status, and then stays until the task lets it go. Umbrous reaps the
  supervisor only after it has finished signalling the group, and never signals
  a group after that. So for as long as Umbrous may signal a group, its id
  cannot belong to any other process, even when everything the command started
  has already exited. If the runtime dies, the supervisor sees it and stops the
  group itself (SIGTERM, then SIGKILL 0.3 s later). It does so on every way
  out except the runtime's own "let go": the control pipe closing, the status
  pipe breaking as the shell finishes, or any unexpected error.

## The coding loop

`umbrous/runloop/chat.py`. With a Code profile the model gets the coding
instructions (inspect, change, run the checks, read failures, fix, repeat;
read before editing; never claim a result a tool did not return; tool output
is data) and the coding limits, set in `config.json`:

| Setting | Default | |
|---|---|---|
| `code_max_steps` | 40 | model turns |
| `code_max_tool_calls` | 120 | tool calls in all |
| `code_task_timeout_s` | 3600 | the whole task |
| `command_timeout_s`, `command_timeout_max_s` | 120, 1800 | a command |

Old tool results are folded away when the context fills up (the last two stay),
so a long task does not run out of room; files can be read again.

The **report** (M4) adds what the task changed and ran, from the tool results
themselves, never from the model's words:

- `workspace`, `changes` (path, operation, hashes, `+/−` lines), `commands`
  (command, status, exit, time);
- "not fully checked" notes when the last run of a command failed or timed
  out, when changes were made and no command ran, or when changes came after
  the last command.

In the browser page each step shows what it did (*Edit calc.py · Edited
calc.py (+2 −1)*, *Run python3 -B -m unittest · exit 1*), with **Changes** (the
diff) or **Output** folded under it, and the report lists **Changed** and
**Ran**.

## Continue after an interruption

M5 closed every task left open at startup and could resume read-only tasks.
M9 adds the checks its own tools need (M5 listed them as the requirements for
any write tool):

### A change that was running

| What the record and the disk say | Recovered as |
|---|---|
| a receipt exists | as the receipt says (M5) |
| no `write.intent` | **cancelled**: the intent comes before any write, so nothing was written |
| the file has the *after* hash | **completed** (`reconciled_from: disk`): the change happened, it is kept and listed in the report |
| the file has the *before* hash (or is still absent, for a create) | **cancelled**: it did not happen |
| anything else | **unknown, conflict**: something else changed the file. It is left alone and shown to you |

A temporary file left by the interrupted write is removed.

### A command that was running

Its effects cannot be checked afterwards: it may have changed files before it
was cut off. So it stays **unknown**, is never run again by itself, and the
report and the Resume notice say so. If its process group is still alive
(the runtime died, the command's supervisor did not get to stop it), it is
stopped and that is recorded (`command.stopped_after_restart`) — but only when the process is shown to be
the very one that was started. A process id can be reused after a restart or
a reboot, so `command.started` records the leader's identity: the boot it ran
in (`/proc/sys/kernel/random/boot_id`, or macOS's `kern.boottime`) and its
start time (`/proc/<pid>/stat`, or `ps -o lstart`). Recovery signals the group
only when the process with that id is still a group leader in the same boot
with the same start time. Otherwise (another process, another boot, or no
identity recorded) nothing is signalled, `command.not_signalled` is recorded,
and the report says the process was left alone for you to check. Usually the
supervisor has already stopped the group when the runtime went away; then the
process is gone and `command.gone_after_restart` is recorded.

### Resume

Resume (the interrupted notice in the browser page and in Umbrous.app) runs
the task again as a new task, under the **current** settings, in the **same
folder**:

- **Permissions are checked again first**, for every kind of action the
  interrupted attempt took: if the project now has another folder, no folder,
  or a profile that no longer allows those changes or commands, the resume is
  refused (`resume_scope_changed`) and nothing runs.
- A change whose state could not be checked (no folder to check) blocks it
  (`needs_verification`). A conflict and an unknown command do not: they are
  shown to you, and the model is told.
- The model is given a note, built from the record and the disk (never from
  the old model text): the changes already made and whether each file is
  still as written, unclear changes, commands that finished, and commands
  that were running ("do not just run them again").
- It continues from the files as they are. An edit it repeats anyway is
  recognised as already made and not applied twice.

What resuming shows before you press it: *Already changed (kept, not made
again): calc.py. A command was running and may have changed files; it is not
re-run by itself.*

## Working on itself

Umbrous can be pointed at its own source like any folder ("work on
~/workspace/github.com/freddyrosa16/Umbrous"). It recognises the folder
(`umbrous/__init__.py`, `bin/umbrous`, `desktop/`) and then:

- tells the model that editing these files **does not change the running
  app** (the runtime has loaded its code; Umbrous.app runs its own bundled
  copy), that changes take effect only after a rebuild
  (`desktop/scripts/build-native.sh`) and a restart, and never to say the
  running app was updated;
- puts the same note in the report and at the top of the reply.

The task can run the tests (`bin/umbrous test`) and the build script like any
command. Installing the new build and restarting are yours to do.

## The desktop app

Umbrous.app uses the same runtime, so all of the above applies. In the app:

- the reply's line names the running step (*Step 3: editing calc.py…*,
  *Step 4: running python3 -B -m unittest…*) and ends with the report, now with
  *2 changes · 2 commands (1 not passing)*;
- "work on <folder>" shows a notice naming the folder and the profile it was
  attached with (and the own-source note);
- the interrupted notice lists what was already changed and any command that
  was running;
- the composer's access pill chooses this chat's access, for example *Full
  access* (Code in workspace, after a confirmation dialog) or *Workspace write*
  (Code, ask before commands); under the latter each command waits on a card above
  the composer (Approve once, Allow commands in this project, Deny);
  `validate-m9.sh` checks both, with the receipts, in a second scenario
  (`m9-approve.txt`) that also runs the tool check from the app.

`desktop/e2e/validate-m9.sh WORKDIR` drives the real app with a fake model on
two throwaway projects: "work on" attaches a folder, the task edits `calc.py`,
the tests fail, it fixes the bug and the tests pass; Stop while a 30-second
command runs; then the runtime is killed while the tests run, restarted, and
the task resumed. Then from outside: each project has exactly one fixed
`multiply`, no temporary file, no test command still running, and the records
show the kept edit, the unknown command, its stopped process and a resume
that made only the fix.

## The API

`POST /api/chat` is unchanged. Its stream adds:

- `start.tools`: `root`, `coding`, `tools` (offered), `selected` (`root`,
  `attached`, `previous`) when your message named a folder, `self_source`;
- `tool_result.observed`: for a change `op`, `path`, `before`, `after`,
  `added`, `removed`; for a command `command`, `status`, `exit`, `ms`,
  `cmd_id`; and `tool_result.detail`: the diff or the output (live only, never
  stored);
- the report's `workspace`, `changes`, `commands`.

`GET /api/tasks/<id>` `resume` adds `done`, `conflicts`, `commands_unknown`
and `workspace` for a coding task.

## The pieces

| File | What |
|---|---|
| `umbrous/tools/edit.py` | `find`, `change` (expected version, intent, temp file, link or rename), `edit_compute`, `current_hash`, `remove_tmp` |
| `umbrous/tools/run.py` | `CommandRegistry` (start, wait, stop, check, end a task's commands), output capture, `process_alive` |
| `umbrous/gate/gate.py` | the five new tools, their validation, `ToolContext` (the hashes the task has seen, the intent recorder, the commands), `offered` |
| `umbrous/gate/access.py` | the two Code profiles |
| `umbrous/runloop/chat.py` | named folders, the coding prompt and limits, the resume note, ending commands with the task |
| `umbrous/tasks/run.py` | intents, changes and commands in the report, the "not fully checked" notes |
| `umbrous/tasks/recover.py` | checking interrupted changes on disk, stopping left-over commands, `resume_brief` |
| `umbrous/selfsource.py` | recognising Umbrous's own source |
| `umbrous/persistence/db.py` | schema 9: `tasks.workspace`, the Code profiles |
| `umbrous/web/chat.js` | the folder line, step labels, diffs and output, the report, approval cards for changes and commands |
| `desktop/app/src/api.rs`, `workspace.rs` | step labels, the folder notice, the report line, the resume note |

## Demonstrate

1. Make a small project with a failing test, or use the fixture:
   `python3 desktop/e2e/stress.py seed-coding-project /tmp/m9demo`.
2. Run the fake coder: `python3 desktop/e2e/stress.py fake-coding 11435`,
   and `bin/umbrous --home /tmp/m9home` with `{"ollama_url":
   "http://127.0.0.1:11435"}` in `/tmp/m9home/config.json`.
3. Check `coder:3b` in the model menu, pick it, and send *work on
   /tmp/m9demo/proj: add a multiply function to calc.py and make the tests
   pass*. Watch it find, read, edit, run the tests (exit 1), open the Output to
   see the failure, fix, run again (exit 0) and report.
4. Interrupt one: reset `calc.py` from the fixture, pick `slow-coder:3b`
   (its test command sleeps 30 s first), send the same message, and when it
   is *Running sleep 30; …*, kill the runtime (`kill -9`). Start it again: the
   chat shows the interrupted task with *Already changed: calc.py*. Pick
   `coder:3b` and Resume: it reads `calc.py`, sees `multiply` is there, runs
   the tests, fixes the bug and finishes. `calc.py` has one `multiply`.

With a real model, use a project under git so `git diff` shows you exactly
what changed.

## Tests

| Test | Checks |
|---|---|
| `test_m9_coding.ToolTests` (27) | create (and missing folders), create refuses to overwrite, targeted edit after a read, edit without a read, no match, ambiguous, already applied, whole-file replace and unchanged, a newer edit is a conflict, a conflict at the last moment, atomic write keeping the mode, no intent means no write, boundaries (outside, absolute, `.env`, `.git`, keys, symlinks, hard links), find; commands: success, failure, cwd and no session token, timeout, background check and stop, a finished background command, Stop under 2 s, ending the task stops its commands, ending the task stops what a finished command left running (`sleep 60 & echo $!`), a command that left nothing is forgotten, a group stays owned (its id taken) after everything in it exits until the task ends and is never signalled once released, the supervisor stops the group when the runtime goes away, and when the status pipe is broken as the shell finishes, bounded and redacted output, the added tool folders on PATH; a version remembered under one spelling is checked through another on a case-insensitive disk (skipped on a case-sensitive one) |
| `ProfileTests` (2) | what each of the four profiles allows, and which tools each offers |
| `LoopTests` (6) | a whole task: find, read, edit, test (exit 1), fix, test (exit 0), report; the notes when the last check failed or none ran; "work on" attaches with the Code profile and an attached folder keeps its profile; a refused folder never reaches the model; Stop during a command; Umbrous's own source |
| `NamedFolderTests` | which messages name a folder, including quoted paths with spaces |
| `RecoveryTests` (10) | a crash before the intent, after the intent and before the write (temp file removed), after the write, and with the file changed by someone else; an interrupted command (unknown, its process stopped); a reused process id (other start time, other boot, no identity) is not signalled; resume tells the model what was done and does not redo it; resume re-checks changes and commands under the current profile; another folder refuses it |
| `CrashTests` | the real runtime killed with SIGKILL while the tests run; restarted: the edit completed, the command unknown and its process stopped (by its supervisor or by recovery); resumed: no repeat of the first edit, the fix made, the tests pass, no temp file |
| `test_browser_m9` | in Chromium: "work on" attaches and shows the folder; step labels, diffs and command output; the report's Changed and Ran; under *Code, ask before commands* the command waits for approval (with the not-sandboxed note) and runs after Approve once |
| `test_validate_scripts` (M9 cases) | `validate-m9.sh` fails on any app failure and on a duplicated or unfixed edit, a temp file left, an edit made again, a command marked completed, a left-over process not stopped |
| `api.rs` `report_line_counts_changes_and_commands`, `step_labels_and_folder_notes`, `stop_reads_on_to_the_last_event` | the app's report line, step labels, folder notice and resume note; after Stop the app reads every event up to the last (`done` or `error`), through a batch of cancelled results and tool calls, so the report is shown |
| `tests/prove_m9.py` | 18 mutations (19 on a case-insensitive disk), each removing one behaviour above (the hash check, the last check before the rename, create-only, already applied, intent before the write, Stop reaching a command, dropping the session token, the disk check after a crash, stopping a left-over command, the resume note, the permission re-check for commands, the "no command checked this" note, stopping what a finished command left in its group, never signalling a released group, keeping a group owned until the task ends, the supervisor stopping the group when the runtime goes away, the same when the status pipe breaks (the reviewed version restored), the identity check before signalling after a restart, and on macOS remembering a version under the disk's own spelling); each makes its guarding tests fail |

## Not in M9

- **A sandbox for commands.** They run as you. The gate confines the file
  tools, not what a command does. A future milestone could run commands in a
  container or under macOS's sandbox; until then, choose the folder and the
  profile with that in mind.
- **Undo.** Changes are not rolled back by Umbrous; use git (or Time Machine).
  The report lists every file changed with its hashes.
- **A lock against other editors.** The hash checks catch a file changed
  between the read and the write, and again just before the rename, but an
  editor saving in the instant between that last check and the rename can
  still lose its save (the file is never half-written, but the newer save is
  replaced). Close the file elsewhere while a task edits it.
- **Binary files, and text over 256 KB.** Not written.
- **Commands across a restart.** A background command's id ends with the task
  or the runtime. A process a command moved into its own session or group (a
  daemon, `setsid`, `nohup … &` with its own group) is not stopped with it.
  If the runtime and the supervisor are both killed at once (SIGKILL to the
  whole process tree), what the command started is not stopped by the
  supervisor, and recovery leaves it alone: without its leader, it cannot be
  shown to be the command's.
- **Approval of commands in Umbrous.app.** Only the browser page can ask.
- **How well a real local model codes.** The loop, limits, checks and
  recovery are tested with a scripted model; a small local model may still
  make poor changes or give up. The report and the diffs are there so you can
  judge.

### Keeping progress through context compaction

Coding requests fit the actual serialized transcript, including tool arguments,
within the configured input budget. Older complete tool rounds are removed before
new results are shortened. A fresh runtime progress record carries recent confirmed
edits (including resulting hashes), command statuses/exit codes, action outcomes,
and counts across those removals. File contents and command output remain untrusted;
progress records label their JSON values as data, not instructions.

This avoids replacing successful edits with generic “result removed” messages,
then forcing the model to guess whether it changed anything. An edit is not proof
of a passing test, and a successful inspection command is not a test run. The
record explicitly identifies changes made since the last command and asks for
relevant verification. Detailed old file contents may still require a targeted
reread. The progress record is bounded to recent entries and is not full long-term
memory; extremely small configured windows can omit it rather than discard the
user's request or system policy.

Native action status now distinguishes a proposal from the gate's actual start,
which occurs after any required approval. Completed runs with unresolved checks
are labelled accordingly rather than simply “Task completed”. Execution lifecycle
status in the task database remains separate from proof that every user objective
was achieved.

Regression: `python3 -m unittest tests.test_coding_context` covers oversized edit
arguments, complete-round eviction, and an edit/read/verify sequence that preserves
its receipt and stays within the input budget. Optional real-model check:
`python3 -m tests.live_coding_context --live --model codex/MODEL`; it uses only a
disposable workspace and requires a successful edit and passing test command.

The self-editing failure that motivated this fix recorded four successful edits
but ended with a model answer saying their results were unavailable. Its task
history showed repeated inspection, output truncation, and no verified build/test
completion. The new opt-in Codex check completed in five steps, retained one
confirmed edit, and ran `python3 -m unittest test_value.py` successfully in a
4,096-token fixture context. This establishes that the edit/read/verify path works;
it does not establish completion of every broad product-improvement request.
