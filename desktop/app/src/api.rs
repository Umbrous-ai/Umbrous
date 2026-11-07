//! The runtime's local HTTP API, called from background threads only.
//!
//! Same contract as the browser page: claim the one-time launch code once, then
//! send the session credential as `X-Umbrous-Token` on every call. The
//! credential lives only in this process's memory. Requests go to
//! 127.0.0.1:<port>, so the runtime's Host check passes; no Origin header is
//! sent (this is not a browser), and the credential check still applies.

use std::io::{BufRead, BufReader};
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Debug, Clone)]
pub struct ApiError {
    pub code: String,
    pub message: String,
}

impl ApiError {
    fn transport(e: impl std::fmt::Display) -> Self {
        ApiError { code: "unreachable".into(), message: format!("The Umbrous runtime is not answering ({e}).") }
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

    /// POST /api/chat and call `on_event` for each JSON line as it arrives.
    /// Returns when the runtime closes the stream. Blocking: run it off the UI thread.
    pub fn chat_stream(&self, chat_id: &str, model: &str, message: &str, mut on_event: impl FnMut(Value) -> bool) -> Result<()> {
        let resp = self
            .stream_agent
            .post(&format!("{}/api/chat", self.origin))
            .set("X-Umbrous-Token", &self.token)
            .send_json(json!({"chat_id": chat_id, "model": model, "message": message}))
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
    let unc = len("uncertainty");
    if unc > 0 {
        parts.push(format!("{unc} thing{} not fully checked", if unc == 1 { "" } else { "s" }));
    }
    let attention = status != "completed" || unc > 0 || len("failures") > 0 || !limits.is_empty();
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
    pub parameter_size: String,
    pub context_length: Option<u64>,
    #[serde(default)]
    pub tool_capable: bool,
}

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
    use super::*;

    #[test]
    fn report_line_sums_up_a_task() {
        let rep = json!({"status": "completed", "steps": 4, "tool_calls": 3, "actions": {"completed": 3},
                         "uncertainty": ["x"], "failures": [], "limits_hit": []});
        assert_eq!(report_line(&rep), Some(("Task completed · 4 steps · 3 of 3 actions completed · 1 thing not fully checked".into(), true)));
        let rep = json!({"status": "cancelled", "steps": 1, "tool_calls": 0, "actions": {}, "uncertainty": [],
                         "failures": [], "limits_hit": []});
        assert_eq!(report_line(&rep), Some(("Task stopped · 1 step · 0 of 0 actions completed".into(), true)));
        let rep = json!({"status": "completed", "steps": 7, "tool_calls": 6, "actions": {"completed": 6},
                         "uncertainty": ["a", "b"], "failures": [], "limits_hit": ["steps"]});
        assert_eq!(report_line(&rep).unwrap().0,
                   "Task completed · 7 steps · 6 of 6 actions completed · step limit reached · 2 things not fully checked");
        let clean = json!({"status": "completed", "steps": 2, "tool_calls": 1, "actions": {"completed": 1},
                           "uncertainty": [], "failures": [], "limits_hit": []});
        assert!(!report_line(&clean).unwrap().1);
        assert_eq!(report_line(&json!({})), None);
    }
}
