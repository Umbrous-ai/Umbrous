# Delete chats and projects

In the native app, use **⋯ → Delete chat** or **⋯ → Delete project**, then confirm
once with **Delete**. The dialog closes on success and the item leaves the list.
Archived chats use the same short dialog in **Settings → Archived chats**.
Archive remains a separate, reversible action. **Start fresh** remains available
for explicitly choosing a mixture of chats and memory items.

Deleting a project removes its Umbrous entry, all active and archived chats,
project memory, review suggestions, and stored folder/access configuration.
It never removes the attached source directory or any file in it. Personal
memory, other projects, task records and receipts remain. Even the last project
can be deleted; **Create project** is then shown in the sidebar.

The runtime uses the existing authenticated `/api/reset/preview` and `/api/reset`
endpoints. A selection with `delete_projects: [project_id]` includes the project's
chats and memory and deletes its database references in the same transaction.
An empty project still has a previewable project item. Active replies block
removal. Concurrent changes invalidate the preview instead of silently expanding
the deletion. Normal deletion has one confirmation, not a preview wizard followed
by a result dialog. A second review is needed only if the contents actually change.

## References reviewed on 2026-11-08

- [Claude conversation deletion](https://support.claude.com/en/articles/8230524-delete-or-rename-a-conversation)
  documents a sidebar menu followed by one confirmation. Its
  [project management guide](https://support.claude.com/en/articles/9519177-how-can-i-create-and-manage-projects)
  also documents Delete followed by a confirmation.
- [Codex](https://help.openai.com/en/articles/20001333) documents archive first,
  then permanent deletion in Settings → Archived chats. Umbrous retains that
  Settings entry point and also allows direct deletion of active chats.
- [Hermes dashboard](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md)
  exposes session deletion through a trash icon. This review checked its documented
  entry point, not a logged-in dashboard's exact confirmation wording.
- [T3 Code project deletion issue](https://github.com/pingdotgg/t3code/issues/2866)
  records the empty-project/archived-thread mismatch. Umbrous counts archives in
  its backend preview and tests deletion of a project containing only archives.
  T3 here means T3 Code; T3 Chat's authenticated UI was not inspected.
- [DeepSeek's policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
  confirms chat-history deletion through Settings. Its current live confirmation
  sequence was not verified, so it is not used as evidence of a one-step flow.

## Verification

- `python3 -m unittest tests.test_m8_reset`: project removal, archives, source-file
  preservation, personal memory, last-project deletion, concurrent changes, busy
  replies, rollback, and authenticated API coverage.
- `desktop/scripts/build-native.sh --e2e`, then
  `desktop/e2e/validate-deletion.sh /tmp/umbrous-delete-check`: exercises the
  native UI handlers with disposable data; checks cancellation, one confirmation,
  double-click suppression, automatic dismissal, archived chats, project removal,
  empty state, and creating another project. Checks the database and source file
  independently after the app exits.
