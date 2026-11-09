# Token use — 8 November 2026

Goal: send the model as little as possible while it can still do the work.
Every model step resends the system prompt, the tool definitions and the whole
task so far, so anything repeated per step or per tool result is multiplied.

## What changed

| Where | Before | After |
| --- | --- | --- |
| Progress record (coding) | First message, ahead of the system prompt, rebuilt every step: nothing in the request could be reused from a cache | Last message: the rest of the request is the same as the previous step's, so a server with prompt caching (Ollama's KV cache, OpenAI's prompt cache) only processes what is new |
| Progress record content | File hashes, full commands, every action repeated, indented JSON | Path and kind of change, commands clipped to 120 characters, only actions not already listed (failures kept), compact JSON |
| Repeated calls | An identical read, listing, search or command kept every earlier copy | The older copy becomes a one-line note; the newest stays. Changes, failed or refused results and commands still running in the background are never replaced; running command ids are also kept in the progress record (`coding_context.supersede`) |
| Results later steps made redundant | Kept in full for the whole task | Outside the newest two rounds, a listing, search or name find and a read of a file edited since keep only their header line (`coding_context.compact_old`). A read the model may still edit from stays whole, and command output stays until the same command runs again |
| When older results are rewritten | — | Only when it saves at least half of what it makes the server reprocess (counted with tool-call arguments and the progress record), so the cached prefix stays stable on most steps (`coding_context.trim`); always when the task is over its budget |
| Memory suggestions, workspace inventory | Long suggestion prompt; every candidate folder sent with its name and a false flag | Same rules in fewer words; only the path, and the Umbrous-source flag when true |
| Tool definitions | Long descriptions per tool and parameter, `expected_sha256` offered though the default is always right | Short descriptions; shared rules stated once in the system prompt; `expected_sha256` still accepted, no longer offered |
| Untrusted-data wrapper | Two-sentence preamble on every result | One short line; the markers and the rule in the system prompt are unchanged |
| Tool results | SHA-256 in every read and edit result; padded listing columns; sizes on every `find_files` match; empty stdout/stderr sections | No hashes (a change defaults to the version this task read); `name/` and `name (N B)`; names only for `find_files`; empty streams omitted |
| System prompts | Default prompt said "You cannot read files or use tools yet", contradicting the tool prompts; long coding prompt | Stale sentence removed; tool and coding prompts keep every rule in fewer words |

Not changed: what the gate allows, limits, read sizes, history and memory
selection, and the workspace-resolution instructions and the Codex envelope (both
were being changed in other work at the time; see "Next").

## Measurement

`python3 -m tests.measure_tokens --workspace DIR` runs the real runtime with
scripted models (tests/fake_ollama.py) on four tasks and counts every request
Umbrous sends, with tiktoken's o200k_base when installed (else Umbrous's
4-characters-a-token estimate). Both versions used the same workspace copy.

| Task | Requests | Input before | Input after | Saved | Reprocessed before | after |
| --- | --- | --- | --- | --- | --- | --- |
| Chat without a folder, 3 messages | 3 | 2,077 | 1,762 | 15.2 % | 786 | 681 |
| Read-only question (list, search, 2 reads) | 6 | 9,348 | 8,495 | 9.1 % | 4,305 | 4,028 |
| Coding: add a function, fix, tests pass | 8 | 23,800 | 12,128 | 49.0 % | 17,431 | 3,898 |
| Coding, longer (re-reads, re-runs) | 10 | 49,249 | 31,637 | 35.8 % | 40,350 | 11,441 |
| **All four** | 27 | **84,474** | **54,022** | **36.0 %** | **62,872** | **20,048 (−68.1 %)** |

"Reprocessed" is the part of each request that differs from the start of the
previous one: what a server with prompt caching has to read again.

Limits: the model's choices are scripted, so a real model may take more or
fewer steps, and may read a file again where an older result was shortened
(that cost is not in these numbers); the tokenizer is OpenAI's, and local models (Qwen and others)
count somewhat differently; reply tokens are not included (the model writes
those). Whether a provider bills cached tokens less depends on the provider.

## Next

- Workspace resolution sends its instructions, the history and an inventory of
  up to 40 folders on every message of a chat without a folder. Its instructions
  were being rewritten in other work, so they were left as they are.
- The ChatGPT provider still JSON-encodes the transcript (every newline and quote
  in file contents is escaped), and each new Codex conversation carries about
  6,500 tokens of Codex's own overhead. The memory-suggestion call after a task
  opens one more conversation and is not cached.

## ChatGPT provider: one conversation per task

Before, every model step started a new, throwaway Codex conversation with the
whole transcript and all system text (including the per-step progress record)
in its instructions, so nothing could be served from OpenAI's prompt cache.
Now a task keeps one conversation (`codex._Session`): the leading system text is
its instructions, later system notes travel in the transcript, and each further
step sends only `transcript_continues` (the messages after the model's previous
answer) and the tools only when they change. A step whose request does not
exactly continue the conversation (an earlier message was rewritten, the model's
answer differs, a failure, Stop or time limit) starts a new conversation from the
full transcript, as before. A conversation belongs to the task that started it (its
stop token), so no other task or chat can continue it whatever its content. Idle
conversations close after 120 s; at most 4 wait; shutdown closes every one.

Real runs, same task (add multiply to calc.py and make the tests pass), model
gpt-6-astra at medium effort through the same ChatGPT sign-in; token counts are
Codex's own usage reports. All runs passed the tests.

| | Runs | Input tokens | From cache | Not cached |
| --- | --- | --- | --- | --- |
| Codex CLI | 2 | 76,400 | 70,000 (92 %) | 6,400 |
| Umbrous before (main) | 3 | 39,990–41,730 | 0–3,600 | 38,100–41,730 |
| Umbrous now | 3 | 40,828 | 15,900–28,000 (39–69 %) | 12,800–25,000 |

Counting cached input at a tenth of the price, a task now costs Umbrous about
20,000 token-equivalents (was about 40,000) against Codex's about 13,400.

## Claude provider (new): Claude Code with its own tools off

`claude/` models run through the official Claude Code CLI with your claude.ai
subscription (umbrous/model/claude.py; never an API key). Claude Code is started
headless with no built-in tools, MCP servers, skills, plugins, slash commands or
settings files, with Umbrous's instructions as its whole system prompt and a JSON
schema for its answer, so it can only propose calls for Umbrous's gate. Its
initialize handshake must show a subscription sign-in, dontAsk and no commands
before anything is sent; its start-up report must list only the answer tool, no
MCP server and the exact model; a direct tool call by the model must come back
refused. One process holds a task's conversation (umbrous/model/sessions.py,
the same rules as the ChatGPT provider), so later steps are served from cache.

Same task, Claude Sonnet 5.5, same subscription; Claude Code's own usage reports
(2 runs each, all tests passed). "Weighted" counts cache reads at 0.1x and cache
writes at 2x (1-hour cache), as Anthropic prices them.

| | Requests | Input tokens | From cache | Weighted |
| --- | --- | --- | --- | --- |
| Claude Code (default setup) | 6–7 | 168,750–212,911 | 89–91 % | 50,600–55,700 |
| Umbrous | 5 | 23,493–23,528 | 67–74 % | 13,700–17,000 |

## Grok provider (new): Grok Build with its own tools off

`grok/` models run through the official Grok Build CLI with your grok.com
subscription (umbrous/model/grok.py; never XAI_API_KEY). Grok Build ignores an
empty `--tools` list, so every known tool is denied by name and the tool list it
reports must be empty, else the step stops (this caught a quirk in testing: with
GROK_WORKFLOWS=0 an `update_goal` tool appeared). Any tool event stops the step.

Resuming a Grok session would let later steps hit xAI's prompt cache, but in
Grok Build 1.0.70 a resumed session ran the server-side X search although it was
denied: Umbrous stopped the step, but the search had started on xAI's side. So
every step starts a new session with the whole transcript (`resume=False`), and
that costs the cache.

Same task, grok-4.7, same subscription; Grok Build's own usage and cost reports
(2 runs each, all tests passed):

| | Requests | Input tokens | From cache | xAI-reported cost |
| --- | --- | --- | --- | --- |
| Grok Build | 5 turns | 90,966–91,056 | 82–90 % | $0.062–0.073 |
| Umbrous (new session per step) | 6 | 49,658–49,689 | 14 % | $0.098–0.099 |

Umbrous sends 45 % fewer tokens but, without the cache, costs about 45 % more.
About 5,000 tokens of every new Grok session are Grok Build's own prompt.
