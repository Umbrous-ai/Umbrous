# Receipt rules (format version 1)

Status: **defined in M0, written since M3** (one per tool attempt, in the
`receipts` table; see [m3-gate.md](m3-gate.md) for storage, retention and the CLI). Code:
`umbrous/receipts/model.py` (format) and `umbrous/receipts/redaction.py`
(redaction). Tests: `tests/test_receipts.py`.

## What a receipt is for

A receipt is the record that Umbrous attempted an action: what it tried, what
it was allowed to touch, what was decided, and when. It is written for every
allowed, denied, failed, or uncertain attempt. It is evidence for you (and for
recovery in M4/M5), not memory: receipts live in their own store and are never
fed back to the model as preferences or facts.

## Identifiers

| Id | Prefix | Meaning |
|---|---|---|
| Task id | `tsk_` | One user request and everything done for it |
| Action id | `act_` | One attempted tool call or operation inside a task |
| Receipt id | `rcp_` | One receipt (one per action attempt) |

Format: prefix, 12 hex digits of Unix milliseconds, 16 random hex digits, e.g.
`act_019a5b3c2d1e8f0a1b2c3d4e5f60`. Sorting ids as text sorts by creation
time.

## Fields

| Field | Type | Rule |
|---|---|---|
| `format_version` | int | Always `1` for this spec |
| `receipt_id`, `task_id`, `action_id` | id | As above |
| `action` | text ≤ 64 | Operation name like `fs.read`, `fs.search` (lowercase, dots, underscores) |
| `scope` | text ≤ 256, one line | What the action was allowed to touch, e.g. `project:demo resource:docs/a.md` |
| `decision` | enum | `allowed`, `denied`, `failed`, `uncertain` |
| `reason` | text ≤ 280, one line, optional | Short reason for the decision. Not a place for content |
| `started_at`, `ended_at` | UTC timestamp | Millisecond ISO 8601 with `Z` |
| `duration_ms` | int | Derived |
| `evidence` | ≤ 50 references | See below |

An **evidence reference** points at evidence without containing it:
`kind` (`file`, `directory`, `search`, `url`, `tool_result`, `other`),
`ref` (≤ 512 characters, e.g. a workspace-relative path), and optionally
`sha256` of the content at the time, `size_bytes`, and `lines` (first, last).

## Never in a receipt

- File contents, search result text, or tool output
- Prompts, model replies, or chat messages
- Secrets of any kind, including the session credential

The length limits are deliberately small so that pasting content into a
receipt by mistake fails loudly with a `ReceiptError` instead of being stored.

## Redaction (applied before anything is persisted)

`Receipt.to_record()` is the only form a receipt may be written in, and it
runs the shared `Redactor` over every field. The same redactor runs over every
log line. Rules, in order:

1. **Runtime secrets by exact value.** The session credential and launch code
   are registered at startup and replaced with `[REDACTED:runtime_secret]`.
2. **Known secret shapes.** Private key blocks, Anthropic/OpenAI/GitHub/AWS/
   Slack keys and tokens, JWTs, `Bearer ...` values, and `user:pass@` in URLs
   become `[REDACTED:<kind>]`.
3. **Assignments.** `password=...`, `API_KEY: "..."`, `*_token=...` keep the
   name and lose the value, whatever its length. A quoted value is removed up
   to its closing quote, spaces included (or to the end of the line if the
   quote is never closed). The only exception is a fixed list of names that
   hold token counts (`max_tokens`, `token_count`, `prompt_tokens`, ...; see
   `COUNT_NAMES`), and only when the value is a whole number. Plural credential
   names such as `access_tokens` are still redacted.
4. **Sensitive field names.** In structured data, any value under a key such
   as `password`, `token`, `authorization`, `cookie`, or ending in `_token`,
   `_secret`, `_password` becomes `[REDACTED:field]`, whatever it contains.

Limits: redaction only catches what it recognises. A secret with no known
shape that the runtime was not told about will pass through. That is why the
format stores references rather than content in the first place.

## Purge

Manual purge is a command, never automatic deletion behind your back:

- Now: `bin/umbrous purge logs` lists the log files and deletes them only with
  `--yes`.
- M3: `bin/umbrous purge receipts` (with `--before <date>` or `--all`, also
  requiring `--yes`) is added alongside the receipts table, using the same
  pattern.

## Retention: OPEN DECISION

How long receipts are kept is yours to decide (see the plan's open decisions).
Until it is decided, M3 keeps receipts only for the test workspace, and nothing
beyond that is retained.
