//! The runtime's local HTTP API, called from background threads only.
//!
//! Same contract as the browser page: claim the one-time launch code once, then
//! send the session credential as `X-Umbrous-Token` on every call. The
//! credential lives only in this process's memory. Requests go to
//! 127.0.0.1:<port>, so the runtime's Host check passes; no Origin header is
//! sent (this is not a browser), and the credential check still applies.

use std::io::{BufRead, BufReader};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug, Clone)]
pub struct ApiError {
    pub code: String,
    pub message: String,
    /// M8: a refused reset (preview_changed, chat_busy) carries the new preview
    pub preview: Option<Value>,
}

impl ApiError {
    fn transport(e: impl std::fmt::Display) -> Self {
        ApiError { code: "unreachable".into(), message: format!("The Umbrous runtime is not answering ({e})."), preview: None }
    }
}

pub type Result<T> = std::result::Result<T, ApiError>;

#[derive(Clone)]
pub struct Api {
    origin: String,
    token: String,
    agent: ureq::Agent,
    stream_agent: ureq::Agent,
}

impl std::fmt::Debug for Api {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Api").field("origin", &self.origin).finish_non_exhaustive() // never the token
    }
}

fn agent(read: Duration) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(read)
        .redirects(0)
        .no_delay(true)
        .build()
}

fn to_error(e: ureq::Error) -> ApiError {
    match e {
        ureq::Error::Status(_, resp) => {
            let body: Value = resp.into_json().unwrap_or(Value::Null);
            ApiError {
                code: body.get("error").and_then(Value::as_str).unwrap_or("error").to_string(),
                message: body.get("message").and_then(Value::as_str).unwrap_or("The runtime refused the request.").to_string(),
                preview: body.get("preview").cloned(),
            }
        }
        other => ApiError::transport(other),
    }
}

impl Api {
    /// Trade the one-time launch code for the session credential.
    pub fn claim(origin: &str, launch_code: &str) -> Result<Api> {
        let a = agent(Duration::from_secs(15));
        let resp = a
            .post(&format!("{origin}/api/session/claim"))
            .set("X-Umbrous-Launch", launch_code)
            .call()
            .map_err(to_error)?;
        let v: Value = resp.into_json().map_err(ApiError::transport)?;
        let token = v.get("token").and_then(Value::as_str).ok_or_else(|| ApiError::transport("no token"))?.to_string();
        // Reading a reply can take as long as the model's first-token deadline (180 s by default).
        Ok(Api { origin: origin.to_string(), token, agent: a, stream_agent: agent(Duration::from_secs(700)) })
    }

    pub fn get<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T> {
        self.agent
            .get(&format!("{}{path}", self.origin))
            .set("X-Umbrous-Token", &self.token)
            .call()
            .map_err(to_error)?
            .into_json()
            .map_err(ApiError::transport)
    }

    pub fn post<T: for<'de> Deserialize<'de>>(&self, path: &str, body: Value) -> Result<T> {
        self.agent
            .post(&format!("{}{path}", self.origin))
            .set("X-Umbrous-Token", &self.token)
            .send_json(body)
            .map_err(to_error)?
            .into_json()
            .map_err(ApiError::transport)
    }

    /// POST with the streaming client's long read timeout, for requests that wait on a
    /// model: the tool check makes two model calls of up to 180 s each.
    pub fn post_long<T: for<'de> Deserialize<'de>>(&self, path: &str, body: Value) -> Result<T> {
        self.stream_agent
            .post(&format!("{}{path}", self.origin))
            .set("X-Umbrous-Token", &self.token)
            .send_json(body)
            .map_err(to_error)?
            .into_json()
            .map_err(ApiError::transport)
    }

    /// POST /api/chat and call `on_event` for each JSON line as it arrives.
    /// Returns when the runtime closes the stream. Blocking: run it off the UI thread.
    /// `resume`: an interrupted task to run again (M5) instead of sending `message`.
    /// `use_memory`: false sends the reply without memory items (M6), to compare.
    pub fn chat_stream(&self, chat_id: &str, model: &str, message: &str, resume: Option<&str>, use_memory: bool, controls: &ModelControls,
                       mut on_event: impl FnMut(Value) -> bool) -> Result<()> {
        let mut body = match resume {
            Some(task) => json!({"chat_id": chat_id, "model": model, "resume_task": task}),
            None => json!({"chat_id": chat_id, "model": model, "message": message}),
        };
        if !use_memory {
            body["use_memory"] = json!(false);
        }
        // this app shows approval cards (access_controls.rs), so the gate may ask
        body["approvals"] = json!(true);
        if model.starts_with("codex/") {
            if let Some(effort) = &controls.reasoning_effort { body["reasoning_effort"] = json!(effort); }
            body["speed"] = json!(controls.speed);
        }
        let resp = self
            .stream_agent
            .post(&format!("{}/api/chat", self.origin))
            .set("X-Umbrous-Token", &self.token)
            .send_json(body)
            .map_err(to_error)?;
        let reader = BufReader::new(resp.into_reader());
        for line in reader.lines() {
            let line = line.map_err(ApiError::transport)?;
            if line.trim().is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<Value>(&line) {
                if !on_event(v) {
                    break; // dropping the reader closes the connection; the runtime stops the model
                }
            }
        }
        Ok(())
    }
}

// ---- response shapes (umbrous/history/store.py, umbrous/model/router.py) ----

#[derive(Debug, Clone, Deserialize)]
pub struct Project {
    pub id: String,
    pub name: String,
    pub chats: u32,
    pub archived_chats: u32,
}

#[derive(Debug, Deserialize)]
pub struct Projects {
    pub projects: Vec<Project>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatInfo {
    pub id: String,
    pub project_id: String,
    pub title: String,
    pub updated_at: String,
    pub archived: bool,
    pub messages: u32,
    /// the chat's own access (a workspace profile); None: the project's profile
    #[serde(default)]
    pub access: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct Chats {
    pub chats: Vec<ChatInfo>,
}

#[derive(Debug, Deserialize)]
pub struct OneChat {
    pub chat: ChatInfo,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: String,
    pub model: Option<String>,
    pub status: String,
    pub prompt_tokens: Option<u64>,
    pub reply_tokens: Option<u64>,
    /// the task this reply was (M4): its report is fetched from /api/tasks
    #[serde(default)]
    pub task_id: Option<String>,
    /// the memory items this reply was given (M6), as a reference
    #[serde(default)]
    pub memory_ids: Option<Vec<String>>,
}

/// GET /api/memory?project_id= (umbrous/memory/store.py, M6): personal memory
/// and the selected project's memory, two separate stores.
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryItem {
    pub id: String,
    pub store: String,
    pub kind: String,
    pub text: String,
    /// personal only: whether its scope includes the selected project
    #[serde(default)]
    pub applies: Option<bool>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MemoryView {
    pub project_id: Option<String>,
    pub personal: Vec<MemoryItem>,
    pub project: Vec<MemoryItem>,
    /// M7: suggestions from tasks, waiting for review (never retrieved until accepted)
    #[serde(default)]
    pub candidates: Vec<Candidate>,
}

/// One suggestion for memory after a task (umbrous/memory/review.py, M7).
#[derive(Debug, Clone, Deserialize)]
pub struct Candidate {
    pub id: String,
    pub store: String,
    pub kind: String,
    pub text: String,
    pub quote: String,
    /// "message" (your own words) or "reply"
    pub basis: String,
    /// personal: "all" or a list of project ids; project: the project id
    pub scope: Value,
}

impl Candidate {
    /// Where accepting it writes it, as the panel shows it.
    pub fn destination(&self, project_name: &str) -> String {
        if self.store == "project" {
            return format!("this project · {}", self.kind);
        }
        let where_ = if self.scope.as_str() == Some("all") { "all projects".to_string() } else { project_name.to_string() };
        format!("personal {} · {}", self.kind, where_)
    }

    pub fn source(&self) -> String {
        let from = if self.basis == "message" { "your message" } else { "the reply" };
        format!("From {from}: “{}”", self.quote)
    }
}

// ---- M8: selective start-fresh reset (umbrous/reset.py) ----

/// What you ticked in Start fresh. `selection()` is what the runtime reads:
/// each part names one store, so nothing here can mean "everything".
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ResetChoice {
    pub project_id: String,
    pub all_chats: bool,
    pub chats: std::collections::BTreeSet<String>,
    pub all_personal: bool,
    pub personal: std::collections::BTreeSet<String>,
    pub all_project: bool,
    pub project: std::collections::BTreeSet<String>,
    pub start_fresh: bool,
}

impl ResetChoice {
    pub fn selection(&self) -> Value {
        let chats_in: Vec<&String> = if self.all_chats { vec![&self.project_id] } else { vec![] };
        let personal = if self.all_personal { json!("all") } else { json!(self.personal) };
        let mut project = serde_json::Map::new();
        if self.all_project {
            project.insert(self.project_id.clone(), json!("all"));
        } else if !self.project.is_empty() {
            project.insert(self.project_id.clone(), json!(self.project));
        }
        json!({"chats": self.chats, "chats_in": chats_in, "personal": personal, "project": project,
               "start_fresh": self.start_fresh})
    }

    pub fn is_empty(&self) -> bool {
        !(self.all_chats || self.all_personal || self.all_project)
            && self.chats.is_empty() && self.personal.is_empty() && self.project.is_empty()
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetItem {
    #[serde(rename = "type")]
    pub item_type: String, // chat | personal | project | suggestion
    pub id: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub store: Option<String>,
    #[serde(default)]
    pub messages: u32,
    #[serde(default)]
    pub archived: bool,
    #[serde(default)]
    pub busy: bool,
}

impl ResetItem {
    /// The line the preview shows for it.
    pub fn label(&self) -> String {
        match self.item_type.as_str() {
            "chat" => self.title.clone().unwrap_or_default(),
            "suggestion" => format!("A {} {} suggestion from a chat above",
                                    self.store.as_deref().unwrap_or(""), self.kind.as_deref().unwrap_or("")),
            _ => self.text.clone().unwrap_or_default(),
        }
    }

    pub fn tag(&self) -> String {
        match self.item_type.as_str() {
            "chat" => {
                let mut t = format!("chat · {} messages", self.messages);
                if self.archived {
                    t.push_str(" · archived");
                }
                if self.busy {
                    t.push_str(" · a reply is running");
                }
                t
            }
            "personal" => format!("personal memory · {}", self.kind.as_deref().unwrap_or("")),
            "project" => format!("project memory · {}", self.kind.as_deref().unwrap_or("")),
            _ => "waiting suggestion · quotes the chat".into(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetKept {
    pub what: String,
    pub count: u64,
    pub text: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetPreview {
    pub preview_id: String,
    pub items: Vec<ResetItem>,
    pub kept: Vec<ResetKept>,
    pub not_touched: String,
    pub backups: String,
    #[serde(default)]
    pub blocked: Vec<String>,
    #[serde(default)]
    pub empty: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetDeleted {
    pub chats: u64,
    pub messages: u64,
    pub personal: u64,
    pub project: u64,
    pub suggestions: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetIdOnly {
    pub id: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResetDone {
    pub deleted: ResetDeleted,
    pub items: Vec<ResetIdOnly>,
    pub kept: Vec<ResetKept>,
    pub backups: String,
    #[serde(default)]
    pub start_fresh: bool,
}

impl ResetDone {
    pub fn summary(&self) -> String {
        let d = &self.deleted;
        let mut s = format!("Deleted {} chat(s) with {} message(s), {} personal and {} project memory item(s)",
                            d.chats, d.messages, d.personal, d.project);
        if d.suggestions > 0 {
            s.push_str(&format!(", {} waiting suggestion(s)", d.suggestions));
        }
        s.push('.');
        s
    }
}

/// The reply's meta line says how many suggestions wait for review (M7).
pub fn suggestion_note(n: usize) -> Option<String> {
    match n {
        0 => None,
        1 => Some("1 memory suggestion to review".into()),
        n => Some(format!("{n} memory suggestions to review")),
    }
}

/// The reply's meta line names the memory items it was given (M6).
pub fn memory_note(ids: &[String]) -> Option<String> {
    if ids.is_empty() {
        None
    } else {
        Some(format!("memory {}", ids.join(", ")))
    }
}

/// GET /api/tasks?chat_id= (umbrous/tasks/store.py, M4): each task with its report.
#[derive(Debug, Deserialize)]
pub struct TaskList {
    pub tasks: Vec<TaskBrief>,
}

#[derive(Debug, Deserialize)]
pub struct TaskBrief {
    pub task_id: String,
    #[serde(default)]
    pub report: Option<Value>,
    /// M5: set for a task whose outcome is unknown (interrupted by a crash or restart)
    #[serde(default)]
    pub resume: Option<Resume>,
}

/// Whether an interrupted task can be resumed (umbrous/tasks/recover.py, M5).
#[derive(Debug, Clone, Deserialize)]
pub struct Resume {
    pub possible: bool,
    /// what resuming means (when possible), or why it is not possible
    #[serde(default)]
    pub message: String,
    /// the message that was waiting, when it can be resumed
    #[serde(default)]
    pub text: Option<String>,
    /// M9: changes the interrupted attempt made (kept, not made again)
    #[serde(default)]
    pub done: Vec<ResumeItem>,
    /// M9: files changed by something else meanwhile (left alone)
    #[serde(default)]
    pub conflicts: Vec<String>,
    /// M9: commands that were running, whose effects are unknown
    #[serde(default)]
    pub commands_unknown: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ResumeItem {
    #[serde(default)]
    pub resource: String,
}

/// M9: what resuming a coding task builds on, in one line, or None.
pub fn resume_note(r: &Resume) -> Option<String> {
    let mut parts = Vec::new();
    if !r.done.is_empty() {
        parts.push(format!("Already changed (kept, not made again): {}",
                           r.done.iter().map(|d| d.resource.as_str()).collect::<Vec<_>>().join(", ")));
    }
    if !r.conflicts.is_empty() {
        parts.push(format!("Changed by something else meanwhile (left alone): {}", r.conflicts.join(", ")));
    }
    if !r.commands_unknown.is_empty() {
        parts.push("A command was running and may have changed files; it is not re-run by itself".into());
    }
    if parts.is_empty() { None } else { Some(parts.join(". ") + ".") }
}

/// M9: what a running step is doing, for the reply's meta line while it runs.
/// M9: whether the chat stream is read on after an event of type `ty`.
///
/// Before Stop, always. After Stop, when the runtime was asked to cancel the
/// run, until its last event (`done` or `error`): a stopped task can send
/// several events first (a cancelled command's result, the next tool call and
/// its cancelled result, …) and the last one carries "Stopped by you" and the
/// task's report. When it could not be asked (Stop came before the run had an
/// id), reading ends here, which closes the connection, and the runtime stops
/// the run for that.
pub fn keep_reading(stopped: bool, cancel_sent: bool, ty: &str) -> bool {
    if !stopped {
        return true;
    }
    cancel_sent && !matches!(ty, "done" | "error")
}

pub fn activity_icon(tool: &str) -> &'static str {
    match tool {
        "read_file" => "doc",
        "list_files" => "folder",
        "search_files" | "find_files" => "search",
        "write_file" | "edit_file" => "pen",
        "run_command" => "terminal",
        "check_command" => "shield-check",
        _ => "bolt",
    }
}

pub fn step_label(tool: &str, args: &Value) -> String {
    let s = |k: &str| args.get(k).and_then(Value::as_str).filter(|p| !p.is_empty());
    let path = s("path").unwrap_or(".");
    let what = match tool {
        "read_file" => format!("reading {path}"),
        "list_files" => format!("listing {path}"),
        "search_files" => format!("searching {path}"),
        "find_files" => format!("finding files named {}", s("pattern").unwrap_or("?")),
        "write_file" => format!("writing {path}"),
        "edit_file" => format!("editing {path}"),
        "run_command" => format!("running {}", s("command").map(|c| c.split_whitespace().collect::<Vec<_>>().join(" "))
                                                   .unwrap_or_else(|| "a command".into())),
        "check_command" => "checking a command".into(),
        _ => "running a tool".into(),
    };
    what.chars().take(80).collect()
}

/// M9: the folder a task works in, from the start event's tools, for a notice:
/// shown when "work on <folder>" attached it, or when it is Umbrous's own source.
pub fn folder_note(tools: &Value) -> Option<String> {
    let root = tools.get("root").and_then(Value::as_str)?;
    let attached = tools.pointer("/selected/attached").and_then(Value::as_bool).unwrap_or(false);
    let own = tools.get("self_source").and_then(Value::as_bool).unwrap_or(false);
    let mut s = format!("Workspace: {root}");
    if attached {
        s.push_str(" (attached now with Code in workspace: Umbrous may edit files and run commands there, as you, without a sandbox)");
    }
    if own {
        s.push_str(". This is Umbrous's own source: changes take effect only after a rebuild and restart");
    }
    Some(s + ".")
}

/// One line summing up a task report (M4), for the reply's meta line, and
/// whether it needs attention (something failed, was not checked, or a limit
/// was reached). The full trace is in the browser page and `bin/umbrous tasks`.
pub fn report_line(rep: &Value) -> Option<(String, bool)> {
    let status = rep.get("status")?.as_str()?;
    let n = |k: &str| rep.get(k).and_then(Value::as_u64).unwrap_or(0);
    let len = |k: &str| rep.get(k).and_then(Value::as_array).map(|a| a.len()).unwrap_or(0);
    let done = rep.get("actions").and_then(|a| a.get("completed")).and_then(Value::as_u64).unwrap_or(0);
    let word = match status {
        "completed" => "Task completed",
        "cancelled" => "Task stopped",
        "failed" => "Task failed",
        _ => "Task outcome unknown",
    };
    let steps = n("steps");
    let mut parts = vec![
        word.to_string(),
        format!("{steps} step{}", if steps == 1 { "" } else { "s" }),
        format!("{done} of {} actions completed", n("tool_calls")),
    ];
    let limits: Vec<&str> = rep.get("limits_hit").and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
    for l in &limits {
        parts.push(match *l {
            "steps" => "step limit reached".into(),
            "tool_calls" => "action limit reached".into(),
            "task_time" => "time limit reached".into(),
            other => format!("{other} limit reached"),
        });
    }
    // M9: what a coding task changed and ran
    let changes = len("changes");
    let cmds: Vec<&Value> = rep.get("commands").and_then(Value::as_array).map(|a| a.iter().collect()).unwrap_or_default();
    if changes > 0 || !cmds.is_empty() {
        let bad = cmds.iter().filter(|c| c.get("status").and_then(Value::as_str) != Some("exited")
                                       || c.get("exit").and_then(Value::as_i64) != Some(0)).count();
        parts.push(format!("{changes} change{}", if changes == 1 { "" } else { "s" }));
        parts.push(format!("{} command{}{}", cmds.len(), if cmds.len() == 1 { "" } else { "s" },
                           if bad > 0 { format!(" ({bad} not passing)") } else { String::new() }));
    }
    let unc = len("uncertainty");
    if unc > 0 {
        parts.push(format!("{unc} thing{} not fully checked", if unc == 1 { "" } else { "s" }));
    }
    let command_failed = cmds.iter().any(|c| {
        c.get("status").and_then(Value::as_str) != Some("exited")
            || c.get("exit").and_then(Value::as_i64) != Some(0)
    });
    if changes > 0 && cmds.is_empty() {
        parts.push("No commands run; changes not verified by checks".into());
    }
    let attention = status != "completed" || unc > 0 || len("failures") > 0
        || !limits.is_empty() || command_failed || (changes > 0 && cmds.is_empty());
    if status == "completed" && attention { parts[0] = "Finished with unresolved checks".into(); }
    Some((parts.join(" · "), attention))
}

#[derive(Debug, Deserialize)]
pub struct Transcript {
    pub chat: ChatInfo,
    pub messages: Vec<Message>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelInfo {
    pub name: String,
    #[serde(default)]
    pub provider: String,
    #[serde(default)]
    pub display_name: String,
    #[serde(default)]
    pub reasoning_efforts: Vec<String>,
    #[serde(default)]
    pub default_reasoning_effort: Option<String>,
    #[serde(default)]
    pub speeds: Vec<String>,
    #[serde(default)]
    pub parameter_size: String,
    pub context_length: Option<u64>,
    #[serde(default)]
    pub tool_capable: bool,
}

impl ModelInfo {
    pub fn is_cloud(&self) -> bool { self.provider == "codex" || self.name.starts_with("codex/") }
    pub fn label(&self) -> &str { if self.display_name.is_empty() { &self.name } else { &self.display_name } }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct ModelControls {
    pub reasoning_effort: Option<String>,
    pub speed: String,
}
impl Default for ModelControls {
    fn default() -> Self { Self { reasoning_effort: None, speed: "standard".into() } }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct CodexStatus {
    #[serde(default)]
    pub available: bool,
    #[serde(default)]
    pub authenticated: bool,
    #[serde(default)]
    pub auth_type: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct CodexLogin { pub auth_url: String }

#[derive(Debug, Clone, Deserialize)]
pub struct Models {
    pub reachable: bool,
    #[serde(default)]
    #[allow(dead_code)]
    pub version: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub models: Vec<ModelInfo>,
}

/// GET /api/projects/<id>/access (umbrous/gate/access.py, M3). Only what the app uses.
#[derive(Debug, Clone, Deserialize)]
pub struct AccessWorkspace {
    pub name: String,
    pub profile: String,
    /// M9: the folder, shown in Access and checked by the e2e driver
    #[serde(default)]
    pub root: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Access {
    pub project_id: String,
    pub workspace: Option<AccessWorkspace>,
}

#[allow(dead_code)] // logged nowhere yet; kept as the claim step's sanity check
#[derive(Debug, Clone, Deserialize)]
pub struct Health {
    pub version: String,
    pub milestone: String,
    pub schema_version: u32,
    pub bind: String,
    #[serde(default)]
    pub uptime_s: u64,
    #[serde(default)]
    pub ollama_exposure: Option<Value>,
}

#[cfg(test)]
mod tests {
    #[test]
    fn stop_reads_on_to_the_last_event() {
        // Stop during a batch of tool calls: everything up to the final error is read
        let seq = ["tool_result", "tool_call", "tool_result", "tool_call", "tool_result", "error"];
        let read: Vec<bool> = seq.iter().map(|t| keep_reading(true, true, t)).collect();
        assert_eq!(read, [true, true, true, true, true, false]);
        assert!(!keep_reading(true, true, "done"));
        // not stopped: every event
        assert!(seq.iter().all(|t| keep_reading(false, false, t)));
        // Stop before the run had an id: no cancel was sent, so reading ends (closing the stream)
        assert!(!keep_reading(true, false, "start"));
        assert!(!keep_reading(true, false, "tool_result"));
    }

    use super::*;

    #[test]
    fn report_line_counts_changes_and_commands() {
        let rep = json!({"status": "completed", "steps": 7, "tool_calls": 6, "actions": {"completed": 6},
                         "changes": [{"path": "calc.py"}, {"path": "calc.py"}],
                         "commands": [{"status": "exited", "exit": 1}, {"status": "exited", "exit": 0}]});
        assert_eq!(report_line(&rep).unwrap().0,
                   "Finished with unresolved checks · 7 steps · 6 of 6 actions completed · 2 changes · 2 commands (1 not passing)");
        let rep = json!({"status": "completed", "steps": 2, "tool_calls": 1, "actions": {"completed": 1},
                         "changes": [], "commands": [{"status": "timed_out", "exit": null}]});
        assert_eq!(report_line(&rep).unwrap().0,
                   "Finished with unresolved checks · 2 steps · 1 of 1 actions completed · 0 changes · 1 command (1 not passing)");
    }

    #[test]
    fn step_labels_and_folder_notes() {
        assert_eq!(step_label("edit_file", &json!({"path": "calc.py"})), "editing calc.py");
        assert_eq!(step_label("run_command", &json!({"command": "python3  -m\nunittest"})), "running python3 -m unittest");
        assert_eq!(step_label("read_file", &json!({})), "reading .");
        assert_eq!(step_label("nope", &json!({})), "running a tool");
        assert_eq!(folder_note(&json!({"root": "/p", "selected": {"attached": false}, "self_source": false})), Some("Workspace: /p.".into()));
        assert!(folder_note(&json!({"root": "/p", "selected": {"attached": true}})).unwrap().starts_with("Workspace: /p (attached now"));
        assert!(folder_note(&json!({"root": "/u", "self_source": true})).unwrap().contains("rebuild and restart"));
        let r: Resume = serde_json::from_value(json!({"possible": true, "done": [{"tool": "edit_file", "resource": "calc.py"}],
                                                      "commands_unknown": ["."]})).unwrap();
        assert_eq!(resume_note(&r).unwrap(), "Already changed (kept, not made again): calc.py. A command was running and may have changed files; it is not re-run by itself.");
        let plain: Resume = serde_json::from_value(json!({"possible": true})).unwrap();
        assert_eq!(resume_note(&plain), None);
    }

    #[test]
    fn report_line_sums_up_a_task() {
        let rep = json!({"status": "completed", "steps": 4, "tool_calls": 3, "actions": {"completed": 3},
                         "uncertainty": ["x"], "failures": [], "limits_hit": []});
        assert_eq!(report_line(&rep), Some(("Finished with unresolved checks · 4 steps · 3 of 3 actions completed · 1 thing not fully checked".into(), true)));
        let rep = json!({"status": "cancelled", "steps": 1, "tool_calls": 0, "actions": {}, "uncertainty": [],
                         "failures": [], "limits_hit": []});
        assert_eq!(report_line(&rep), Some(("Task stopped · 1 step · 0 of 0 actions completed".into(), true)));
        let rep = json!({"status": "completed", "steps": 7, "tool_calls": 6, "actions": {"completed": 6},
                         "uncertainty": ["a", "b"], "failures": [], "limits_hit": ["steps"]});
        assert_eq!(report_line(&rep).unwrap().0,
                   "Finished with unresolved checks · 7 steps · 6 of 6 actions completed · step limit reached · 2 things not fully checked");
        let clean = json!({"status": "completed", "steps": 2, "tool_calls": 1, "actions": {"completed": 1},
                           "uncertainty": [], "failures": [], "limits_hit": []});
        assert!(!report_line(&clean).unwrap().1);
        assert_eq!(report_line(&json!({})), None);
    }

    #[test]
    fn reset_choice_names_each_store_and_never_everything() {
        let mut c = ResetChoice { project_id: "prj_a".into(), start_fresh: true, ..Default::default() };
        assert!(c.is_empty());
        c.chats.insert("chat_1".into());
        c.personal.insert("mem_b".into());
        c.personal.insert("mem_a".into());
        assert_eq!(c.selection(), json!({"chats": ["chat_1"], "chats_in": [], "personal": ["mem_a", "mem_b"],
                                         "project": {}, "start_fresh": true}));
        c.all_personal = true;
        c.all_project = true;
        c.all_chats = true;
        assert_eq!(c.selection(), json!({"chats": ["chat_1"], "chats_in": ["prj_a"], "personal": "all",
                                         "project": {"prj_a": "all"}, "start_fresh": true}));
    }

    #[test]
    fn reset_preview_and_result_parse() {
        let p: ResetPreview = serde_json::from_value(json!({"preview_id": "rst_1", "selection": {}, "counts": {},
            "items": [{"type": "chat", "id": "chat_1", "title": "Plan", "project_id": "prj_a", "project": "A",
                       "messages": 4, "archived": true, "busy": false},
                      {"type": "personal", "id": "mem_1", "kind": "fact", "text": "Tea", "scope": "all"},
                      {"type": "suggestion", "id": "cand_1", "chat_id": "chat_1", "store": "project", "kind": "lesson"}],
            "kept": [{"what": "task_records", "count": 2, "text": "2 stay"}], "not_touched": "n", "backups": "b",
            "blocked": [], "empty": false, "start_fresh": true})).unwrap();
        assert_eq!(p.items[0].label(), "Plan");
        assert_eq!(p.items[0].tag(), "chat · 4 messages · archived");
        assert_eq!(p.items[1].tag(), "personal memory · fact");
        assert_eq!(p.items[2].label(), "A project lesson suggestion from a chat above");
        let d: ResetDone = serde_json::from_value(json!({"done": true, "deleted": {"chats": 1, "messages": 2,
            "personal": 1, "project": 0, "suggestions": 0, "pending_turns": 0}, "items": [{"type": "chat", "id": "chat_1"}],
            "kept": [], "not_touched": "n", "backups": "b", "start_fresh": true})).unwrap();
        assert_eq!(d.summary(), "Deleted 1 chat(s) with 2 message(s), 1 personal and 0 project memory item(s).");
    }

    #[test]
    fn candidates_parse() {
        let v: MemoryView = serde_json::from_value(json!({"project_id": "prj_general", "personal": [], "project": [],
            "candidates": [
                {"id": "cand_1", "store": "personal", "kind": "preference", "text": "Prefers lists",
                 "quote": "I prefer lists", "basis": "message", "scope": ["prj_general"], "status": "pending"},
                {"id": "cand_2", "store": "project", "kind": "lesson", "text": "Keep backups",
                 "quote": "Lesson: keep backups", "basis": "reply", "scope": "prj_general", "status": "pending"}]}))
            .unwrap();
        assert_eq!(v.candidates[0].destination("General"), "personal preference · General");
        assert_eq!(v.candidates[1].destination("General"), "this project · lesson");
        assert_eq!(v.candidates[0].source(), "From your message: “I prefer lists”");
        assert_eq!(v.candidates[1].source(), "From the reply: “Lesson: keep backups”");
        assert_eq!(suggestion_note(0), None);
        assert_eq!(suggestion_note(2).unwrap(), "2 memory suggestions to review");
    }

    #[test]
    fn memory_view_and_ids_parse() {
        let v: MemoryView = serde_json::from_value(json!({"project_id": "prj_general", "limits": {},
            "personal": [{"id": "mem_0123456789abcdef", "store": "personal", "kind": "preference", "text": "tea",
                          "scope": "all", "source": {"type": "manual"}, "approval": "approved",
                          "created_at": "x", "updated_at": "x", "applies": true}],
            "project": [{"id": "mem_1123456789abcdef", "store": "project", "kind": "decision", "text": "ship",
                         "scope": "prj_general", "source": {"type": "manual"}, "approval": "approved",
                         "created_at": "x", "updated_at": "x"}]})).unwrap();
        assert_eq!(v.personal[0].applies, Some(true));
        assert_eq!(v.project[0].applies, None);
        let m: Message = serde_json::from_value(json!({"role": "assistant", "content": "x", "model": "m",
            "status": "complete", "prompt_tokens": 1, "reply_tokens": 1, "memory_ids": ["mem_a", "mem_b"]})).unwrap();
        assert_eq!(memory_note(m.memory_ids.as_deref().unwrap()).unwrap(), "memory mem_a, mem_b");
        let old: Message = serde_json::from_value(json!({"role": "assistant", "content": "x", "model": "m",
            "status": "complete", "prompt_tokens": null, "reply_tokens": null, "memory_ids": null})).unwrap();
        assert!(old.memory_ids.is_none());
        assert_eq!(memory_note(&[]), None);
        assert!(v.candidates.is_empty());   // an M6 runtime sends none
    }

    #[test]
    fn interrupted_task_parses_with_its_resume_info() {
        let list: TaskList = serde_json::from_value(json!({"tasks": [
            {"task_id": "tsk_a", "status": "completed", "report": {"status": "completed"}},
            {"task_id": "tsk_b", "status": "unknown", "report": {"status": "unknown", "steps": 1, "tool_calls": 1,
             "actions": {"unknown": 1}, "uncertainty": ["x"]},
             "resume": {"possible": true, "reason": null, "message": "Resuming runs the task again.", "text": "who?"}},
            {"task_id": "tsk_c", "status": "unknown", "resume": {"possible": false, "reason": "already_resumed",
             "message": "This task was already resumed."}}]})).unwrap();
        assert!(list.tasks[0].resume.is_none());
        let r = list.tasks[1].resume.as_ref().unwrap();
        assert!(r.possible);
        assert_eq!(r.text.as_deref(), Some("who?"));
        assert_eq!(report_line(list.tasks[1].report.as_ref().unwrap()).unwrap().0,
                   "Task outcome unknown · 1 step · 0 of 1 actions completed · 1 thing not fully checked");
        assert!(!list.tasks[2].resume.as_ref().unwrap().possible);
    }
}
