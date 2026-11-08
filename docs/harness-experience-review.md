# Umbrous experience review — 8 November 2026

Goal: make starting, following, and finishing local work predictable. Use
DeepSeek Harness's information hierarchy as a reference, while retaining
Umbrous's identity, local storage, and explicit user control.

## Evidence and limits

Captured the installed Umbrous and DeepSeek Harness apps in this review:
new chat, settings/archive, memory, DeepSeek new session, optional tools pane,
and access menu. Screenshots are in `/tmp/umbrous-design-review/` on the review
machine; they are intentionally not committed because they include account and
local project information. Hermes behavior below is official-documentation
research, not a hands-on audit. No model prompts were sent through the reference
app and no accounts, permissions, or reference sessions were modified.

1. Umbrous new chat: usable but poor hierarchy. An empty third column and a long
   introductory notice compete with the composer. The placeholder incorrectly
   says local model when ChatGPT is selected. Fixed in this change.
2. Umbrous Settings / Archived chats: clear recovery location, but sparse settings
   navigation. Existing restore/delete flows remain; no extra confirmation added.
3. Umbrous Settings / Memory: personal and project scopes are visible and editable.
   Keep here, with provenance in replies, rather than a permanent sidebar.
4. DeepSeek new session: strong entry hierarchy; centered composer, workspace and
   mode nearby, tools closed initially. Adapted the hierarchy, not its branding.
5. DeepSeek tools pane: optional workspace files, terminal, browser. Umbrous's old
   pane had static +0/-0 and nonfunctional source affordances. Replaced it with
   real workspace/model/access/activity data and working folder/session links.
6. DeepSeek access menu: three concise modes. Umbrous already exposes scoped
   access; retain the actual permission gate and explicit per-command approvals.

Accessibility: Umbrous's native accessibility tree exposed window/menu elements
but not these custom controls. DeepSeek exposed named buttons and an editable
composer. This is a material remaining gap for VoiceOver and automated focus
navigation, not proof of full compliance in either product. Color-only running
indicators also need text equivalents throughout the navigation.

## Behavior comparison and improvement decisions

| Area | Reference evidence | Umbrous decision / status |
| --- | --- | --- |
| Start a chat | DeepSeek starts around a workspace and composer [1] | Keep immediate chat without requiring a project; center the new-chat entry and use action-oriented text. Implemented. |
| Find the work | Workspace selection is explicit in DeepSeek [1]; Hermes shares session context across clients [2] | Preserve bounded model-driven local workspace resolution and continue the original request. Already implemented and tested previously. |
| Navigation | Hermes execution belongs to gateway, not client [3] | Switching chat retains streams; dropped HTTP observers now leave accepted work running. Implemented and tested. |
| End of background work | DeepSeek retains unread/running/awaiting sessions in navigation [4] | Done / Needs attention stays until opened; approval state remains attached to its chat. Implemented. Unread state is process-local. |
| Stop / delete | DeepSeek archives with a stop-and-archive branch [4]; Hermes busy deletion is refused [5] | Retain one confirmation, cancel only selected work, then delete under locks. Preserve source folders. Already implemented; regression tested. |
| Archive / restore | DeepSeek supports archive undo and archive filters [4] | Existing Settings restore works. A transient Undo action is a future improvement, not implemented here. |
| Task state | DeepSeek conversation UI groups progress and pending inputs [6] | Keep truthful terminal reports. On connection loss say outcome unknown, not Stopped. Optional activity panel links to background chats. Implemented. |
| Follow-up while busy | DeepSeek distinguishes queued messages from steering [6] | Umbrous still uses explicit Stop before another send. Durable per-chat queue/steer requires a separate lifecycle change; do not fake it with a disabled button. |
| Permissions | DeepSeek exposes Read Only / Workspace Write / Full access in captured UI | Retain short choices plus exact action approvals. Do not loosen access to imitate autonomy. |
| Memory | Hermes has bounded persistent personal notes and user facts [7] | Keep inspect/edit/forget in Settings, personal/project separation and provenance. Existing implementation retained. |
| Models | Hermes Desktop shares core configuration [2]; DeepSeek composer shows model and effort | Umbrous retains capability-driven models/reasoning. Removed misleading local-only placeholder. Provider selection still governs data destination. |
| Tools and files | Captured DeepSeek tools pane contains actual tools | Remove inert attachment and source controls; optional context pane shows actual state. Full file explorer/terminal/browser are not implemented. |
| Recovery | Hermes distinguishes disconnect from gateway crash; no automatic replay [3] | Preserve interrupted-task review/resume; do not automatically repeat side effects. Runtime exit still stops work. |
| Verification and undo | Umbrous already stores task/receipt evidence | Keep honest completion reports. Future: clickable changed-file diffs and scoped undo after conflict checks. Static success counters removed now. |
| Search and navigation | DeepSeek exposes session search and grouped workspaces | Future: indexed cross-project search, pinning, and keyboard navigation with real semantics. Do not add decorative controls. |

## What would make Umbrous better for this workflow

Measure improvement through outcomes: a new request always produces visible
progress or a useful error; changing chats never cancels work; deletion completes
once without reviving deleted data; every completion distinguishes changes,
verification, and unresolved work. Avoid claiming general superiority without
comparative task trials.

Next implementation priorities are accessible native controls, durable
reattachment and unread state, queued follow-ups, a real changed-files view, and
archive undo. These are recommendations from the broad review, not shipped
features in this patch. The current change deliberately improves the existing
surfaces without adding a second agent architecture or copying competitor code.

## Sources

[1]: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/index.md
[2]: https://hermes-agent.nousresearch.com/docs/user-guide/desktop
[3]: https://hermes-agent.nousresearch.com/docs/user-guide/sessions
[4]: https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-workspace/README.md
[5]: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md
[6]: https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-conversation/README.md
[7]: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory

## Fresh verification

- Backend lifecycle/history/approval/tasks/recovery/reset regression: 130 tests
  passed. Three disconnect tests additionally passed (completion exactly once,
  explicit Stop after detach, and deletion after detach).
- Native end-to-end scenario: 31 assertions passed with disposable data; checks
  navigation, unread completion clearing, concurrent project deletion, restored
  approval, and deletion while awaiting approval. Independent SQLite checks pass.
- Rust check with the e2e feature, signed e2e build, and signed release build pass.
- Restarted the release app and visually checked the centered entry, real context
  pane, and existing transcript. The composer returns to the bottom in a chat.
- Visual evidence and numbered walkthrough are also saved outside the repo in
  the Codex visualizations folder under `harness-review/review.html`.

### Compact live activity (2026-11-08)

DeepSeek's [group-title rules](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-chat/src/client/conversation-nodes/README.md#group-title-rules) describe the current tool category rather than a numbered step. Umbrous now uses a single live line with its existing SVG assets: CPU for thinking between actions, checklist for finishing, folder for workspace resolution/listing, document for reading, search for searching, pen for editing or responding, terminal for commands, and shield for approval. Proposed actions say “Preparing action”; only execution-start events show an action as running. Tool completion returns to “Thinking” so a finished command does not appear to keep running. Model identity remains in the composer and completion metadata; final verification warnings remain visible. Terminal events clear the live icon, and an unexpected stream end is detected independently of the label's punctuation.
