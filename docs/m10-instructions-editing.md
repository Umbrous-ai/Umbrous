# M10: Project instructions and better editing

Two changes that make a coding task do the right thing sooner, whichever model
runs it. Builds on M9 (`docs/m9-coding.md`) and the token work (`docs/tokens.md`).

## Project instructions (AGENTS.md, CLAUDE.md)

Many repositories tell coding agents how to work in them: AGENTS.md (read by
Codex and others) or CLAUDE.md (read by Claude Code). When a task has a
workspace and its model can use tools, Umbrous reads these two files from the
workspace root and adds them to the system message
(`umbrous/context/instructions.py`).

| Rule | Why |
| --- | --- |
| AGENTS.md first, then CLAUDE.md; a file identical to one already read is skipped | One copy of the conventions, not two |
| Read through the same descriptor walk as `read_file`: no symlinks, no hard links, UTF-8 text only | A file in the folder cannot make Umbrous read something outside it |
| Read only when the gate would allow `read_file` on it without asking | A project on **Ask before read** gets nothing loaded behind its back; the start event says `needs_approval` |
| At most 12,000 characters per file, 16,000 in all, and never more than a quarter of the input budget; a cut file says so | Small models keep room for the task |
| Labelled as the project's conventions, written into the folder, not by you: they cannot widen access, skip approvals or override the rules | The gate still decides every action |
| In the system message, which is the same at every step | Part of the cached prefix, so it is paid in full once per task |

The `start` event's `tools.instructions` lists the files used, any that were
cut, and any that were skipped with the reason.

Not in M10: AGENTS.md files in subfolders, `@file` imports, and a user-level
instructions file.

## Better editing

### Several replacements in one call

`edit_file` now takes either `old_text` + `new_text` (as before), the same with
`replace_all: true` (every occurrence, for a rename), or `edits`: a list of
`{old_text, new_text}` applied in order, each to the result of the one before.

* **All or none.** If any replacement does not match exactly once, nothing is
  written and the message says which one (`Edit 2 of 3: ...`).
* **Repeats are safe.** A call identical to this task's last change of a file,
  while the file is still exactly as that change left it (same hash), is
  reported as already applied and nothing is written; this covers changes whose
  new text contains the old one (`x` → `xx` with `replace_all`), which a text
  match alone would apply again. Otherwise a replacement whose old text is gone
  and whose new text is there counts as already made. Across a crash, M5/M9
  recovery is unchanged: it checks hashes on disk and tells the resumed task
  what was done.
* One call is one step, one approval and one receipt, instead of one per
  change. `edits` may also arrive as a JSON string (some models send nested
  arrays that way). At most 50 replacements per call.

### A parse check after every write

After `write_file` or `edit_file` changes a file, its new text is parsed in
process when its kind is one Python can parse alone: Python (`.py`, `.pyi`),
JSON and TOML (`umbrous/tools/check.py`). Nothing is run and no process is
started, so this needs no permission beyond the write already allowed.

* The change is still written (it was allowed and may be one step of several).
  The result tells the model the file does not parse, with the line, and the
  step summary says `does not parse`.
* The task's progress record lists `files_not_parsing` until a later change
  to that file parses, and tells the model to fix those first.
* Python is parsed by the Python Umbrous runs on; the message names its version.
* A repeated change (already applied) is checked again, so a broken file stays
  listed until a change to it parses.
* "Not checked" is never "passed": TOML is checked only where Python has
  `tomllib` (3.11+); input too deeply nested for the parser, or over 512 KB, is
  not checked (`check: null`), and a checker failure never fails the write.

Not in M10: running the project's own linter or type checker automatically.
That runs project code, so it goes through `run_command` and the gate like any
command; the project's AGENTS.md is the place to say which check to run.

## Tests

`python3 -m unittest tests.test_m10_instructions_editing` (22 tests): which
instruction files are read and when not, multi-edit all-or-none and repeats,
replace_all, argument checks, the parse check, and a running runtime where the
instructions reach the model and a broken edit reaches the progress record.
