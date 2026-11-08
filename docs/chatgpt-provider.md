# ChatGPT subscription

Umbrous can use your ChatGPT subscription through the official local Codex app-server. It does not turn a ChatGPT subscription into API credits or copy authentication tokens. Codex owns sign-in and credential storage. Ollama remains available for local inference; Umbrous never silently switches providers, models, reasoning, or speed.

## Setup

1. Install the official Codex CLI, or use the CLI bundled with the Codex/ChatGPT desktop app. Umbrous searches `PATH` and the supported macOS application locations.
2. Open Umbrous's model menu and select **Connect ChatGPT**. Complete the official browser sign-in, then refresh models. An existing Codex ChatGPT login is recognized automatically. An API-key login does not qualify for this provider.
3. Explicitly select a ChatGPT model. Choose one of its advertised reasoning levels, then **Standard** or **Fast**. Fast is available only when the catalog advertises the priority service tier; it can consume subscription allowance faster. Availability and account limits are enforced by Codex.
4. Run the model's tool check before coding: use **Check tools** in the browser model menu, or `bin/umbrous probe codex/MODEL` for the shared native-app data folder. Only a model that completes the harmless tool round trip can drive Umbrous tools. Refresh the native model menu afterward.

Controls are remembered per model in each frontend. Each request captures its own settings; changing controls during a reply affects the next request. Unsupported or stale choices fail visibly rather than changing to another setting. Standard is the initial speed. To disable discovery and login entirely, set `"codex_enabled": false` in your Umbrous configuration.

## What leaves the computer

Selecting ChatGPT sends the request's conversation context, selected memory, tool descriptions, and approved tool results to OpenAI. This can include project source read by Umbrous. Existing local history, task, and receipt retention still applies. Selecting Ollama continues to use the configured Ollama endpoint.

Each model round uses an ephemeral Codex thread in an empty temporary directory. The adapter disables native shell/file actions, web search, hooks, plugins, apps, inherited MCP servers, memory, and other executable integrations. It verifies inherited MCP servers expose no active tools and rejects server action or permission requests. Returned model, reasoning, and speed must match the request. Codex receives no Umbrous session token or API-key environment variables.

The model returns a validated structured reply and tool proposals. **Only Umbrous's existing permission gate executes actions.** A cloud model does not gain additional project access. Approvals are checked against the current workspace and policy, including after a pending approval's scope changes.

## Limits and testing

This integration uses experimental app-server fields. It was tested with Codex CLI `0.167.0-alpha.3.2`; incompatible versions fail with an error. Catalog entries do not guarantee entitlement or remaining allowance. Sign-in, rate limits, service availability, and model errors never trigger API-key billing or local/cloud fallback.

Structured output is buffered until validated, so partial tokens are not displayed. Stop closes the model connection, and the existing task cleanup stops Umbrous commands. The existing Code profile runs commands unsandboxed; its filesystem/process limitations still apply. See [M9](m9-coding.md).

Automated tests cover protocol limits, cancellation, authentication, disabled integrations, exact settings, provider routing, and browser/native controls. Live synthetic checks exercised Standard, Fast, the tool round trip, rejection of native Codex tool execution, and a complete read/edit/test task through the HTTP API. They do not establish coding quality for every model or project.

To repeat the live coding check, run `python3 -m tests.live_codex_smoke --live --model codex/MODEL` with a model from your catalog. Optional flags are `--reasoning LEVEL` and `--speed standard|fast`. It uses a throwaway project and home, consumes subscription allowance, and permits model-proposed commands under the unsandboxed Code profile. The ordinary automated suite uses fake providers and does not consume subscription allowance.

Official references: [Codex app-server](https://learn.chatgpt.com/docs/app-server) and [Codex authentication](https://learn.chatgpt.com/docs/auth).
