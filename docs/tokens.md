# Token use — 8 November 2026

Goal: send the model as little as possible while it can still do the work.
Every model step resends the system prompt, the tool definitions and the whole
task so far, so anything repeated per step or per tool result is multiplied.

## What changed

| Where | Before | After |
| --- | --- | --- |
| Progress record (coding) | First message, ahead of the system prompt, rebuilt every step: nothing in the request could be reused from a cache | Last message: the rest of the request is the same as the previous step's, so a server with prompt caching (Ollama's KV cache, OpenAI's prompt cache) only processes what is new |
| Progress record content | File hashes, full commands, every action repeated, indented JSON | Path and kind of change, commands clipped to 120 characters, only actions not already listed (failures kept), compact JSON |
| Repeated calls | An identical read, listing, search or command kept every earlier copy | The older copy becomes a one-line note; the newest stays. Changes and failed or refused results are never replaced (`coding_context.supersede`) |
| Tool definitions | Long descriptions per tool and parameter, `expected_sha256` offered though the default is always right | Short descriptions; shared rules stated once in the system prompt; `expected_sha256` still accepted, no longer offered |
| Untrusted-data wrapper | Two-sentence preamble on every result | One short line; the markers and the rule in the system prompt are unchanged |
| Tool results | SHA-256 in every read and edit result; padded listing columns; sizes on every `find_files` match; empty stdout/stderr sections | No hashes (a change defaults to the version this task read); `name/` and `name (N B)`; names only for `find_files`; empty streams omitted |
| System prompts | Default prompt said "You cannot read files or use tools yet", contradicting the tool prompts; long coding prompt | Stale sentence removed; tool and coding prompts keep every rule in fewer words |

Not changed: what the gate allows, limits, read sizes, history and memory
selection, and the workspace-resolution prompt and the Codex envelope (both
were being changed in other work at the time; see "Next").

## Measurement

`python3 -m tests.measure_tokens --workspace DIR` runs the real runtime with
scripted models (tests/fake_ollama.py) on four tasks and counts every request
Umbrous sends, with tiktoken's o200k_base when installed (else Umbrous's
4-characters-a-token estimate). Both versions used the same workspace copy.

| Task | Requests | Input before | Input after | Saved | Reprocessed before | after |
| --- | --- | --- | --- | --- | --- | --- |
| Chat without a folder, 3 messages | 3 | 2,077 | 2,044 | 1.6 % | 786 | 775 |
| Read-only question (list, search, 2 reads) | 6 | 9,794 | 8,964 | 8.5 % | 4,450 | 4,205 |
| Coding: add a function, fix, tests pass | 8 | 23,884 | 15,002 | 37.2 % | 17,515 | 3,995 |
| Coding, longer (re-reads, re-runs) | 10 | 50,071 | 36,929 | 26.2 % | 41,172 | 10,565 |
| **All four** | 27 | **85,826** | **62,939** | **26.7 %** | **63,923** | **19,540 (−69.4 %)** |

"Reprocessed" is the part of each request that differs from the start of the
previous one: what a server with prompt caching has to read again.

Limits: the model's choices are scripted, so a real model may take more or
fewer steps; the tokenizer is OpenAI's, and local models (Qwen and others)
count somewhat differently; reply tokens are not included (the model writes
those). Whether a provider bills cached tokens less depends on the provider.

## Next

- Workspace resolution sends the system prompt, the history and a JSON
  inventory of up to 40 folders on every message of a chat without a folder
  (most of the "chat without a folder" cost). A compact inventory would cut it.
- The ChatGPT (Codex) adapter JSON-encodes the transcript, which escapes every
  newline and quote in file contents, and puts the per-step progress record in
  the instructions, ahead of the transcript.
