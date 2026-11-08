# Runtime boundaries

The repository-wide refactor separates reusable infrastructure from domain behavior. It preserves the public HTTP routes, stored data, CLI commands, gate policy, and M1–M9 behavior; it does not rewrite working algorithms merely to change their shape.

| Area | Responsibility |
| --- | --- |
| `api/server.py`, `api/security.py`, `api/contracts.py` | Loopback transport, authentication, origin and body limits, dispatch, shared route/error contracts |
| `api/*_routes.py` | History, access, memory, tasks/reset, and model endpoint behavior behind the common dispatcher |
| `cli.py`, `commands.py` | Argument parsing/startup separated from store-backed commands |
| `model/factory.py`, `router.py`, `settings.py` | Consistent provider construction, exact model routing, and capability-checked per-request settings |
| `model/ollama.py`, `model/codex.py` | Provider protocol adapters; they return replies and proposals, never execute Umbrous tools |
| `runloop/chat.py`, `runloop/coordination.py` | Task orchestration separated from chat locks and cancellation registry |
| `gate`, `tools`, `tasks` | Current-scope authorization, execution, durable intent and recovery |
| `persistence/session.py`, `timeutil.py` | Shared SQLite connection lifecycle and timestamp formatting across stores |
| `web/model-controls.js`, `desktop/app/src/model_controls.rs` | Capability-driven model preferences and explicit cloud selection, separate from chat rendering |

SQLite helpers retain explicit transaction ownership: they close connections reliably without silently committing caller work. Foreign keys remain enabled, and stores requiring secure deletion request it explicitly. Model settings are immutable and per request, so concurrent chats cannot overwrite one another's controls.

The security review found and fixed a stale approval grant: changing a workspace or profile while an approval waited could authorize the new scope with the old decision. Policy-version checks and transactional updates now reject that decision. Execution also rechecks its workspace snapshot. Credential redaction normalizes camelCase and hyphenated key names.

The review covered the authentication/HTTP, provider, frontend login, approval, filesystem/process, recovery, persistence, and redaction boundaries. Tests support those specific conclusions; they do not prove the absence of vulnerabilities. The documented unsandboxed Code profile and process escape/recovery limits remain in [M9](m9-coding.md).
