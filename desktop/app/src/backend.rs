//! Starts, watches and stops the bundled Python runtime.
//!
//! The runtime is started as `python3 -m umbrous serve --desktop --port 0` and
//! speaks the protocol in `umbrous/desktop.py`: JSON lines on its stdout,
//! commands on its stdin. Rules kept here:
//!
//! * Only the one child this app spawned is ever signalled, through the
//!   `Child` handle it still owns. A waiter thread blocks in
//!   `waitid(…, WNOWAIT)`, which reports the exit without reaping it; the child
//!   is then reaped under the same lock that guards every kill. So a process
//!   id the OS has already handed to another program is never signalled.
//! * A runtime started from a terminal (`bin/umbrous`) is never touched: the
//!   child reports `already_running` and the window explains what to do.
//! * Port 0: the OS picks a free loopback port, so a busy port cannot block startup.
//! * The child holds the read end of a pipe only this process can write to.
//!   If the app dies, the OS closes it and the runtime stops itself.
//! * The one-time launch code travels over that pipe, never on a command line.
//! * No polling: exits are reported by the blocking waiter, output by blocking reads.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use futures::channel::mpsc::UnboundedSender;
use serde_json::Value;

use crate::logfile;

const STARTUP_TIMEOUT: Duration = Duration::from_secs(45);
const STOP_GRACE: Duration = Duration::from_secs(5);
const TERM_GRACE: Duration = Duration::from_secs(2);
const STDERR_TAIL: usize = 30;

#[derive(Clone, Debug)]
pub struct Failure {
    pub code: String,
    pub title: String,
    pub message: String,
    pub detail: Vec<String>,
}

#[derive(Debug)]
pub enum BackendEvent {
    /// The runtime listens on `origin`; trade `launch_code` for the session credential.
    Ready { origin: String, launch_code: String },
    Failed(Failure),
}

struct Running {
    child: Child,
    stdin: Option<ChildStdin>,
    pid: u32,
    exited: bool,
}

struct Inner {
    running: Option<Running>,
    generation: u64,
    shutting_down: bool,
    ready: bool,
    reported_error: bool,
    stderr_tail: VecDeque<String>,
}

#[derive(Clone)]
pub struct Backend {
    inner: Arc<Mutex<Inner>>,
}

pub struct BackendPaths {
    pub python: PathBuf,
    pub app_dir: PathBuf,
}

impl BackendPaths {
    /// `Umbrous.app/Contents/Resources/backend`. Debug builds run from
    /// `target/` and fall back to the repo's `build/desktop-backend`.
    pub fn locate() -> Result<Self, String> {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut candidates = vec![];
        if let Some(contents) = exe.parent().and_then(|p| p.parent()) {
            candidates.push(contents.join("Resources").join("backend"));
        }
        if cfg!(debug_assertions) || cfg!(feature = "e2e") {
            candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../build/desktop-backend"));
        }
        for root in &candidates {
            let python = root.join("python").join("bin").join("python3");
            let app_dir = root.join("app");
            if python.exists() && app_dir.join("umbrous").join("__main__.py").exists() {
                return Ok(BackendPaths { python, app_dir });
            }
        }
        Err(format!("bundled runtime not found (looked in {})", candidates.iter().map(|p| p.display().to_string()).collect::<Vec<_>>().join(", ")))
    }
}

impl Backend {
    pub fn new() -> Self {
        Backend {
            inner: Arc::new(Mutex::new(Inner {
                running: None,
                generation: 0,
                shutting_down: false,
                ready: false,
                reported_error: false,
                stderr_tail: VecDeque::new(),
            })),
        }
    }

    #[cfg(feature = "e2e")]
    pub fn pid(&self) -> Option<u32> {
        self.inner.lock().unwrap().running.as_ref().map(|r| r.pid)
    }

    /// Start the runtime unless one is running. Events arrive on `events`.
    pub fn start(&self, events: UnboundedSender<BackendEvent>) {
        let mut inner = self.inner.lock().unwrap();
        if inner.running.is_some() || inner.shutting_down {
            return;
        }
        inner.generation += 1;
        let generation = inner.generation;
        inner.ready = false;
        inner.reported_error = false;
        inner.stderr_tail.clear();

        let paths = match BackendPaths::locate() {
            Ok(p) => p,
            Err(msg) => {
                logfile::event("backend.missing", &[]);
                let _ = events.unbounded_send(BackendEvent::Failed(failure(
                    "bundle_incomplete",
                    "Umbrous is incomplete",
                    "The bundled Python runtime is missing from the app. Reinstall Umbrous.",
                    vec![msg],
                )));
                return;
            }
        };

        let mut cmd = Command::new(&paths.python);
        // -E: ignore PYTHON* variables, -s: no user site-packages, -B: never write
        // .pyc files into the signed bundle, -X utf8: UTF-8 I/O. The working
        // directory is the bundled source, which is how `-m umbrous` finds it.
        cmd.args(["-E", "-s", "-B", "-X", "utf8", "-m", "umbrous", "serve", "--desktop", "--port", "0"])
            .current_dir(&paths.app_dir)
            .env_clear()
            .envs(child_env())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let started = Instant::now();
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                logfile::event("backend.spawn_failed", &[("error", e.to_string())]);
                let _ = events.unbounded_send(BackendEvent::Failed(failure(
                    "spawn_failed",
                    "Umbrous could not start its runtime",
                    "The bundled Python runtime did not start.",
                    vec![e.to_string()],
                )));
                return;
            }
        };
        let pid = child.id();
        let stdout = child.stdout.take().expect("piped");
        let stderr = child.stderr.take().expect("piped");
        let stdin = child.stdin.take();
        inner.running = Some(Running { child, stdin, pid, exited: false });
        drop(inner);
        logfile::event("backend.spawned", &[("pid", pid.to_string())]);

        // stdout: the protocol
        let me = self.clone();
        let tx = events.clone();
        thread::Builder::new().name("backend-stdout".into()).spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let Ok(ev) = serde_json::from_str::<Value>(&line) else { continue };
                let s = |k: &str| ev.get(k).and_then(Value::as_str).unwrap_or("").to_string();
                let mut inner = me.inner.lock().unwrap();
                if inner.generation != generation {
                    return;
                }
                match s("event").as_str() {
                    "ready" => {
                        let (origin, code) = (s("origin"), s("launch_code"));
                        if !valid_origin(&origin) || code.is_empty() {
                            continue;
                        }
                        inner.ready = true;
                        drop(inner);
                        let ms = started.elapsed().as_millis();
                        logfile::event("backend.ready", &[("origin", origin.clone()), ("ms", ms.to_string())]);
                        let _ = tx.unbounded_send(BackendEvent::Ready { origin, launch_code: code });
                    }
                    "error" => {
                        inner.reported_error = true;
                        drop(inner);
                        let code = s("code");
                        logfile::event("backend.error", &[("code", code.clone())]);
                        let _ = tx.unbounded_send(BackendEvent::Failed(explain_error(&code, &s("message"), &ev)));
                    }
                    _ => {}
                }
            }
        }).expect("thread");

        // stderr: kept in memory for the error screen only (a traceback could quote a message)
        let me = self.clone();
        thread::Builder::new().name("backend-stderr".into()).spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut inner = me.inner.lock().unwrap();
                if inner.generation != generation {
                    return;
                }
                if inner.stderr_tail.len() == STDERR_TAIL {
                    inner.stderr_tail.pop_front();
                }
                inner.stderr_tail.push_back(line);
            }
        }).expect("thread");

        // exit: blocks in waitid(WNOWAIT) — no polling
        let me = self.clone();
        let tx = events.clone();
        thread::Builder::new().name("backend-wait".into()).spawn(move || {
            wait_exit_nowait(pid);
            let mut inner = me.inner.lock().unwrap();
            if inner.generation != generation {
                return;
            }
            let Some(mut running) = inner.running.take() else { return };
            running.exited = true;
            let status = running.child.wait().ok(); // reaped here, under the lock
            let code = status.and_then(|s| s.code()).map(|c| c.to_string()).unwrap_or_else(|| "signal".into());
            logfile::event("backend.exited", &[("pid", pid.to_string()), ("exit", code.clone())]);
            remove_stale_runtime_file(pid);
            if inner.shutting_down || inner.reported_error {
                return;
            }
            let was_ready = inner.ready;
            let detail = std::iter::once(format!("exit status: {code}")).chain(inner.stderr_tail.iter().cloned()).collect();
            drop(inner);
            let _ = tx.unbounded_send(BackendEvent::Failed(if was_ready {
                failure("crashed", "The Umbrous runtime stopped",
                        "It stopped unexpectedly. Chats are saved after every reply. Press Restart to continue.", detail)
            } else {
                failure("startup_failed", "Umbrous could not start", "The runtime exited before it was ready.", detail)
            }));
        }).expect("thread");

        // startup deadline: one sleeping thread, not a poll
        let me = self.clone();
        thread::Builder::new().name("backend-deadline".into()).spawn(move || {
            thread::sleep(STARTUP_TIMEOUT);
            let mut inner = me.inner.lock().unwrap();
            // Only if nothing else happened: not ready, not reported, not exited, not quitting.
            if inner.generation != generation || inner.ready || inner.shutting_down || inner.reported_error
                || inner.running.is_none()
            {
                return;
            }
            inner.reported_error = true;
            if let Some(r) = inner.running.as_mut() {
                if !r.exited {
                    let _ = r.child.kill(); // still our unreaped child
                }
            }
            let detail = inner.stderr_tail.iter().cloned().collect();
            drop(inner);
            logfile::event("backend.startup_timeout", &[]);
            let _ = events.unbounded_send(BackendEvent::Failed(failure(
                "startup_timeout",
                "Umbrous took too long to start",
                "The runtime did not report that it was ready within 45 seconds and was stopped.",
                detail,
            )));
        }).expect("thread");
    }

    /// Stop the runtime: "stop" + EOF, then SIGTERM after 5 s, then SIGKILL after 2 more.
    /// Blocks until it is gone. Only touches the child this app spawned.
    pub fn shutdown(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.shutting_down = true;
        let Some(running) = inner.running.as_mut() else { return };
        let pid = running.pid;
        if let Some(mut stdin) = running.stdin.take() {
            let _ = stdin.write_all(b"stop\n");
        } // dropped: EOF
        drop(inner);
        let how = if self.wait_gone(STOP_GRACE) {
            "clean"
        } else if self.signal_if_ours(libc::SIGTERM) && self.wait_gone(TERM_GRACE) {
            "sigterm"
        } else {
            self.signal_if_ours(libc::SIGKILL);
            self.wait_gone(Duration::from_secs(2));
            "sigkill"
        };
        logfile::event("backend.stopped", &[("pid", pid.to_string()), ("how", how.into())]);
    }

    /// The waiter thread reaps the child and clears `running`.
    fn wait_gone(&self, limit: Duration) -> bool {
        let until = Instant::now() + limit;
        loop {
            if self.inner.lock().unwrap().running.is_none() {
                return true;
            }
            if Instant::now() >= until {
                return false;
            }
            thread::sleep(Duration::from_millis(20)); // only during quit
        }
    }

    fn signal_if_ours(&self, sig: libc::c_int) -> bool {
        let inner = self.inner.lock().unwrap();
        match inner.running.as_ref() {
            // Not yet reaped (reaping happens under this lock), so the pid is still ours.
            Some(r) => unsafe { libc::kill(r.pid as libc::pid_t, sig) == 0 },
            None => false,
        }
    }
}

/// A runtime that was killed cannot remove its own `run/runtime.json`. Remove it
/// if, and only if, it names the child we just reaped.
fn remove_stale_runtime_file(pid: u32) {
    let Some(home) = logfile::umbrous_home() else { return };
    let path = home.join("run").join("runtime.json");
    let Ok(text) = std::fs::read_to_string(&path) else { return };
    let owner = serde_json::from_str::<Value>(&text).ok().and_then(|v| v.get("pid").and_then(Value::as_u64));
    if owner == Some(pid as u64) {
        let _ = std::fs::remove_file(&path);
    }
}

/// Block until `pid` exits, without reaping it.
fn wait_exit_nowait(pid: u32) {
    loop {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::waitid(libc::P_PID, pid as libc::id_t, &mut info, libc::WEXITED | libc::WNOWAIT) };
        if rc == 0 {
            return;
        }
        if std::io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            return;
        }
    }
}

fn failure(code: &str, title: &str, message: &str, detail: Vec<String>) -> Failure {
    Failure { code: code.into(), title: title.into(), message: message.into(), detail }
}

fn explain_error(code: &str, message: &str, ev: &Value) -> Failure {
    match code {
        "already_running" => {
            let pid = ev.get("pid").and_then(Value::as_i64).unwrap_or(0);
            let how = if ev.get("mode").and_then(Value::as_str) == Some("desktop") {
                "Another copy of the Umbrous app is using your data. Quit it first."
            } else {
                "It was started from a terminal (bin/umbrous). Stop it there with Ctrl-C, then press Retry. Umbrous never stops a runtime it did not start."
            };
            failure("already_running", "Umbrous is already running", how, vec![format!("process id {pid}")])
        }
        "port_in_use" => failure("port_in_use", "No free local port", "Umbrous could not listen on 127.0.0.1.", vec![message.into()]),
        "config_error" => failure("config_error", "Umbrous settings are invalid",
                                  "Fix or remove config.json in the Umbrous data folder, then press Retry.", vec![message.into()]),
        "database_error" => failure("database_error", "Umbrous could not open its database",
                                    "Your data was not changed. If a newer Umbrous used this data, use that version.", vec![message.into()]),
        _ => failure("startup_failed", "Umbrous could not start", "The runtime reported an error.", vec![message.into()]),
    }
}

fn valid_origin(origin: &str) -> bool {
    let Some(rest) = origin.strip_prefix("http://127.0.0.1:") else { return false };
    rest.parse::<u16>().map(|p| p > 0).unwrap_or(false)
}

/// A small explicit environment: only the variables the runtime documents.
fn child_env() -> Vec<(String, String)> {
    let mut env = vec![
        ("PATH".to_string(), "/usr/bin:/bin:/usr/sbin:/sbin".to_string()),
        ("LANG".to_string(), "en_US.UTF-8".to_string()),
    ];
    for key in ["HOME", "USER", "LOGNAME", "TMPDIR", "UMBROUS_HOME", "OLLAMA_HOST"] {
        if let Ok(v) = std::env::var(key) {
            env.push((key.to_string(), v));
        }
    }
    env
}
