//! The Umbrous window: sidebar (projects, chats), transcript, composer, details.
//!
//! All network and parsing work runs off the UI thread (`background_spawn` or a
//! dedicated thread for a streaming reply). The UI thread only applies results.
//! Nothing here polls on a timer: Ollama and runtime state are refreshed when
//! the window becomes active, when you open the model menu, or press Retry.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::channel::mpsc;
use futures::StreamExt;
use gpui::{
    div, list, prelude::*, px, AnyElement, App, ClickEvent, ClipboardItem, Context, Entity, FocusHandle, Focusable,
    FontWeight, ListAlignment, ListState, MouseButton, SharedString, Subscription, Window,
};
use serde_json::{json, Value};

use crate::actions::*;
use crate::api::{self, Api};
use crate::backend::{Backend, BackendEvent, Failure};
use crate::composer::{Composer, ComposerEvent};
use crate::logfile;
use crate::markdown;
use crate::theme;
use crate::ui;
use crate::transcript::{self, MetaKind, Msg, Pos, Role, Row, Selection, SelectionHost};

#[path = "model_controls.rs"]
mod model_controls;
#[path = "access_controls.rs"]
mod access_controls;
pub use access_controls::PendingApproval;
#[path = "deletion.rs"]
mod deletion;
pub use deletion::DeleteDialog;
/// Every workspace profile a chat's access may be (the e2e driver names them).
pub fn access_levels() -> &'static [&'static str] { &["workspace_read", "ask_before_read", "code_ask_commands", "workspace_code"] }

const STREAM_BATCH: Duration = Duration::from_millis(33);
/// At most this many frames of render-time correction after one reveal_end.
const REVEAL_FRAMES: u8 = 10;

pub enum Phase {
    Starting,
    Failed(Failure),
    Ready,
}

/// An interrupted task of the open chat that can be resumed (M5).
#[derive(Clone, Debug)]
pub struct Interrupted {
    pub task_id: String,
    /// the message that was waiting when the runtime stopped
    pub text: String,
    /// the recovered report, in one line
    pub report: String,
    /// what resuming does (the runtime's words)
    pub note: String,
}

struct Run {
    /// unique per send; stream events carry it, and only the current run's are applied
    token: u64,
    /// index of the reply message in `msgs` (valid while `view` is unchanged)
    msg_ix: usize,
    chat_id: String,
    run_id: Option<String>,
    stop: Arc<AtomicBool>,
    /// M9: set once Stop has asked the runtime to cancel this run (it knew the
    /// run id), so the stream is read on to the runtime's last event
    cancel_sent: Arc<AtomicBool>,
    /// M6: the memory items this reply was given (from `start`), so a reply
    /// that is stopped or fails part way still names them
    memory_ids: Vec<String>,
}

/// An offscreen stream stays connected. Its events are replayed on return;
/// completed streams are loaded from persisted history instead.
struct BackgroundRun {
    run: Run,
    chat: Option<api::ChatInfo>,
    msgs: Vec<Msg>,
    context: Option<(u64, u64)>,
    approval: Option<PendingApproval>,
    notice: Option<(String, MetaKind, bool)>,
    load_access: bool,
    events: Vec<StreamMsg>,
}

/// What the Memory section's inline field is for (M6).
#[derive(Clone, Debug, PartialEq)]
pub enum MemTarget {
    Add(&'static str), // "personal" | "project"
    Edit(String),      // a memory id
    Accept(String),    // M7: a suggestion's id: edit its text, Enter accepts it
}

/// M8: Start fresh, in the right panel while it is open. The choice is what
/// you ticked; a preview is the runtime's exact list for that choice, and any
/// change to the choice drops it, so only a preview you saw can be confirmed.
#[derive(Debug, Clone, Default)]
pub struct ResetUi {
    pub choice: api::ResetChoice,
    /// the project's chats, active and archived, loaded when Start fresh opens
    pub chats: Option<Vec<api::ChatInfo>>,
    pub preview: Option<api::ResetPreview>,
    /// the runtime refused a preview that no longer matched; this one is new
    pub warning: Option<String>,
    pub error: Option<String>,
    pub done: Option<api::ResetDone>,
    pub working: bool,
    /// Options › Delete chat…: preview as soon as the chat list is in
    preview_on_load: bool,
}

/// One tick box in Start fresh.
#[derive(Clone, Debug)]
pub enum ResetTick {
    AllChats,
    Chat(String),
    AllPersonal,
    Personal(String),
    AllProject,
    Project(String),
    StartFresh,
}

enum StreamMsg {
    Event(Value),
    Ended(Option<api::ApiError>),
}

pub struct Workspace {
    pub backend: Backend,
    backend_tx: mpsc::UnboundedSender<BackendEvent>,
    pub phase: Phase,
    pub api: Option<Api>,
    health: Option<api::Health>,
    pub projects: Vec<api::Project>,
    pub project_id: Option<String>,
    pub chats: Vec<api::ChatInfo>,
    archived: Vec<api::ChatInfo>,
    show_archived: bool,
    chat_menu: Option<String>,
    project_menu: Option<String>,
    pub deletion: Option<DeleteDialog>,
    deletion_seq: u64,
    settings_open: bool,
    settings_memory: bool,
    settings_archived: Option<Vec<api::ChatInfo>>,
    settings_error: Option<String>,
    settings_seq: u64,
    pub chat: Option<api::ChatInfo>,
    pub msgs: Vec<Msg>,
    pub list: ListState,
    /// keep the newest text in view while a reply streams; off once you scroll up
    follow: std::rc::Rc<std::cell::Cell<bool>>,
    /// Frames left in which render may scroll again to bring the last row's bottom
    /// into view (0 = off). Only our own reveal arms it; any scroll input clears it.
    reveal_frames: std::rc::Rc<std::cell::Cell<u8>>,
    /// list rows (see transcript::Row) and each message's row range
    rows: Vec<Row>,
    msg_rows: Vec<std::ops::Range<usize>>,
    selection: Selection,
    pub models: Option<api::Models>,
    pub model: Option<String>,
    model_menu: bool,
    /// the composer's access menu (access_controls.rs)
    access_menu: bool,
    /// the gate is waiting for your answer to this action (shown above the composer)
    pub approval: Option<PendingApproval>,
    /// the model whose tool check is running
    probe_busy: Option<String>,
    /// the access chosen for a chat that does not exist yet (sent when it is created)
    pending_access: Option<String>,
    /// the "Enable Full access?" dialog, while open
    pub full_access_confirm: Option<access_controls::FullAccessConfirm>,
    model_preferences: model_controls::Preferences,
    model_preferences_loaded: bool,
    codex_status: api::CodexStatus,
    codex_login_busy: bool,
    options_menu: bool,
    pub composer: Entity<Composer>,
    project_input: Option<Entity<Composer>>,
    project_error: Option<String>,
    run: Option<Run>,
    background_runs: Vec<BackgroundRun>,
    unread_results: std::collections::HashMap<String, bool>,
    pub(crate) notice: Option<(String, MetaKind, bool)>, // text, kind, offers Retry (Ollama)
    /// M5: the open chat's last task, if the runtime was stopped in the middle
    /// of it and it can be resumed: shown as a notice with a Resume button.
    pub interrupted: Option<Interrupted>,
    /// M9: a reply attached a folder ("work on <folder>"): reload Access when it ends
    load_access_after_stream: bool,
    /// The selected project's workspace access (M3); the composer's access menu
    /// shows and changes its profile, and approval cards answer what it asks.
    pub access: Option<api::Access>,
    access_seq: u64,
    pub access_error: Option<String>,
    /// M6: the selected project's memory view (personal memory and this
    /// project's memory). Add, edit and delete go to the runtime; this is
    /// only what it last returned.
    pub memory: Option<api::MemoryView>,
    memory_seq: u64,
    pub memory_error: Option<String>,
    /// the inline field for adding or editing a memory item, and what it is for
    memory_input: Option<(Entity<Composer>, MemTarget)>,
    /// a memory item whose delete button was pressed once (press again to delete)
    memory_armed: Option<String>,
    /// send replies with memory (the default) or without, to compare
    pub use_memory: bool,
    /// M8: Start fresh (selective reset), while open
    pub reset: Option<ResetUi>,
    /// Start fresh's generation: bumped on open, close, every tick change and
    /// every preview. It lives here, not in ResetUi, so a reply to a panel that
    /// was closed (and maybe reopened for another chat) is never applied.
    reset_seq: u64,
    /// The panel's lifetime: bumped only on open, close and project switch.
    /// The chat list and a confirm's result check this one, so a tick change
    /// while the list loads does not throw the list away.
    reset_panel: u64,
    /// e2e only: hold each preview request this long, to test late replies
    pub e2e_preview_delay_ms: u64,
    /// e2e only: hold Start fresh's chat-list request this long
    pub e2e_list_delay_ms: u64,
    /// e2e only: hold a folder attach this long, to test a late reply after New chat
    pub e2e_attach_delay_ms: u64,
    sidebar: bool,
    /// the right sidebar (Changes and Sources), shown and hidden by the header's panel button
    details: bool,
    card_menu: bool,
    projects_open: bool,
    sessions_open: bool,
    /// estimated tokens and window of the last reply's context (Details ▸ Context)
    context: Option<(u64, u64)>,
    focus: FocusHandle,
    parse_in_flight: bool,
    loading_chat: bool,
    /// Bumped whenever the transcript is replaced (new chat, open chat, other project).
    /// Every stream, parse and transcript response remembers the view it was made
    /// for and is dropped if the view has changed since: this is what keeps a
    /// late reply from writing into another chat.
    view: u64,
    /// The chat a still-loading `open_chat` is for (a second click is ignored).
    opening: Option<String>,
    run_seq: u64,
    /// newest request per list; older responses are dropped
    projects_seq: u64,
    chats_seq: u64,
    models_seq: u64,
    pub started: Instant,
    logged_first_frame: bool,
    window_active: bool,
    pub perf: Perf,
    _subs: Vec<Subscription>,
}

/// Counters for the responsiveness measurements (docs/desktop.md).
#[derive(Default, Clone, Debug)]
pub struct Perf {
    pub stream_batches: u64,
    pub stream_events: u64,
    pub parse_runs: u64,
    pub parse_ms_total: f64,
    pub parse_ms_max: f64,
    pub apply_ms_max: f64,
    pub render_count: u64,
    pub render_ms_total: f64,
    pub render_ms_max: f64,
    pub open_chat_ms: Option<u128>,
    pub first_ready_ms: Option<u128>,
}

impl Focusable for Workspace {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus.clone()
    }
}

impl SelectionHost for Workspace {
    fn selection_down(&mut self, pos: Pos, clicks: usize, shift: bool, cx: &mut Context<Self>) {
        if shift && self.selection.anchor.is_some() {
            self.selection.head = Some(pos);
        } else if clicks == 2 {
            let text = self.msgs.get(pos.msg).and_then(|m| m.blocks.get(pos.block)).map(|b| b.text.clone()).unwrap_or_default();
            let w = transcript::word_at(&text, pos.offset);
            self.selection.anchor = Some(Pos { offset: w.start, ..pos });
            self.selection.head = Some(Pos { offset: w.end, ..pos });
        } else if clicks >= 3 {
            let len = self.msgs.get(pos.msg).and_then(|m| m.blocks.get(pos.block)).map(|b| b.text.len()).unwrap_or(0);
            self.selection.anchor = Some(Pos { offset: 0, ..pos });
            self.selection.head = Some(Pos { offset: len, ..pos });
        } else {
            self.selection.anchor = Some(pos);
            self.selection.head = Some(pos);
        }
        self.selection.dragging = clicks == 1;
        cx.notify();
    }
    fn selection_drag(&mut self, pos: Pos, cx: &mut Context<Self>) {
        if self.selection.dragging && self.selection.head != Some(pos) {
            self.selection.head = Some(pos);
            cx.notify();
        }
    }
}

impl Workspace {
    /// `backend` was already started by main() so Python boots while GPUI sets up the window.
    pub fn new(
        backend: Backend,
        tx: mpsc::UnboundedSender<BackendEvent>,
        mut rx: mpsc::UnboundedReceiver<BackendEvent>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let composer = cx.new(Composer::new);
        let mut subs = vec![cx.subscribe_in(&composer, window, |this, _, ev: &ComposerEvent, window, cx| match ev {
            ComposerEvent::Submit(text) => this.send(text.clone(), window, cx),
        })];
        subs.push(cx.observe_window_activation(window, |this, window, cx| {
            this.window_active = window.is_window_active();
            if window.is_window_active() && matches!(this.phase, Phase::Ready) {
                this.load_models(false, cx); // Ollama may have started or stopped meanwhile
            }
        }));
        // Messages start at the top like the page's thread; `follow` keeps the
        // end in view while new text arrives, until you scroll away from it.
        let list = ListState::new(0, ListAlignment::Top, px(800.));
        let follow = std::rc::Rc::new(std::cell::Cell::new(true));
        let reveal_frames = std::rc::Rc::new(std::cell::Cell::new(0u8));
        let (f, r) = (follow.clone(), reveal_frames.clone());
        list.set_scroll_handler(move |ev, _, _| {
            r.set(0); // a scroll gesture always wins over a pending reveal
            f.set(ev.visible_range.end >= ev.count)
        });
        cx.spawn(async move |this, cx| {
            while let Some(ev) = rx.next().await {
                if this.update(cx, |this, cx| this.on_backend(ev, cx)).is_err() {
                    break;
                }
            }
        })
        .detach();
        Workspace {
            backend,
            backend_tx: tx,
            phase: Phase::Starting,
            api: None,
            health: None,
            projects: vec![],
            project_id: None,
            chats: vec![],
            archived: vec![],
            show_archived: false,
            chat_menu: None,
            project_menu: None,
            deletion: None,
            deletion_seq: 0,
            settings_open: false,
            settings_memory: false,
            settings_archived: None,
            settings_error: None,
            settings_seq: 0,
            chat: None,
            msgs: vec![],
            list,
            follow,
            reveal_frames,
            rows: vec![],
            msg_rows: vec![],
            selection: Selection::default(),
            models: None,
            model: None,
            model_menu: false,
            access_menu: false,
            approval: None,
            probe_busy: None,
            pending_access: None,
            full_access_confirm: None,
            model_preferences: model_controls::Preferences::default(),
            model_preferences_loaded: false,
            codex_status: api::CodexStatus::default(),
            codex_login_busy: false,
            options_menu: false,
            composer,
            project_input: None,
            project_error: None,
            run: None,
            background_runs: Vec::new(),
            unread_results: std::collections::HashMap::new(),
            notice: None,
            interrupted: None,
            load_access_after_stream: false,
            access: None,
            memory: None,
            memory_seq: 0,
            memory_error: None,
            memory_input: None,
            memory_armed: None,
            use_memory: true,
            reset: None,
            reset_seq: 0,
            reset_panel: 0,
            e2e_preview_delay_ms: 0,
            e2e_list_delay_ms: 0,
            e2e_attach_delay_ms: 0,
            access_seq: 0,
            access_error: None,
            sidebar: true,
            details: false,
            card_menu: false,
            projects_open: true,
            sessions_open: true,
            context: None,
            focus: cx.focus_handle(),
            parse_in_flight: false,
            loading_chat: false,
            view: 0,
            opening: None,
            run_seq: 0,
            projects_seq: 0,
            chats_seq: 0,
            models_seq: 0,
            started: Instant::now(),
            logged_first_frame: false,
            window_active: true,
            perf: Perf::default(),
            _subs: subs,
        }
    }

    // ---------- backend ----------
    fn on_backend(&mut self, ev: BackendEvent, cx: &mut Context<Self>) {
        match ev {
            BackendEvent::Failed(f) => {
                self.phase = Phase::Failed(f);
                self.api = None;
                self.run = None;
                self.background_runs.clear();
                cx.notify();
            }
            BackendEvent::Ready { origin, launch_code, .. } => {
                let task = cx.background_spawn(async move {
                    let api = Api::claim(&origin, &launch_code)?;
                    let health: api::Health = api.get("/api/health")?;
                    Ok::<_, api::ApiError>((api, health))
                });
                cx.spawn(async move |this, cx| {
                    let res = task.await;
                    this.update(cx, |this, cx| match res {
                        Ok((api, health)) => {
                            this.api = Some(api);
                            this.health = Some(health);
                            this.phase = Phase::Ready;
                            this.perf.first_ready_ms.get_or_insert(this.started.elapsed().as_millis());
                            logfile::event("ui.connected", &[("ms_since_start", this.started.elapsed().as_millis().to_string())]);
                            this.load_projects(None, cx);
                            this.load_models(false, cx);
                            cx.notify();
                        }
                        Err(e) => {
                            this.phase = Phase::Failed(Failure {
                                code: "claim_failed".into(),
                                title: "Umbrous could not connect to its runtime".into(),
                                message: e.message,
                                detail: vec![],
                            });
                            cx.notify();
                        }
                    })
                    .ok();
                })
                .detach();
            }
        }
    }

    pub fn retry(&mut self, cx: &mut Context<Self>) {
        logfile::event("backend.retry", &[]);
        self.phase = Phase::Starting;
        self.backend.start(self.backend_tx.clone());
        cx.notify();
    }

    fn api_call<R: Send + 'static>(
        &self,
        cx: &mut Context<Self>,
        work: impl FnOnce(&Api) -> api::Result<R> + Send + 'static,
        done: impl FnOnce(&mut Self, api::Result<R>, &mut Context<Self>) + 'static,
    ) {
        let Some(api) = self.api.clone() else { return };
        let task = cx.background_spawn(async move { work(&api) });
        cx.spawn(async move |this, cx| {
            let r = task.await;
            this.update(cx, |this, cx| {
                done(this, r, cx);
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    // ---------- projects and chats ----------
    pub fn load_projects(&mut self, select: Option<String>, cx: &mut Context<Self>) {
        self.projects_seq += 1;
        let seq = self.projects_seq;
        self.api_call(cx, |api| api.get::<api::Projects>("/api/projects"), move |this, r, cx| {
            if seq != this.projects_seq {
                return; // a newer request is on its way
            }
            if let Ok(p) = r {
                this.projects = p.projects;
                let keep = select.or(this.project_id.clone()).filter(|id| this.projects.iter().any(|p| &p.id == id));
                let id = keep.or_else(|| this.projects.iter().find(|p| p.id == "prj_general").or(this.projects.first()).map(|p| p.id.clone()));
                if this.project_id != id {
                    this.new_chat(cx);
                    this.project_id = id;
                    this.chats.clear();
                    this.archived.clear();
                    this.access = None;
                    this.memory = None;
                    this.access_seq += 1;
                    this.memory_seq += 1;
                }
                this.load_chats(cx);
            }
        });
    }

    pub fn load_chats(&mut self, cx: &mut Context<Self>) {
        self.load_memory(cx);
        self.chats_seq += 1;
        let Some(pid) = self.project_id.clone() else { return };
        self.load_access(cx);
        let show_archived = self.show_archived;
        let seq = self.chats_seq;
        let for_project = pid.clone();
        self.api_call(
            cx,
            move |api| {
                let active: api::Chats = api.get(&format!("/api/chats?project_id={pid}"))?;
                let archived = if show_archived {
                    api.get::<api::Chats>(&format!("/api/chats?project_id={pid}&archived=1"))?.chats
                } else {
                    vec![]
                };
                Ok((active.chats, archived))
            },
            move |this, r, _| {
                // drop it if another project was selected or a newer list was asked for
                if seq != this.chats_seq || this.project_id.as_deref() != Some(for_project.as_str()) {
                    return;
                }
                if let Ok((a, b)) = r {
                    this.chats = a;
                    this.archived = b;
                    this.merge_running_chats();
                    // Keep the open chat's title in step with the merged list.
                    if let Some(cur) = this.chat.as_mut() {
                        if let Some(fresh) = this.chats.iter().chain(this.archived.iter()).find(|c| c.id == cur.id) {
                            *cur = fresh.clone();
                        }
                    }
                }
            },
        );
    }

    /// The selected project's access (workspace and profile). Asked again whenever
    /// the chat list is, so a change made in the browser page or the CLI shows up.
    pub fn load_access(&mut self, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        self.access_seq += 1;
        let seq = self.access_seq;
        let path = format!("/api/projects/{pid}/access");
        self.api_call(cx, move |api| api.get::<api::Access>(&path), move |this, r, _| {
            if seq != this.access_seq || this.project_id.as_deref() != Some(pid.as_str()) {
                return;
            }
            if let Ok(a) = r {
                this.access = Some(a);
            }
        });
    }

    // ---------- memory (M6) ----------
    /// Personal memory and the selected project's memory, asked again whenever
    /// the chat list is, so a change made in the browser or the CLI shows up.
    pub fn load_memory(&mut self, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        self.memory_seq += 1;
        let seq = self.memory_seq;
        let path = format!("/api/memory?project_id={pid}");
        self.api_call(cx, move |api| api.get::<api::MemoryView>(&path), move |this, r, _| {
            if seq != this.memory_seq || this.project_id.as_deref() != Some(pid.as_str()) {
                return;
            }
            match r {
                Ok(v) => this.memory = Some(v),
                Err(e) => this.memory_error = Some(e.message),
            }
        });
    }

    /// The memory item with this id, in either list.
    pub fn memory_item(&self, id: &str) -> Option<&api::MemoryItem> {
        let v = self.memory.as_ref()?;
        v.personal.iter().chain(v.project.iter()).find(|m| m.id == id)
    }

    fn memory_done(&mut self, r: Result<Value, api::ApiError>, cx: &mut Context<Self>) {
        match r {
            Ok(_) => {
                self.memory_input = None;
                self.memory_error = None;
                self.memory_armed = None;
                self.load_memory(cx);
            }
            Err(e) => self.memory_error = Some(e.message),
        }
    }

    /// Add an item: personal (a fact, for every project) or this project's (a decision).
    /// The kind and a narrower scope are set in the browser page or the CLI.
    pub fn add_memory(&mut self, store: &'static str, text: String, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        logfile::event("ui.memory_add", &[("store", store.into())]); // never the text
        let body = if store == "personal" {
            json!({"store": "personal", "text": text, "kind": "fact"})
        } else {
            json!({"store": "project", "project_id": pid, "text": text, "kind": "decision"})
        };
        self.api_call(cx, move |api| api.post::<Value>("/api/memory", body), |this, r, cx| this.memory_done(r, cx));
    }

    pub fn edit_memory(&mut self, id: String, text: String, cx: &mut Context<Self>) {
        logfile::event("ui.memory_edit", &[("id", id.clone())]);
        let path = format!("/api/memory/{id}");
        self.api_call(cx, move |api| api.post::<Value>(&path, json!({"text": text})), |this, r, cx| this.memory_done(r, cx));
    }

    /// Delete an item. Chats that already used it are not changed.
    pub fn delete_memory(&mut self, id: String, cx: &mut Context<Self>) {
        logfile::event("ui.memory_delete", &[("id", id.clone())]);
        let path = format!("/api/memory/{id}/delete");
        self.api_call(cx, move |api| api.post::<Value>(&path, json!({})), |this, r, cx| this.memory_done(r, cx));
    }

    // ---------- suggestions to review (M7) ----------
    /// The suggestion with this id, waiting in the selected project.
    pub fn candidate(&self, id: &str) -> Option<&api::Candidate> {
        self.memory.as_ref()?.candidates.iter().find(|c| c.id == id)
    }

    /// Accept a suggestion: the runtime checks it again and writes it to the
    /// store it names. `text`: your edited text, or None to keep it.
    pub fn accept_candidate(&mut self, id: String, text: Option<String>, cx: &mut Context<Self>) {
        logfile::event("ui.candidate_accept", &[("id", id.clone()), ("edited", text.is_some().to_string())]);
        let path = format!("/api/memory/candidates/{id}/accept");
        let body = match text { Some(t) => json!({"text": t}), None => json!({}) };
        self.api_call(cx, move |api| api.post::<Value>(&path, body), |this, r, cx| this.memory_done(r, cx));
    }

    /// Reject a suggestion. It was never in memory; its text is not kept.
    pub fn reject_candidate(&mut self, id: String, cx: &mut Context<Self>) {
        logfile::event("ui.candidate_reject", &[("id", id.clone())]);
        let path = format!("/api/memory/candidates/{id}/reject");
        self.api_call(cx, move |api| api.post::<Value>(&path, json!({})), |this, r, cx| this.memory_done(r, cx));
    }

    // ---------- start fresh (M8) ----------
    /// Open Start fresh for the selected project. `chat`: Options › Delete chat…
    /// on the open chat, which ticks it and asks for the preview at once.
    pub fn open_reset(&mut self, chat: Option<String>, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        self.open_reset_in_project(chat, pid, cx);
    }

    fn open_reset_in_project(&mut self, chat: Option<String>, pid: String, cx: &mut Context<Self>) {
        self.chat_menu = None;
        self.options_menu = false;
        self.details = true;
        let mut ui = ResetUi { preview_on_load: chat.is_some(), ..Default::default() };
        ui.choice.project_id = pid.clone();
        ui.choice.start_fresh = chat.is_none();
        if let Some(c) = chat {
            ui.choice.chats.insert(c);
        }
        self.reset = Some(ui);
        self.reset_seq += 1;
        self.reset_panel += 1;
        let panel = self.reset_panel;
        let delay = self.e2e_list_delay_ms;
        logfile::event("ui.reset_open", &[]);
        self.load_memory(cx);
        let for_project = pid.clone();
        self.api_call(
            cx,
            move |api| {
                if delay > 0 {
                    std::thread::sleep(std::time::Duration::from_millis(delay));
                }
                let mut all = api.get::<api::Chats>(&format!("/api/chats?project_id={pid}"))?.chats;
                all.extend(api.get::<api::Chats>(&format!("/api/chats?project_id={pid}&archived=1"))?.chats);
                Ok(all)
            },
            move |this, r, cx| {
                if this.reset_panel != panel {
                    return; // closed, or opened again, since
                }
                let Some(ui) = this.reset.as_mut() else { return };
                if ui.choice.project_id != for_project {
                    return;
                }
                match r {
                    Ok(all) => {
                        ui.chats = Some(all);
                        if std::mem::take(&mut ui.preview_on_load) {
                            this.preview_reset(cx);
                        }
                    }
                    Err(e) => ui.error = Some(e.message),
                }
            },
        );
        cx.notify();
    }

    /// Cancel: close it. Nothing was sent that could delete anything.
    pub fn close_reset(&mut self, cx: &mut Context<Self>) {
        self.reset_seq += 1;
        self.reset_panel += 1;
        if self.reset.take().is_some() {
            logfile::event("ui.reset_cancel", &[]);
        }
        cx.notify();
    }

    pub fn toggle_reset(&mut self, tick: ResetTick, cx: &mut Context<Self>) {
        self.reset_seq += 1;
        let Some(ui) = self.reset.as_mut() else { return };
        let c = &mut ui.choice;
        fn flip(set: &mut std::collections::BTreeSet<String>, id: String) {
            if !set.remove(&id) {
                set.insert(id);
            }
        }
        match tick {
            ResetTick::AllChats => c.all_chats = !c.all_chats,
            ResetTick::Chat(id) => flip(&mut c.chats, id),
            ResetTick::AllPersonal => c.all_personal = !c.all_personal,
            ResetTick::Personal(id) => flip(&mut c.personal, id),
            ResetTick::AllProject => c.all_project = !c.all_project,
            ResetTick::Project(id) => flip(&mut c.project, id),
            ResetTick::StartFresh => c.start_fresh = !c.start_fresh,
        }
        // a different choice needs its own preview; one still loading is dropped
        ui.working = false;
        ui.preview = None;
        ui.warning = None;
        ui.error = None;
        ui.done = None;
        cx.notify();
    }

    /// Back to the ticks: the preview is dropped, so Delete needs a new one.
    pub fn change_reset_selection(&mut self, cx: &mut Context<Self>) {
        let Some(ui) = self.reset.as_mut() else { return };
        if ui.working {
            return;
        }
        self.reset_seq += 1;
        ui.preview = None;
        ui.warning = None;
        ui.error = None;
        cx.notify();
    }

    /// Ask the runtime for the exact items this choice covers, and what stays.
    pub fn preview_reset(&mut self, cx: &mut Context<Self>) {
        let Some(ui) = self.reset.as_mut() else { return };
        if ui.choice.is_empty() {
            ui.error = Some("Nothing is selected. Tick chats or memory items first.".into());
            cx.notify();
            return;
        }
        ui.working = true;
        ui.error = None;
        ui.warning = None;
        ui.done = None;
        let sel = ui.choice.selection();
        self.reset_seq += 1;
        let asked = self.reset_seq;
        let delay = self.e2e_preview_delay_ms;
        self.api_call(cx, move |api| {
                if delay > 0 {
                    std::thread::sleep(std::time::Duration::from_millis(delay));
                }
                api.post::<api::ResetPreview>("/api/reset/preview", json!({"selection": sel}))
            },
            move |this, r, _| {
                if this.reset_seq != asked {
                    return; // closed, reopened or re-ticked while it loaded: not for what is shown
                }
                let Some(ui) = this.reset.as_mut() else { return };
                ui.working = false;
                match r {
                    Ok(p) => ui.preview = Some(p),
                    Err(e) => ui.error = Some(e.message),
                }
            });
        cx.notify();
    }

    /// Delete exactly the preview shown. The runtime refuses one that no longer
    /// matches and sends the new one, which is shown to check instead.
    pub fn confirm_reset(&mut self, cx: &mut Context<Self>) {
        let Some(ui) = self.reset.as_mut() else { return };
        let Some(preview) = ui.preview.as_ref() else { return };
        if ui.working {
            return;
        }
        ui.working = true;
        let body = json!({"selection": ui.choice.selection(), "preview_id": preview.preview_id});
        logfile::event("ui.reset_confirm", &[("items", preview.items.len().to_string())]); // ids stay in the runtime log
        let panel = self.reset_panel;
        self.api_call(cx, move |api| api.post::<api::ResetDone>("/api/reset", body), move |this, r, cx| {
            if this.reset_panel != panel || this.reset.is_none() {
                // the panel was closed (or reopened) meanwhile: show nothing in
                // it, but what was deleted must still leave the lists
                if let Ok(done) = &r {
                    if this.chat.as_ref().map(|c| done.items.iter().any(|i| i.id == c.id)).unwrap_or(false) {
                        this.new_chat(cx);
                    }
                    this.load_projects(None, cx);
                    if this.settings_open { this.load_settings_archived(cx); }
                }
                return;
            }
            let Some(ui) = this.reset.as_mut() else { return };
            ui.working = false;
            match r {
                Ok(done) => {
                    let gone_open = this.chat.as_ref().map(|c| done.items.iter().any(|i| i.id == c.id)).unwrap_or(false);
                    let fresh = done.start_fresh;
                    ui.preview = None;
                    ui.choice = api::ResetChoice { project_id: ui.choice.project_id.clone(), start_fresh: true, ..Default::default() };
                    ui.chats = None;
                    ui.done = Some(done);
                    if fresh || gone_open {
                        this.new_chat(cx);
                    }
                    this.load_projects(None, cx);
                    if this.settings_open { this.load_settings_archived(cx); }
                }
                Err(e) => match e.preview.and_then(|p| serde_json::from_value::<api::ResetPreview>(p).ok()) {
                    Some(p) => {
                        ui.preview = Some(p);
                        ui.warning = Some(e.message);
                    }
                    None => ui.error = Some(e.message),
                },
            }
        });
        cx.notify();
    }

    fn begin_memory_input(&mut self, target: MemTarget, window: &mut Window, cx: &mut Context<Self>) {
        let initial = match &target {
            MemTarget::Edit(id) => self.memory_item(id).map(|m| m.text.clone()).unwrap_or_default(),
            MemTarget::Accept(id) => self.candidate(id).map(|c| c.text.clone()).unwrap_or_default(),
            MemTarget::Add(_) => String::new(),
        };
        let placeholder = match &target {
            MemTarget::Add(s) if *s == "personal" => "A fact about you, then Enter",
            MemTarget::Add(_) => "A decision for this project, then Enter",
            MemTarget::Edit(_) => "Edit, then Enter",
            MemTarget::Accept(_) => "Edit, then Enter to accept",
        };
        let input = cx.new(|cx| {
            let mut c = Composer::new(cx);
            c.set_placeholder(placeholder, cx);
            c.set_compact(cx);
            if !initial.is_empty() {
                c.set_text(&initial, cx);
            }
            c
        });
        let t = target.clone();
        let sub = cx.subscribe_in(&input, window, move |this, _, ev: &ComposerEvent, _window, cx| match ev {
            ComposerEvent::Submit(text) => match &t {
                MemTarget::Add(store) => this.add_memory(*store, text.clone(), cx),
                MemTarget::Edit(id) => this.edit_memory(id.clone(), text.clone(), cx),
                MemTarget::Accept(id) => this.accept_candidate(id.clone(), Some(text.clone()), cx),
            },
        });
        self._subs.push(sub);
        window.focus(&input.focus_handle(cx));
        self.memory_input = Some((input, target));
        self.memory_error = None;
        self.memory_armed = None;
        cx.notify();
    }

    pub fn select_project(&mut self, id: String, cx: &mut Context<Self>) {
        if self.project_id.as_deref() != Some(&id) {
            self.project_id = Some(id);
            self.chat_menu = None;
            self.show_archived = false;
            self.access = None;
            self.access_error = None;
            self.memory = None;
            self.memory_error = None;
            self.memory_input = None;
            self.memory_armed = None;
            self.reset = None;
            self.reset_seq += 1;
            self.reset_panel += 1;
            self.new_chat(cx);
            self.load_chats(cx);
        }
    }

    /// Replace the visible transcript while keeping its stream alive offscreen.
    /// Invalidate pending parse/transcript responses for the old view.
    fn begin_view(&mut self, cx: &mut Context<Self>) -> u64 {
        self.detach_run(cx);
        self.rebuild_rows();
        self.view += 1;
        self.parse_in_flight = false;
        self.opening = None;
        self.loading_chat = false;
        self.selection = Selection::default();
        self.options_menu = false;
        self.view
    }

    fn detach_run(&mut self, _cx: &mut Context<Self>) {
        if let Some(run) = self.run.take() {
            self.background_runs.push(BackgroundRun {
                run, chat: self.chat.take(), msgs: std::mem::take(&mut self.msgs),
                context: self.context.take(), approval: self.approval.take(), notice: self.notice.take(),
                load_access: std::mem::take(&mut self.load_access_after_stream), events: Vec::new(),
            });
        } else {
            self.approval = None;
        }
    }

    fn background_events(&mut self, token: u64, batch: Vec<StreamMsg>, ended: bool, cx: &mut Context<Self>) {
        let Some(index) = self.background_runs.iter().position(|b| b.run.token == token) else { return };
        let background = &mut self.background_runs[index];
        let mut created = false;
        for event in &batch {
            if let StreamMsg::Event(ev) = event {
                if let Some(id) = ev.get("run_id").and_then(Value::as_str) {
                    background.run.run_id = Some(id.into());
                }
                match ev.get("type").and_then(Value::as_str) {
                    Some("approval_required") => background.approval = PendingApproval::from_event(ev),
                    Some("tool_result") => background.approval = None,
                    _ => {}
                }
                if ev.get("type").and_then(Value::as_str) == Some("chat_created") {
                    if let Ok(mut chat) = serde_json::from_value::<api::ChatInfo>(ev["chat"].clone()) {
                        if let Some(user) = background.msgs.get(background.run.msg_ix.saturating_sub(1)) {
                            chat.title = user.text.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(80).collect();
                        }
                        background.run.chat_id = chat.id.clone();
                        background.chat = Some(chat);
                        created = true;
                    }
                }
            }
        }
        background.events.extend(batch);
        if ended {
            let background = self.background_runs.remove(index);
            let chat = background.chat.map(|c| c.id);
            // Only a terminal event proves completion; transport loss does not.
            let outcome = background.events.iter().rev().find_map(|event| match event {
                StreamMsg::Event(ev) => match ev.get("type").and_then(Value::as_str) {
                    Some("done") => Some(true),
                    Some("error") => Some(false),
                    _ => None,
                },
                _ => None,
            });
            if let (Some(id), Some(success)) = (&chat, outcome) {
                self.unread_results.insert(id.clone(), success);
            }
            self.stale_run_ended(chat, cx);
        } else if created {
            if self.project_id.is_none() {
                self.project_id = self.background_runs[index].chat.as_ref().map(|c| c.project_id.clone());
            }
            self.load_projects(None, cx);
        }
        cx.notify();
    }

    fn merge_running_chats(&mut self) {
        let running = self.background_runs.iter().filter_map(|b| b.chat.as_ref())
            .chain(self.chat.as_ref().filter(|_| self.run.is_some())).cloned().collect::<Vec<_>>();
        for chat in running {
            if self.project_id.as_deref() != Some(chat.project_id.as_str()) { continue; }
            if let Some(row) = self.chats.iter_mut().find(|c| c.id == chat.id) {
                if row.title == "New chat" { row.title = chat.title; }
            } else {
                self.chats.insert(0, chat);
            }
        }
    }

    pub fn new_chat(&mut self, cx: &mut Context<Self>) {
        self.begin_view(cx);
        self.chat = None;
        self.pending_access = None; // a new chat starts with the project's access
        self.context = None;
        self.msgs.clear();
        self.rebuild_rows();
        self.selection = Selection::default();
        self.notice = None;
        self.interrupted = None;
        self.options_menu = false;
        self.composer.update(cx, |c, cx| c.set_disabled(false, cx));
        cx.notify();
    }

    pub fn open_chat(&mut self, id: String, cx: &mut Context<Self>) {
        self.open_chat_inner(id, false, cx)
    }

    fn open_chat_inner(&mut self, id: String, force: bool, cx: &mut Context<Self>) {
        self.unread_results.remove(&id);
        // A → B → A: while B is loading, A is still on screen but no longer what
        // you asked for, so clicking A again must open A (and cancel B).
        let showing = self.opening.is_none() && self.run.is_none()
            && self.chat.as_ref().map(|c| c.id == id).unwrap_or(false);
        let already_opening = self.opening.as_deref() == Some(id.as_str());
        if !force && (showing || already_opening) {
            return;
        }
        let view = self.begin_view(cx);
        if let Some(index) = self.background_runs.iter().position(|b| b.run.chat_id == id) {
            let background = self.background_runs.remove(index);
            self.chat = background.chat;
            self.project_id = self.chat.as_ref().map(|c| c.project_id.clone());
            self.msgs = background.msgs;
            self.context = background.context;
            self.approval = background.approval;
            self.notice = background.notice;
            self.load_access_after_stream = background.load_access;
            self.interrupted = None;
            self.pending_access = None;
            self.run = Some(background.run);
            self.rebuild_rows();
            self.composer.update(cx, |c, cx| c.set_disabled(false, cx));
            self.apply_stream(background.events, cx);
            self.load_access(cx);
            cx.notify();
            return;
        }
        self.opening = Some(id.clone());
        self.loading_chat = true;
        let t0 = Instant::now();
        // fetch and parse every message off the UI thread
        self.api_call(
            cx,
            move |api| {
                let t: api::Transcript = api.get(&format!("/api/chats/{id}"))?;
                let mut msgs: Vec<Msg> = t.messages.iter().map(stored_msg).collect();
                let mut interrupted = None;
                // M4: replies that were tasks get their report line back.
                // M5: and a task the runtime was stopped in the middle of is offered for resuming.
                if let Ok(list) = api.get::<api::TaskList>(&format!("/api/tasks?chat_id={id}")) {
                    interrupted = list.tasks.iter().rev().find_map(|x| {
                        let r = x.resume.as_ref().filter(|r| r.possible)?;
                        Some(Interrupted {
                            task_id: x.task_id.clone(),
                            text: r.text.clone().unwrap_or_default(),
                            report: x.report.as_ref().and_then(api::report_line).map(|(l, _)| l).unwrap_or_default(),
                            note: match api::resume_note(r) {   // M9: what was already changed
                                Some(extra) => format!("{} {extra}", r.message),
                                None => r.message.clone(),
                            },
                        })
                    });
                    {
                        for (m, msg) in t.messages.iter().zip(msgs.iter_mut()) {
                            // the user's message carries the same task id; the report belongs to the reply only
                            if m.role == "user" {
                                continue;
                            }
                            let Some(tid) = m.task_id.as_deref() else { continue };
                            let rep = list.tasks.iter().find(|x| x.task_id == tid).and_then(|x| x.report.as_ref());
                            if let Some((line, attention)) = rep.and_then(api::report_line) {
                                msg.meta = format!("{} · {line}", msg.meta);
                                if attention && msg.meta_kind == MetaKind::Normal {
                                    msg.meta_kind = MetaKind::Warn;
                                }
                            }
                        }
                    }
                }
                Ok((t.chat, msgs, interrupted))
            },
            move |this, r, cx| {
                if view != this.view {
                    return; // you went somewhere else meanwhile
                }
                this.loading_chat = false;
                this.opening = None;
                match r {
                    Ok((chat, msgs, interrupted)) => {
                        let archived = chat.archived;
                        this.chat = Some(chat);
                        this.context = None;
                        this.msgs = msgs;
                        this.interrupted = interrupted;
                        this.rebuild_rows();
                        this.selection = Selection::default();
                        this.notice = None;
                        this.composer.update(cx, |c, cx| c.set_disabled(archived, cx));
                        let ms = t0.elapsed().as_millis();
                        this.perf.open_chat_ms = Some(ms);
                        logfile::event("ui.chat_opened", &[("messages", this.msgs.len().to_string()), ("ms", ms.to_string())]);
                    }
                    Err(e) => this.notice = Some((e.message, MetaKind::Bad, false)),
                }
            },
        );
    }

    pub fn set_archived(&mut self, archive: bool, cx: &mut Context<Self>) {
        let Some(chat) = self.chat.clone() else { return };
        self.set_chat_archived(chat.id, archive, cx);
    }

    fn set_chat_archived(&mut self, id: String, archive: bool, cx: &mut Context<Self>) {
        self.options_menu = false;
        self.chat_menu = None;
        if archive {
            self.stop_chat_reply(&id, cx); // archive stops work; navigation does not
        }
        let path = format!("/api/chats/{}/{}", id, if archive { "archive" } else { "unarchive" });
        self.api_call(
            cx,
            move |api| {
                // the stopped reply may still be saving: retry briefly on "busy"
                for _ in 0..50 {
                    match api.post::<api::OneChat>(&path, json!({})) {
                        Err(e) if e.code == "chat_busy" => std::thread::sleep(Duration::from_millis(100)),
                        other => return other,
                    }
                }
                api.post::<api::OneChat>(&path, json!({}))
            },
            move |this, r, cx| match r {
                Ok(one) => {
                    if this.chat.as_ref().map(|c| c.id == one.chat.id).unwrap_or(false) {
                        let archived = one.chat.archived;
                        this.chat = Some(one.chat);
                        this.composer.update(cx, |c, cx| c.set_disabled(archived, cx));
                    }
                    this.load_projects(None, cx);
                    if this.settings_open { this.load_settings_archived(cx); }
                }
                Err(e) => this.notice = Some((e.message, MetaKind::Bad, false)),
            },
        );
    }

    #[cfg(feature = "e2e")]
    pub fn add_project_named(&mut self, name: String, window: &mut Window, cx: &mut Context<Self>) {
        self.add_project(name, window, cx)
    }

    fn add_project(&mut self, name: String, window: &mut Window, cx: &mut Context<Self>) {
        let name = name.trim().to_string();
        self.api_call(
            cx,
            move |api| api.post::<Value>("/api/projects", json!({ "name": name })),
            |this, r, cx| match r {
                Ok(v) => {
                    this.project_input = None;
                    this.project_error = None;
                    let id = v.pointer("/project/id").and_then(Value::as_str).map(str::to_string);
                    if let Some(id) = id.clone() {
                        this.project_id = Some(id);
                        this.new_chat(cx);
                    }
                    this.load_projects(id, cx);
                }
                Err(e) => this.project_error = Some(e.message),
            },
        );
        let _ = window;
    }

    fn can_send(&self) -> bool {
        matches!(self.phase, Phase::Ready)
            && self.provider_up()
            && self.model.is_some()
            && self.run.is_none()
            && !self.chat.as_ref().map(|c| c.archived).unwrap_or(false)
    }

    // ---------- sending and streaming ----------
    pub fn send(&mut self, text: String, _window: &mut Window, cx: &mut Context<Self>) {
        self.send_inner(text, None, cx)
    }

    /// M5: run the interrupted task again, from its waiting message, under the
    /// current settings. Nothing from the interrupted attempt is replayed.
    pub fn resume_task(&mut self, cx: &mut Context<Self>) {
        let Some(it) = self.interrupted.clone() else { return };
        if self.chat.is_none() || !self.can_send() {
            return;
        }
        logfile::event("ui.resume", &[]);
        self.send_inner(it.text, Some(it.task_id), cx)
    }

    fn send_inner(&mut self, text: String, resume: Option<String>, cx: &mut Context<Self>) {
        if !self.can_send() || text.trim().is_empty() {
            return;
        }
        self.interrupted = None;
        let model = self.model.clone().unwrap();
        let use_memory = self.use_memory;
        let controls = self.current_model_controls();
        let project_id = self.project_id.clone();
        let existing = self.chat.as_ref().map(|c| c.id.clone());
        let existing_for_ui = existing.clone();
        let access = if existing.is_none() { self.pending_access.clone() } else { None };
        if resume.is_none() {
            self.composer.update(cx, |c, cx| c.set_text("", cx));
        }
        self.notice = None;
        // show the message and an empty reply at once; the runtime saves both together
        let n = self.msgs.len();
        self.msgs.push(Msg {
            role: Role::User,
            blocks: Arc::new(markdown::plain(&text)),
            parsed_len: text.len(),
            text: text.clone(),
            meta: String::new(),
            activity_icon: None,
            meta_kind: MetaKind::Normal,
            streaming: false,
        });
        self.msgs.push(Msg {
            role: Role::Assistant,
            text: String::new(),
            blocks: Arc::new(vec![]),
            parsed_len: 0,
            meta: "Working…".into(),
            activity_icon: Some("logo"),
            meta_kind: MetaKind::Normal,
            streaming: true,
        });
        self.follow.set(true); // sending always brings the conversation's end into view
        self.append_rows(n);
        self.append_rows(n + 1);
        let stop = Arc::new(AtomicBool::new(false));
        let cancel_sent = Arc::new(AtomicBool::new(false));
        self.run_seq += 1;
        let token = self.run_seq;
        self.run = Some(Run { token, msg_ix: n + 1, chat_id: existing.clone().unwrap_or_default(), run_id: None, stop: stop.clone(), cancel_sent: cancel_sent.clone(), memory_ids: vec![] });
        cx.notify();

        let Some(api) = self.api.clone() else { return };
        let (tx, mut rx) = mpsc::unbounded::<StreamMsg>();
        // A plain thread: the stream blocks on the network for up to minutes.
        let api2 = api.clone();
        let created_tx = tx.clone();
        std::thread::Builder::new()
            .name("chat-stream".into())
            .spawn(move || {
                let chat_id = match existing {
                    Some(id) => id,
                    None => match api2.post::<api::OneChat>("/api/chats", json!({ "project_id": project_id, "access": access })) {
                        Ok(c) => {
                            let _ = created_tx.unbounded_send(StreamMsg::Event(json!({"type": "chat_created", "chat": {
                                "id": c.chat.id, "project_id": c.chat.project_id, "title": c.chat.title,
                                "updated_at": c.chat.updated_at, "archived": c.chat.archived, "messages": c.chat.messages,
                                "access": c.chat.access}})));
                            c.chat.id
                        }
                        Err(e) => {
                            let _ = tx.unbounded_send(StreamMsg::Ended(Some(e)));
                            return;
                        }
                    },
                };
                let r = api2.chat_stream(&chat_id, &model, &text, resume.as_deref(), use_memory, &controls, |ev| {
                    let more = api::keep_reading(stop.load(Ordering::SeqCst), cancel_sent.load(Ordering::SeqCst),
                                                 ev.get("type").and_then(Value::as_str).unwrap_or(""));
                    let _ = tx.unbounded_send(StreamMsg::Event(ev));
                    more
                });
                let _ = tx.unbounded_send(StreamMsg::Ended(r.err()));
            })
            .expect("thread");

        // Apply events in batches: wait for one, take everything queued, then pause ~33 ms.
        let mut stream_chat = existing_for_ui;
        cx.spawn(async move |this, cx| {
            loop {
                let Some(first) = rx.next().await else { break };
                let mut batch = vec![first];
                while let Ok(more) = rx.try_recv() {
                    batch.push(more);
                }
                let ended = batch.iter().any(|m| matches!(m, StreamMsg::Ended(_)));
                for m in &batch {
                    if let StreamMsg::Event(ev) = m {
                        if let Some(id) = ev.pointer("/chat/id").and_then(Value::as_str) {
                            stream_chat = Some(id.to_string());
                        }
                    }
                }
                let chat_of_run = stream_chat.clone();
                let applied = this.update(cx, |this, cx| {
                    if this.run.as_ref().map(|r| r.token) == Some(token) {
                        this.apply_stream(batch, cx);
                    } else if this.background_runs.iter().any(|b| b.run.token == token) {
                        this.background_events(token, batch, ended, cx);
                    } else if ended {
                        this.stale_run_ended(chat_of_run, cx);
                    }
                });
                if applied.is_err() || ended {
                    break;
                }
                cx.background_executor().timer(STREAM_BATCH).await;
            }
        })
        .detach();
    }

    fn apply_stream(&mut self, batch: Vec<StreamMsg>, cx: &mut Context<Self>) {
        let t0 = Instant::now();
        self.perf.stream_batches += 1;
        let Some(ix) = self.run.as_ref().map(|r| r.msg_ix).filter(|&ix| ix < self.msgs.len()) else { return };
        let sent_title: String = self.msgs[ix.saturating_sub(1)].text.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(80).collect();
        let mut ended = false;
        let mut refresh_lists = false;
        for m in batch {
            match m {
                StreamMsg::Event(ev) => {
                    self.perf.stream_events += 1;
                    let ty = ev.get("type").and_then(Value::as_str).unwrap_or("");
                    let Some(msg) = self.msgs.get_mut(ix) else { return };
                    match ty {
                        "workspace_resolving" => {
                            if let Some(run) = self.run.as_mut() {
                                run.run_id = ev.get("run_id").and_then(Value::as_str).map(str::to_string);
                            }
                            msg.meta = "Finding workspace…".into();
                            msg.activity_icon = Some("folder");
                        }
                        "chat_created" => {
                            if let Ok(mut c) = serde_json::from_value::<api::ChatInfo>(ev["chat"].clone()) {
                                c.title = sent_title.clone();
                                if let Some(run) = self.run.as_mut() {
                                    run.chat_id = c.id.clone();
                                }
                                self.project_id = Some(c.project_id.clone());
                                self.chat = Some(c);
                                refresh_lists = true;
                            }
                        }
                        "start" => {
                            if let Some(run) = self.run.as_mut() {
                                run.run_id = ev.get("run_id").and_then(Value::as_str).map(str::to_string);
                                run.memory_ids = ev.pointer("/memory/used").and_then(Value::as_array)
                                    .map(|a| a.iter().filter_map(|m| m.get("id").and_then(Value::as_str)).map(str::to_string).collect())
                                    .unwrap_or_default();
                            }
                            msg.meta = "Working…".into();
                            msg.activity_icon = Some("logo");
                            let c = &ev["context"];
                            if let (Some(est), Some(win)) = (c["estimated_tokens"].as_u64(), c["window"].as_u64()) {
                                self.context = Some((est, win));
                            }
                            // M9: "work on <folder>" attached a folder, or it is Umbrous's own source
                            if let Some(note) = api::folder_note(&ev["tools"]) {
                                self.notice = Some((note, MetaKind::Warn, false));
                            }
                            if ev.pointer("/tools/selected/attached").and_then(Value::as_bool).unwrap_or(false) {
                                self.load_access_after_stream = true;
                            }
                            // a folder is attached but this model gets no tools: say so, never silently
                            if let Some(why) = ev.pointer("/tools/off_reason").and_then(Value::as_str) {
                                self.notice = Some((why.to_string(), MetaKind::Warn, false));
                            }
                        }
                        "delta" => {
                            msg.text.push_str(ev.get("text").and_then(Value::as_str).unwrap_or(""));
                            msg.meta = "Writing response…".into();
                            msg.activity_icon = Some("pen");
                        },
                        // M7: the reply is saved; the task looks for things to remember
                        "suggesting" => {
                            msg.meta = "Finishing…".into();
                            msg.activity_icon = Some("logo");
                        },
                        "done" => {
                            msg.activity_icon = None;
                            let g = |k: &str| ev.get(k).and_then(Value::as_u64);
                            let model = ev.get("model").and_then(Value::as_str).unwrap_or("").to_string();
                            let mut parts = vec![model];
                            if let (Some(p), Some(r)) = (g("prompt_tokens"), g("reply_tokens")) {
                                parts.push(format!("{p} in · {r} out"));
                            }
                            if let Some(ms) = g("ms") {
                                parts.push(format!("{:.1}s", ms as f64 / 1000.0));
                            }
                            // M6: the memory items this reply was given
                            let ids: Vec<String> = ev.get("memory_ids").and_then(Value::as_array)
                                .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default();
                            parts.extend(api::memory_note(&ids));
                            msg.meta = parts.join(" · ");
                            msg.meta_kind = MetaKind::Normal;
                            // M4: a task's report, in one line (the trace is in the browser page)
                            if let Some((line, attention)) = api::report_line(&ev["report"]) {
                                msg.meta = format!("{} · {line}", msg.meta);
                                if attention {
                                    msg.meta_kind = MetaKind::Warn;
                                }
                            }
                            // M7: suggestions wait in Memory; nothing was saved
                            let n = ev.get("candidates").and_then(Value::as_array).map(Vec::len).unwrap_or(0);
                            if let Some(note) = api::suggestion_note(n) {
                                msg.meta = format!("{} · {note}", msg.meta);
                            }
                            refresh_lists = true;
                        }
                        "tool_call" => {
                            msg.meta = "Preparing action…".into();
                            msg.activity_icon = Some("clock");
                        }
                        "tool_started" => {
                            let tool = ev.get("tool").and_then(Value::as_str).unwrap_or("a tool");
                            let label = api::step_label(tool, &ev["args"]);
                            let mut chars = label.chars();
                            let label = chars.next().map(|c| c.to_uppercase().to_string() + chars.as_str()).unwrap_or_default();
                            msg.meta = format!("{label}…");
                            msg.activity_icon = Some(api::activity_icon(tool));
                        }
                        "approval_required" => {
                            self.approval = PendingApproval::from_event(&ev);
                            msg.meta = "Approval needed".into();
                            msg.activity_icon = Some("shield");
                        }
                        "tool_result" => {
                            // answered (here, by a time limit, or by Stop): the card has done its job
                            self.approval = None;
                            msg.meta = "Working…".into();
                            msg.activity_icon = Some("logo");
                        }
                        "error" => {
                            msg.activity_icon = None;
                            let code = ev.get("code").and_then(Value::as_str).unwrap_or("error");
                            let text = ev.get("message").and_then(Value::as_str).unwrap_or("The reply failed.").to_string();
                            let partial = ev.get("partial").and_then(Value::as_bool).unwrap_or(false);
                            msg.meta = if code == "cancelled" {
                                format!("Stopped by you.{}", if partial { " The text above is incomplete." } else { "" })
                            } else {
                                text.clone()
                            };
                            msg.meta_kind = if code == "cancelled" { MetaKind::Warn } else { MetaKind::Bad };
                            if let Some((line, _)) = api::report_line(&ev["report"]) {
                                msg.meta = format!("{} {line}.", msg.meta.trim_end());
                            }
                            // M6: a partial reply is stored with the memory it was given; say so now,
                            // not only when the chat is opened again
                            if partial {
                                let ids = self.run.as_ref().map(|r| r.memory_ids.clone()).unwrap_or_default();
                                if let Some(n) = api::memory_note(&ids) {
                                    msg.meta = format!("{} · {n}", msg.meta.trim_end());
                                }
                            }
                            if code == "ollama_unreachable" {
                                self.notice = Some((text, MetaKind::Warn, true));
                                if let Some(m) = self.models.as_mut() {
                                    m.reachable = false;
                                }
                            }
                            refresh_lists = true;
                        }
                        _ => {}
                    }
                }
                StreamMsg::Ended(err) => {
                    ended = true;
                    let Some(msg) = self.msgs.get_mut(ix) else { return };
                    let ended_early = msg.activity_icon.take().is_some();
                    if let Some(e) = err {
                        if ended_early {
                            msg.meta = format!("Connection lost: {} Work may still be running. Reopen this chat to check its saved result.", e.message);
                            msg.meta_kind = MetaKind::Bad;
                        }
                    } else if ended_early {
                        msg.meta = "Connection closed before a final result. Work may still be running; reopen this chat to check.".into();
                        msg.meta_kind = MetaKind::Warn;
                    }
                    // the runtime keeps text that had arrived, with its memory ids (Stop or a
                    // transport error alike), so the meta line names them as well
                    if ended_early && !msg.text.trim().is_empty() {
                        let ids = self.run.as_ref().map(|r| r.memory_ids.clone()).unwrap_or_default();
                        if let Some(n) = api::memory_note(&ids) {
                            msg.meta = format!("{} · {n}", msg.meta.trim_end());
                        }
                    }
                }
            }
        }
        if ended {
            self.msgs[ix].streaming = false;
            self.run = None;
            self.approval = None;
            refresh_lists = true;
            // an empty reply is never saved; drop the pair from the view as well
            if self.msgs[ix].text.trim().is_empty() && ix >= 1 && self.msgs[ix].meta_kind != MetaKind::Normal {
                // keep the error visible on the reply row, but show the message again in the box
                let text = self.msgs[ix - 1].text.clone();
                if self.composer.read(cx).text().is_empty() {
                    self.composer.update(cx, |c, cx| c.set_text(&text, cx));
                }
            }
        }
        self.schedule_parse(ix, cx);
        self.refresh_msg_rows(ix, usize::MAX); // only the meta row changes here; blocks change after parsing
        if refresh_lists {
            self.load_projects(None, cx);
        }
        if std::mem::take(&mut self.load_access_after_stream) {
            self.load_access(cx);
        }
        let ms = t0.elapsed().as_secs_f64() * 1000.0;
        self.perf.apply_ms_max = self.perf.apply_ms_max.max(ms);
        cx.notify();
    }

    /// Parse the streaming message's markdown on a background thread; latest text wins.
    fn schedule_parse(&mut self, ix: usize, cx: &mut Context<Self>) {
        if self.parse_in_flight {
            return;
        }
        let Some(m) = self.msgs.get(ix) else { return };
        if m.parsed_len == m.text.len() {
            return;
        }
        self.parse_in_flight = true;
        let view = self.view;
        let text = m.text.clone();
        let task = cx.background_spawn(async move {
            let t = Instant::now();
            let blocks = markdown::parse(&text);
            (blocks, text.len(), t.elapsed().as_secs_f64() * 1000.0)
        });
        cx.spawn(async move |this, cx| {
            let (blocks, len, ms) = task.await;
            this.update(cx, |this, cx| {
                if view != this.view {
                    return; // parsed for a transcript that has been replaced
                }
                this.parse_in_flight = false;
                this.perf.parse_runs += 1;
                this.perf.parse_ms_total += ms;
                this.perf.parse_ms_max = this.perf.parse_ms_max.max(ms);
                if let Some(m) = this.msgs.get_mut(ix) {
                    // only rows from the first block that changed are re-measured
                    let first_changed = m.blocks.iter().zip(blocks.iter())
                        .position(|(a, b)| a.text != b.text || a.kind != b.kind || a.spans != b.spans)
                        .unwrap_or(m.blocks.len().min(blocks.len()));
                    m.blocks = Arc::new(blocks);
                    m.parsed_len = len;
                    this.refresh_msg_rows(ix, first_changed);
                }
                this.schedule_parse(ix, cx); // text may have grown meanwhile
                cx.notify();
            })
            .ok();
        })
        .detach();
    }

    fn stale_run_ended(&mut self, run_chat: Option<String>, cx: &mut Context<Self>) {
        self.load_projects(None, cx);
        let showing = self.chat.as_ref().map(|c| c.id.clone());
        if run_chat.is_some() && showing == run_chat && self.run.is_none() && !self.loading_chat {
            // you came back to that chat while its stopped reply was being saved
            self.open_chat_inner(showing.unwrap(), true, cx);
        }
    }

    /// Title of the chat a pending open is for (e2e checks that nothing is left loading).
    #[cfg(feature = "e2e")]
    pub fn opening_title(&self) -> Option<String> {
        let id = self.opening.as_ref()?;
        Some(self.chats.iter().chain(self.archived.iter()).find(|c| &c.id == id).map(|c| c.title.clone()).unwrap_or_else(|| id.clone()))
    }

    #[cfg(feature = "e2e")]
    pub fn set_card_open(&mut self, open: bool, cx: &mut Context<Self>) {
        self.details = open;
        cx.notify();
    }

    /// (following the end, corrective frames left) for e2e scroll checks.
    #[cfg(feature = "e2e")]
    pub fn scroll_state(&self) -> (bool, u8) {
        (self.follow.get(), self.reveal_frames.get())
    }

    #[cfg(feature = "e2e")]
    pub fn is_running(&self) -> bool {
        self.run.is_some()
    }

    #[cfg(feature = "e2e")]
    pub fn background_count(&self) -> usize { self.background_runs.len() }

    #[cfg(feature = "e2e")]
    pub fn unread_count(&self) -> usize { self.unread_results.len() }

    pub fn stop_reply(&mut self, cx: &mut Context<Self>) {
        if let Some(run) = self.run.as_ref() {
            let id = run.chat_id.clone();
            self.stop_chat_reply(&id, cx);
        }
    }

    fn stop_chat_reply(&mut self, chat_id: &str, cx: &mut Context<Self>) {
        if let Some(run) = self.run.as_ref().filter(|r| r.chat_id == chat_id)
            .or_else(|| self.background_runs.iter().find(|b| b.run.chat_id == chat_id).map(|b| &b.run)) {
            // cancel_sent before stop: the stream thread must never see Stop without it
            run.cancel_sent.store(run.run_id.is_some(), Ordering::SeqCst);
            run.stop.store(true, Ordering::SeqCst);
            if let Some(run_id) = run.run_id.clone() {
                self.api_call(cx, move |api| api.post::<Value>("/api/chat/cancel", json!({ "run_id": run_id })), |_, _, _| {});
            }
        }
    }

    // ---------- list rows ----------
    fn rebuild_rows(&mut self) {
        self.rows.clear();
        self.msg_rows.clear();
        for (i, m) in self.msgs.iter().enumerate() {
            let start = self.rows.len();
            self.rows.extend(transcript::rows_for(i, m));
            self.msg_rows.push(start..self.rows.len());
        }
        self.list.reset(self.rows.len());
        self.follow.set(true);
        self.reveal_end();
    }

    fn reveal_end(&self) {
        if self.follow.get() && !self.rows.is_empty() {
            self.list.scroll_to_reveal_item(self.rows.len() - 1);
            self.reveal_frames.set(REVEAL_FRAMES); // render finishes it once the rows are measured
        }
    }

    fn append_rows(&mut self, ix: usize) {
        let start = self.rows.len();
        let new = transcript::rows_for(ix, &self.msgs[ix]);
        let n = new.len();
        self.rows.extend(new);
        self.msg_rows.push(start..start + n);
        self.list.splice(start..start, n);
        self.reveal_end();
    }

    /// Replace message `ix`'s rows; rows before block `first_changed` keep their
    /// measured heights. With `usize::MAX` only the trailing meta/pending rows change.
    fn refresh_msg_rows(&mut self, ix: usize, first_changed: usize) {
        let Some(old) = self.msg_rows.get(ix).cloned() else { return };
        let new = transcript::rows_for(ix, &self.msgs[ix]);
        let keep = new
            .iter()
            .zip(self.rows[old.clone()].iter())
            .take_while(|(a, b)| a == b && match a.kind { transcript::RowKind::Block(bi) => bi < first_changed, _ => false })
            .count();
        let (old_tail, new_tail) = (old.start + keep..old.end, new.len() - keep);
        let delta = new.len() as isize - old.len() as isize;
        self.rows.splice(old_tail.clone(), new[keep..].iter().copied());
        self.list.splice(old_tail, new_tail);
        self.msg_rows[ix] = old.start..old.start + new.len();
        self.reveal_end();
        for r in self.msg_rows.iter_mut().skip(ix + 1) {
            *r = (r.start as isize + delta) as usize..(r.end as isize + delta) as usize;
        }
    }

    // ---------- actions ----------
    fn copy(&mut self, _: &Copy, _: &mut Window, cx: &mut Context<Self>) {
        if let Some(t) = transcript::selected_text(&self.msgs, &self.selection) {
            cx.write_to_clipboard(ClipboardItem::new_string(t));
        }
    }
    fn select_all(&mut self, _: &SelectAll, _: &mut Window, cx: &mut Context<Self>) {
        if self.msgs.is_empty() {
            return;
        }
        let last = self.msgs.len() - 1;
        let lb = self.msgs[last].blocks.len().saturating_sub(1);
        let len = self.msgs[last].blocks.last().map(|b| b.text.len()).unwrap_or(0);
        self.selection = Selection { anchor: Some(Pos { msg: 0, block: 0, offset: 0 }), head: Some(Pos { msg: last, block: lb, offset: len }), dragging: false };
        cx.notify();
    }
    fn escape(&mut self, _: &Escape, window: &mut Window, cx: &mut Context<Self>) {
        if self.full_access_confirm.is_some() {
            self.full_access_confirm = None;
        } else if self.deletion.is_some() {
            self.cancel_deletion(cx);
        } else if self.project_menu.is_some() {
            self.project_menu = None;
        } else if self.reset.is_some() {
            self.close_reset(cx);
        } else if self.chat_menu.is_some() {
            self.chat_menu = None;
        } else if self.settings_open {
            self.settings_open = false;
        } else if self.model_menu || self.access_menu || self.options_menu || self.project_input.is_some() || self.card_menu
            || self.memory_input.is_some() || self.memory_armed.is_some() {
            self.memory_input = None;
            self.memory_armed = None;
            self.card_menu = false;
            self.model_menu = false;
            self.access_menu = false;
            self.options_menu = false;
            self.project_input = None;
            self.project_error = None;
        } else if self.run.is_some() {
            self.stop_reply(cx);
        } else {
            self.selection = Selection::default();
        }
        window.focus(&self.composer.focus_handle(cx));
        cx.notify();
    }
}

fn stored_msg(m: &api::Message) -> Msg {
    let (role, blocks) = if m.role == "user" {
        (Role::User, markdown::plain(&m.content))
    } else {
        (Role::Assistant, markdown::parse(&m.content))
    };
    let (meta, kind) = if role == Role::User {
        (String::new(), MetaKind::Normal)
    } else {
        let toks = match (m.prompt_tokens, m.reply_tokens) {
            (Some(p), Some(r)) => format!("{p} in · {r} out"),
            _ => String::new(),
        };
        let model = m.model.clone().unwrap_or_default();
        let mem = m.memory_ids.as_deref().and_then(api::memory_note).map(|n| format!(" · {n}")).unwrap_or_default();
        match m.status.as_str() {
            "complete" => ([model, toks].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · ") + &mem, MetaKind::Normal),
            "stopped" => (format!("{model} · Stopped by you. Incomplete.{mem}"), MetaKind::Warn),
            other => (format!("{model} · Incomplete ({other}).{mem}"), MetaKind::Warn),
        }
    };
    Msg { role, text: m.content.clone(), parsed_len: m.content.len(), blocks: Arc::new(blocks), meta, activity_icon: None, meta_kind: kind, streaming: false }
}

// ---------------- rendering ----------------
//
// Every view below mirrors a part of prototypes/umbrous-harness.html. The
// comments name the CSS class it reproduces; sizes and colours come from
// theme.rs, which holds that file's tokens.

/// chat.js `ago()`: now, 5m, 3h, 2d
fn ago(iso: &str) -> String {
    let secs = parse_iso_secs(iso).map(|t| {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        (now - t).max(0)
    });
    match secs {
        Some(s) if s < 60 => "now".into(),
        Some(s) if s < 3600 => format!("{}m", s / 60),
        Some(s) if s < 86_400 => format!("{}h", s / 3600),
        Some(s) => format!("{}d", s / 86_400),
        None => String::new(),
    }
}

/// "2026-11-07T13:40:27.844Z" → Unix seconds (UTC), without a date library.
fn parse_iso_secs(iso: &str) -> Option<i64> {
    let (date, time) = iso.split_once('T')?;
    let mut d = date.split('-').map(|x| x.parse::<i64>());
    let (y, m, dd) = (d.next()?.ok()?, d.next()?.ok()?, d.next()?.ok()?);
    let t: Vec<i64> = time.trim_end_matches('Z').split(':').map(|x| x.split('.').next().unwrap_or("0").parse().unwrap_or(0)).collect();
    let (y2, m2) = if m <= 2 { (y - 1, m + 9) } else { (y, m - 3) };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let doy = (153 * m2 + 2) / 5 + dd - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(days * 86_400 + t.first().copied().unwrap_or(0) * 3600 + t.get(1).copied().unwrap_or(0) * 60 + t.get(2).copied().unwrap_or(0))
}

/// chat.js `fmtK()`
fn fmt_k(n: u64) -> String {
    if n >= 10_000 { format!("{}k", (n as f64 / 1000.0).round()) } else if n >= 1000 { format!("{:.1}k", n as f64 / 1000.0) } else { n.to_string() }
}

impl Workspace {
    /// `aside.panel`: workspace switcher, New chat, Projects, Sessions.
    fn render_sidebar(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let mut projects = div().flex().flex_col();
        for p in &self.projects {
            let current = self.project_id.as_deref() == Some(&p.id);
            let id = p.id.clone();
            let row_id = SharedString::from(format!("p-{}", p.id));
            let menu_id = p.id.clone();
            let delete_id = p.id.clone();
            let delete_title = p.name.clone();
            projects = projects.child(
                ui::nav(row_id.clone(), current)
                    .font_weight(FontWeight::NORMAL)
                    .child(ui::hover_icon("folder", &row_id, if current { text_strong() } else { text_default() }, text_strong()))
                    .child(ui::nav_title(p.name.clone()))
                    .when(p.chats > 0, |d| d.child(ui::nav_count(p.chats.to_string())))
                    .child(div().id(SharedString::from(format!("project-menu-{}", p.id)))
                        .size(px(28.)).flex_none().flex().items_center().justify_center().rounded(px(R_6))
                        .hover(|s| s.bg(bg_hover())).child(ui::icon("more").text_color(text_default()))
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                            cx.stop_propagation();
                            this.chat_menu = None;
                            this.project_menu = if this.project_menu.as_ref() == Some(&menu_id) { None } else { Some(menu_id.clone()) };
                            cx.notify();
                        })))
                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.select_project(id.clone(), cx))),
            );
            if self.project_menu.as_ref() == Some(&p.id) {
                projects = projects.child(div().p(px(6.)).rounded(px(R_8)).bg(bg_raised())
                    .child(ui::nav(SharedString::from(format!("delete-project-{}", p.id)), false).text_color(danger()).child("Delete project…")
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                            this.open_deletion(true, delete_id.clone(), delete_title.clone(), cx);
                        }))));
            }
        }
        if self.projects.is_empty() {
            projects = projects.child(ui::nav("create-first-project", false).child("Create project")
                .on_click(cx.listener(|this, _: &ClickEvent, window, cx| this.begin_add_project(window, cx))));
        }
        if let Some(input) = &self.project_input {
            // `.proj-input`
            projects = projects.child(
                div()
                    .h(px(32.))
                    .px(px(10.))
                    .flex()
                    .items_center()
                    .border_1()
                    .border_color(border_strong())
                    .rounded(px(R_8))
                    .bg(bg_raised())
                    .text_size(fs_13())
                    .child(input.clone()),
            );
            if let Some(err) = &self.project_error {
                projects = projects.child(div().text_size(fs_12()).text_color(danger()).px(px(10.)).py(px(4.)).child(err.clone()));
            }
        }

        let mut sessions = div().flex().flex_col();
        let current = self.chat.as_ref().map(|c| c.id.clone());
        let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref());
        let pname = project.map(|p| p.name.clone()).unwrap_or_default();
        let archived_count = project.map(|p| p.archived_chats).unwrap_or(0);
        if self.chats.is_empty() {
            // `.side-empty`
            let text = if self.project_id.is_none() {
                "No chats yet. Send a message to get started.".into()
            } else if archived_count > 0 {
                format!("No active chats in {pname}. {archived_count} archived.")
            } else {
                format!("No chats in {pname} yet.")
            };
            sessions = sessions.child(div().text_size(fs_12()).text_color(text_subtle()).px(px(10.)).py(px(4.)).child(text));
        }
        for c in self.chats.iter().chain(self.archived.iter().filter(|_| self.show_archived)) {
            let id = c.id.clone();
            let is_current = current.as_deref() == Some(&c.id);
            let streaming = self.run.as_ref().map(|r| r.chat_id == c.id).unwrap_or(false)
                || self.background_runs.iter().any(|b| b.run.chat_id == c.id);
            let mut row = ui::nav(SharedString::from(format!("c-{}", c.id)), is_current);
            row = if c.archived {
                // `.nav.archived .t`: subtle, italic
                row.child(ui::nav_title(c.title.clone()).text_color(text_subtle()).italic()).child(ui::nav_count("archived"))
            } else {
                row.child(ui::nav_title(c.title.clone()))
            };
            if !c.archived {
                let approval = (is_current && self.approval.is_some())
                    || self.background_runs.iter().any(|b| b.run.chat_id == c.id && b.approval.is_some());
                row = if approval { row.child(ui::nav_count("Approval").text_color(warning())) }
                    else if streaming { row.child(ui::dot(accent())) }
                    else if let Some(success) = self.unread_results.get(&c.id) {
                        row.child(ui::nav_count(if *success { "Done" } else { "Needs attention" })
                            .text_color(if *success { accent() } else { warning() }))
                    }
                    else { row.child(ui::nav_count(ago(&c.updated_at))) };
            }
            let menu_id = id.clone();
            let archive_id = id.clone();
            let delete_id = id.clone();
            let delete_title = c.title.clone();
            let archived = c.archived;
            row = row.child(div().id(SharedString::from(format!("chat-menu-{}", c.id)))
                .size(px(28.)).flex_none().flex().items_center().justify_center().rounded(px(R_6))
                .hover(|s| s.bg(bg_hover())).child(ui::icon("more").text_color(text_default()))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                    cx.stop_propagation();
                    this.chat_menu = if this.chat_menu.as_ref() == Some(&menu_id) { None } else { Some(menu_id.clone()) };
                    cx.notify();
                })));
            sessions = sessions.child(row.on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                this.chat_menu = None;
                this.open_chat(id.clone(), cx);
            })));
            if self.chat_menu.as_ref() == Some(&c.id) {
                sessions = sessions.child(div().p(px(6.)).rounded(px(R_8)).bg(bg_raised()).flex().flex_col()
                    .child(ui::nav(SharedString::from(format!("archive-{}", c.id)), false)
                        .child(if archived { "Restore chat" } else { "Archive chat" })
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_chat_archived(archive_id.clone(), !archived, cx))))
                    .child(ui::nav(SharedString::from(format!("delete-{}", c.id)), false).text_color(danger()).child("Delete chat…")
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.open_deletion(false, delete_id.clone(), delete_title.clone(), cx)))));
            }
        }
        if self.show_archived || archived_count > 0 {
            // `.nav.toggle-archived`
            let label = if self.show_archived { "Hide archived".to_string() } else { format!("Show archived ({archived_count})") };
            sessions = sessions.child(
                ui::nav("toggle-archived", false)
                    .h(px(30.))
                    .text_size(fs_12())
                    .text_color(text_subtle())
                    .child(label)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.show_archived = !this.show_archived;
                        this.load_chats(cx);
                    })),
            );
        }

        // `.section`: 32px, margin-top 24, 13px medium subtle, chevron; `.add` shows on hover
        let section = |id: &'static str, label: &'static str, open: bool| {
            div()
                .id(id)
                .group(id)
                .flex()
                .items_center()
                .gap(px(4.))
                .w_full()
                .h(px(32.))
                .px(px(10.))
                .mt(px(SP_24))
                .rounded(px(R_6))
                .text_size(fs_13())
                .font_weight(FontWeight::MEDIUM)
                .text_color(text_subtle())
                .cursor_pointer()
                .hover(|s| s.text_color(text_default()))
                .child(label)
                .child(ui::chevron(open).text_color(text_subtle()).group_hover(id, |s| s.text_color(text_default())))
        };
        let projects_open = self.projects_open;
        let sessions_open = self.sessions_open;

        ui::panel()
            .w(px(300.))
            .flex_none()
            .h_full()
            // `.ws`: padding 16 16 12
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(8.))
                    .pt(px(SP_16))
                    .px(px(SP_16))
                    .pb(px(SP_12))
                    .child(
                        // `.ws-btn`
                        div()
                            .flex()
                            .items_center()
                            .gap(px(8.))
                            .h(px(32.))
                            .px(px(6.))
                            .ml(px(-6.))
                            .rounded(px(R_8))
                            .text_size(fs_14())
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(text_strong())
                            .hover(|s| s.bg(bg_hover()))
                            .child(div().size(px(22.)).flex().items_center().justify_center().child(ui::icon_sized("logo", 20.).text_color(text_strong())))
                            .child("Umbrous")
                            .child(ui::icon("chevrons").text_color(text_subtle())),
                    )
                    .child(div().flex_1())
                    .child(ui::icon_btn("search", "search", false))
                    .child(ui::icon_btn("collapse-sidebar", "sidebar", false).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.sidebar = false;
                        cx.notify();
                    }))),
            )
            // `.side-scroll`: padding 4 12 12
            .child(
                div()
                    .id("side-scroll")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .pt(px(SP_4))
                    .px(px(SP_12))
                    .pb(px(SP_12))
                    .child(
                        ui::nav("new-chat", self.chat.is_none() && self.msgs.is_empty())
                            .text_color(text_strong())
                            .font_weight(FontWeight::MEDIUM)
                            .child(ui::icon("plus-circle").text_color(text_strong()))
                            .child(ui::nav_title("New chat"))
                            .child(
                                // `.nav kbd`
                                div()
                                    .text_size(fs_12())
                                    .font_weight(FontWeight::NORMAL)
                                    .text_color(text_subtle())
                                    .border_1()
                                    .border_color(border())
                                    .rounded(px(R_4))
                                    .px(px(5.))
                                    .line_height(px(18.))
                                    .child("⌘N"),
                            )
                            .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                                this.new_chat(cx);
                                window.focus(&this.composer.focus_handle(cx));
                            })),
                    )
                    .child(
                        section("sec-projects", "Projects", projects_open)
                            .child(
                                div()
                                    .id("add-project")
                                    .group("add-project")
                                    .ml_auto()
                                    .size(px(22.))
                                    .flex()
                                    .items_center()
                                    .justify_center()
                                    .rounded(px(R_6))
                                    .invisible()
                                    .group_hover("sec-projects", |s| s.visible())
                                    .hover(|s| s.bg(bg_hover()).text_color(text_strong()))
                                    .child(ui::hover_icon("plus", &"add-project".into(), text_subtle(), text_strong()))
                                    .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                                        cx.stop_propagation();
                                        this.begin_add_project(window, cx);
                                    })),
                            )
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                this.projects_open = !this.projects_open;
                                cx.notify();
                            })),
                    )
                    .when(projects_open, |d| d.child(projects))
                    .child(section("sec-sessions", "Sessions", sessions_open).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.sessions_open = !this.sessions_open;
                        cx.notify();
                    })))
                    .when(sessions_open, |d| d.child(sessions)),
            )
            .child(ui::nav("settings", false).child("Settings")
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                    this.settings_open = true;
                    this.chat_menu = None;
                    this.load_settings_archived(cx);
                    cx.notify();
                })))
            .into_any_element()
    }

    fn begin_add_project(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let input = cx.new(|cx| {
            let mut c = Composer::new(cx);
            c.set_placeholder("Project name, then Enter", cx);
            c.set_compact(cx);
            c
        });
        let sub = cx.subscribe_in(&input, window, |this, _, ev: &ComposerEvent, window, cx| match ev {
            ComposerEvent::Submit(name) => this.add_project(name.clone(), window, cx),
        });
        self._subs.push(sub);
        window.focus(&input.focus_handle(cx));
        self.project_input = Some(input);
        self.project_error = None;
        self.projects_open = true;
        cx.notify();
    }

    /// `.head`: 56px, padding 0 16 0 24, bottom border.
    fn render_header(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let title = self.chat.as_ref().map(|c| c.title.clone()).unwrap_or_else(|| "New chat".into());
        let has_chat = self.chat.is_some();
        let sidebar_hidden = !self.sidebar;
        div()
            .h(px(56.))
            .flex_none()
            .flex()
            .items_center()
            .gap(px(10.))
            .pl(px(SP_24))
            .pr(px(SP_16))
            .border_b_1()
            .border_color(border())
            .when(sidebar_hidden, |d| {
                d.child(ui::icon_btn("show-sidebar", "sidebar", false).ml(px(-8.)).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                    this.sidebar = true;
                    cx.notify();
                })))
            })
            .child(ui::icon("plus-circle").text_color(text_subtle()))
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .overflow_hidden()
                    .whitespace_nowrap()
                    .text_ellipsis()
                    .text_size(fs_14())
                    .font_weight(FontWeight::MEDIUM)
                    .text_color(text_strong())
                    .child(title),
            )
            .when(has_chat, |d| {
                d.child(ui::icon_btn("chat-options", "more", self.options_menu).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                    this.options_menu = !this.options_menu;
                    cx.notify();
                })))
            })
            .child(ui::icon_btn("rtoggle", "sidebar-right", self.details).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                this.details = !this.details;
                cx.notify();
            })))
            .into_any_element()
    }

    fn load_settings_archived(&mut self, cx: &mut Context<Self>) {
        self.settings_error = None;
        self.settings_seq += 1;
        let seq = self.settings_seq;
        self.api_call(cx, |api| {
            let projects = api.get::<api::Projects>("/api/projects")?;
            let mut chats = Vec::new();
            for project in projects.projects {
                chats.extend(api.get::<api::Chats>(&format!("/api/chats?project_id={}&archived=1", project.id))?.chats);
            }
            Ok(chats)
        }, move |this, result, _| {
            if this.settings_seq != seq { return; }
            match result {
                Ok(chats) => this.settings_archived = Some(chats),
                Err(error) => this.settings_error = Some(error.message),
            }
        });
    }

    fn render_settings(&mut self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        if !self.settings_open { return None; }
        let mut list = div().id("archived-settings-list").flex_1().min_h_0().overflow_y_scroll().flex().flex_col().gap(px(6.));
        if let Some(error) = &self.settings_error {
            list = list.child(div().text_color(danger()).child(error.clone()));
        } else if let Some(chats) = &self.settings_archived {
            if chats.is_empty() { list = list.child("No archived chats."); }
            for chat in chats {
                let restore_id = chat.id.clone();
                let delete_id = chat.id.clone();
                let delete_title = chat.title.clone();
                let pid = chat.project_id.clone();
                let project = self.projects.iter().find(|p| p.id == pid).map(|p| p.name.clone()).unwrap_or_default();
                list = list.child(div().flex().items_center().gap(px(8.)).py(px(8.)).border_b_1().border_color(border())
                    .child(div().flex_1().min_w_0().child(ui::nav_title(chat.title.clone()))
                        .child(div().text_size(fs_12()).text_color(text_subtle()).child(project)))
                    .child(ui::nav(SharedString::from(format!("settings-restore-{}", chat.id)), false).w(px(76.)).child("Restore")
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_chat_archived(restore_id.clone(), false, cx))))
                    .child(ui::nav(SharedString::from(format!("settings-delete-{}", chat.id)), false).w(px(76.)).text_color(danger()).child("Delete…")
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.open_deletion(false, delete_id.clone(), delete_title.clone(), cx)))));
            }
        } else { list = list.child("Loading archived chats…"); }
        Some(div().id("settings-overlay").absolute().inset_0().flex().items_center().justify_center().bg(gpui::rgba(0x0000_0099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
            .child(div().w(px(620.)).max_w_full().h(px(480.)).max_h_full().p(px(20.)).rounded(px(R_12))
                .bg(bg_raised()).border_1().border_color(border_strong()).shadow_lg().flex().flex_col().gap(px(16.))
                .child(div().flex().items_center().justify_between().child(div().text_size(fs_16()).text_color(text_strong()).child("Settings"))
                    .child(ui::icon_btn("settings-close", "x", false).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.settings_open = false;
                        cx.notify();
                    }))))
                .child(div().flex().gap(px(8.))
                    .child(ui::nav("settings-archived-tab", !self.settings_memory).child("Archived chats")
                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                            this.settings_memory = false;
                            this.load_settings_archived(cx);
                            cx.notify();
                        })))
                    .child(ui::nav("settings-memory-tab", self.settings_memory).child("Memory")
                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                            this.settings_memory = true;
                            this.load_memory(cx);
                            cx.notify();
                        }))))
                .child(if self.settings_memory { self.render_memory(cx) } else { list.into_any_element() })).into_any_element())
    }

    fn render_reset_dialog(&mut self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        self.reset.as_ref()?;
        let content = self.render_reset(cx);
        Some(div().id("reset-overlay").absolute().inset_0().flex().items_center().justify_center().bg(gpui::rgba(0x0000_0099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
            .child(div().id("reset-dialog").w(px(520.)).max_w_full().max_h_full().overflow_y_scroll().py(px(16.))
                .rounded(px(R_12)).bg(bg_raised()).border_1().border_color(border_strong()).shadow_lg()
                .child(content)).into_any_element())
    }

    /// `.opt-menu`: top 48, right 48, min 220, raised, strong border, radius 12.
    fn render_options_menu(&mut self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        if !self.options_menu {
            return None;
        }
        let archived = self.chat.as_ref().map(|c| c.archived).unwrap_or(false);
        Some(
            div()
                .absolute()
                .top(px(48.))
                .right(px(48.))
                .min_w(px(220.))
                .p(px(6.))
                .rounded(px(R_12))
                .bg(bg_raised())
                .border_1()
                .border_color(border_strong())
                .shadow_lg()
                .flex()
                .flex_col()
                .text_size(fs_13())
                .child(
                    div()
                        .id("opt-archive")
                        .px(px(10.))
                        .py(px(7.))
                        .rounded(px(R_8))
                        .cursor_pointer()
                        .text_color(text_strong())
                        .hover(|s| s.bg(bg_selected()))
                        .child(if archived { "Restore chat" } else { "Archive chat" })
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_archived(!archived, cx))),
                )
                .child(
                    div()
                        .id("opt-delete")
                        .px(px(10.))
                        .py(px(7.))
                        .rounded(px(R_8))
                        .cursor_pointer()
                        .text_color(text_strong())
                        .hover(|s| s.bg(bg_selected()))
                        .child("Delete chat…")
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                            if let Some(chat) = this.chat.clone() {
                                this.open_deletion(false, chat.id, chat.title, cx);
                            }
                        })),
                )
                .into_any_element(),
        )
    }

    /// The right sidebar: Changes and Sources only (Freddy, 2026-11-07), laid
    /// out like his reference card: 20 pt side padding, 30 pt rows, 13 pt text.
    /// The header's panel button shows and hides it.
    fn render_right_panel(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_else(|| "Umbrous".into());
        let title_row = |title: SharedString, button: gpui::Stateful<gpui::Div>| {
            div().flex().items_center().justify_between().h(px(30.)).px(px(20.))
                .text_size(fs_13()).text_color(text_default()).child(title).child(button)
        };
        let workspace = self.access.as_ref().and_then(|a| a.workspace.as_ref());
        let path = workspace.map(|w| w.root.clone()).filter(|p| !p.is_empty());
        let mut context = div().px(px(20.)).py(px(12.)).flex().flex_col().gap(px(12.))
            .child(div().text_color(text_strong()).child("Workspace"))
            .child(div().text_size(fs_12()).child(path.clone().unwrap_or_else(|| "No folder connected. Describe the project and Umbrous will look for it.".into())))
            .child(div().text_size(fs_12()).child(format!("Access: {}", self.access_pill_label())))
            .child(div().text_size(fs_12()).child(format!("Model: {}", self.selected_model_label())));
        if let Some(path) = path {
            context = context.child(ui::pill("reveal-workspace").child("Show folder")
                .on_click(move |_, _, cx| cx.reveal_path(std::path::Path::new(&path))));
        }
        let status = if self.approval.is_some() { "Waiting for your approval" }
            else if self.run.is_some() { "Working — you can switch chats" }
            else { "Ready" };
        context = context.child(div().text_color(text_strong()).child("Activity"))
            .child(div().text_size(fs_12()).child(status));
        for background in &self.background_runs {
            let id = background.run.chat_id.clone();
            let title = background.chat.as_ref().map(|c| c.title.clone()).unwrap_or_else(|| "New chat".into());
            let state = if background.approval.is_some() { "Approval needed" } else { "Working" };
            context = context.child(ui::nav(SharedString::from(format!("activity-{id}")), false)
                .child(ui::nav_title(title)).child(ui::nav_count(state))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.open_chat(id.clone(), cx))));
        }
        let mut menu = None;
        if self.card_menu {
            let item = |id: &'static str, label: &'static str| {
                div().id(id).px(px(10.)).py(px(7.)).rounded(px(R_8)).cursor_pointer().text_size(fs_13()).text_color(text_strong()).hover(|s| s.bg(bg_selected())).child(label)
            };
            menu = Some(
                div()
                    .absolute()
                    .top(px(34.))
                    .right(px(14.))
                    .min_w(px(200.))
                    .p(px(6.))
                    .rounded(px(R_12))
                    .bg(bg_raised())
                    .border_1()
                    .border_color(border_strong())
                    .shadow_lg()
                    .flex()
                    .flex_col()
                    .child(item("card-folder", "Show data folder").on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        if let Some(h) = logfile::umbrous_home() {
                            cx.reveal_path(&h);
                        }
                        this.card_menu = false;
                        cx.notify();
                    }))),
            );
        }
        ui::panel()
            .id("right-panel")
            .relative()
            .w(px(300.))
            .flex_none()
            .h_full()
            .pt(px(12.))
            .child(title_row(project.into(), ui::icon_btn("card-more", "more", self.card_menu).on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                this.card_menu = !this.card_menu;
                cx.notify();
            }))))
            .child(context)
            .children(menu)
            .into_any_element()
    }

    /// Settings → Memory: personal memory and this project's memory,
    /// two separate stores. Click an item to edit it; × twice deletes it; + adds.
    /// Replies list the ids of the items they were given in their meta line.
    fn render_memory(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let title = |id: &'static str, label: String, button: Option<gpui::Stateful<gpui::Div>>| {
            div()
                .id(id)
                .flex()
                .items_center()
                .justify_between()
                .h(px(30.))
                .px(px(20.))
                .text_size(fs_13())
                .text_color(text_default())
                .child(label)
                .children(button.map(|b| b.mr(px(-6.))))
        };
        let note = |text: String| div().px(px(20.)).py(px(3.)).text_size(fs_12()).text_color(text_subtle()).child(text);
        let mut col = div().id("memory").flex().flex_col().flex_1().min_h_0().overflow_y_scroll().pb(px(12.));
        let on = self.use_memory;
        col = col.child(title("mem-title", "Memory".into(), None)).child(
            div()
                .id("mem-use")
                .group("mem-use")
                .flex()
                .items_center()
                .gap(px(14.))
                .h(px(30.))
                .px(px(20.))
                .cursor_pointer()
                .child(ui::hover_icon(if on { "check" } else { "x" }, &"mem-use".into(), if on { success() } else { text_subtle() }, text_strong()))
                .child(div().text_size(fs_13()).text_color(text_default()).group_hover("mem-use", |s| s.text_color(text_strong()))
                    .child(if on { "Used in replies" } else { "Off for the next replies" }))
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                    this.use_memory = !this.use_memory;
                    logfile::event("ui.memory_toggle", &[("on", this.use_memory.to_string())]);
                    cx.notify();
                })),
        );
        if self.reset.is_some() {
            return col.into_any_element();
        }
        let Some(view) = self.memory.clone() else {
            return col.child(note(self.memory_error.clone().unwrap_or_else(|| "Loading…".into()))).into_any_element();
        };
        let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_default();
        // M7: suggestions from tasks, waiting for you. Not in memory, never
        // retrieved, until you accept one.
        if !view.candidates.is_empty() {
            col = col.child(title("mem-h-review", format!("Waiting for review ({})", view.candidates.len()), None))
                .child(note("Suggested after tasks. Click the text to edit it, then Enter accepts.".into()));
            for c in view.candidates.iter() {
                if matches!(&self.memory_input, Some((_, MemTarget::Accept(id))) if *id == c.id) {
                    col = col.child(self.memory_field());
                    continue;
                }
                let gid = SharedString::from(format!("cand-{}", c.id));
                let (id_edit, id_ok, id_no) = (c.id.clone(), c.id.clone(), c.id.clone());
                let button = |id: String, label: &'static str| {
                    div()
                        .id(gpui::ElementId::Name(id.into()))
                        .flex_none()
                        .h(px(22.))
                        .px(px(8.))
                        .flex()
                        .items_center()
                        .rounded(px(R_6))
                        .border_1()
                        .border_color(border_strong())
                        .cursor_pointer()
                        .text_size(fs_12())
                        .text_color(text_strong())
                        .hover(|s| s.bg(bg_hover()))
                        .child(label)
                };
                col = col.child(
                    div()
                        .id(gpui::ElementId::Name(gid.clone()))
                        .flex()
                        .flex_col()
                        .gap(px(2.))
                        .px(px(20.))
                        .py(px(5.))
                        .child(
                            div()
                                .id(gpui::ElementId::Name(format!("cand-{}-text", c.id).into()))
                                .cursor_pointer()
                                .text_size(fs_13())
                                .line_height(px(18.))
                                .text_color(text_strong())
                                .child(c.text.clone())
                                .on_click(cx.listener(move |this, _: &ClickEvent, window, cx| {
                                    this.begin_memory_input(MemTarget::Accept(id_edit.clone()), window, cx);
                                })),
                        )
                        .child(div().text_size(fs_12()).text_color(text_subtle()).child(format!("→ {}", c.destination(&project))))
                        .child(div().text_size(fs_12()).text_color(text_subtle()).child(c.source()))
                        .child(
                            div()
                                .flex()
                                .gap(px(6.))
                                .pt(px(3.))
                                .child(button(format!("cand-{}-accept", c.id), "Accept").on_click(cx.listener(
                                    move |this, _: &ClickEvent, _, cx| this.accept_candidate(id_ok.clone(), None, cx))))
                                .child(button(format!("cand-{}-reject", c.id), "Reject").on_click(cx.listener(
                                    move |this, _: &ClickEvent, _, cx| this.reject_candidate(id_no.clone(), cx)))),
                        ),
                );
            }
        }
        let sections: [(&'static str, &'static str, String, &Vec<api::MemoryItem>); 2] = [
            ("personal", "mem-add-personal", "Personal".to_string(), &view.personal),
            ("project", "mem-add-project", format!("This project: {project}"), &view.project),
        ];
        for (store, add_id, label, items) in sections {
            let plus = ui::icon_btn(add_id, "plus", false).on_click(cx.listener(move |this, _: &ClickEvent, window, cx| {
                this.begin_memory_input(MemTarget::Add(store), window, cx);
            }));
            col = col.child(title(if store == "personal" { "mem-h-personal" } else { "mem-h-project" }, label, Some(plus)));
            if items.is_empty() {
                col = col.child(note("Nothing saved.".into()));
            }
            for m in items.iter() {
                let editing = matches!(&self.memory_input, Some((_, MemTarget::Edit(id))) if *id == m.id);
                if editing {
                    col = col.child(self.memory_field());
                    continue;
                }
                let armed = self.memory_armed.as_deref() == Some(m.id.as_str());
                let off = m.applies == Some(false);
                let gid = SharedString::from(format!("mem-{}", m.id));
                let (id_edit, id_del) = (m.id.clone(), m.id.clone());
                let mut tag = format!("{} · {}", m.id, m.kind);
                if off {
                    tag.push_str(" · not for this project");
                }
                col = col.child(
                    div()
                        .id(gpui::ElementId::Name(gid.clone()))
                        .group(gid.clone())
                        .flex()
                        .items_start()
                        .gap(px(8.))
                        .px(px(20.))
                        .py(px(5.))
                        .child(
                            div()
                                .id(gpui::ElementId::Name(format!("mem-{}-text", m.id).into()))
                                .flex_1()
                                .min_w_0()
                                .cursor_pointer()
                                .child(div().text_size(fs_13()).line_height(px(18.))
                                    .text_color(if off { text_subtle() } else { text_strong() }).child(m.text.clone()))
                                .child(div().text_size(fs_12()).text_color(text_subtle()).overflow_hidden().whitespace_nowrap().text_ellipsis().child(tag))
                                .on_click(cx.listener(move |this, _: &ClickEvent, window, cx| {
                                    this.begin_memory_input(MemTarget::Edit(id_edit.clone()), window, cx);
                                })),
                        )
                        .child(
                            div()
                                .id(gpui::ElementId::Name(format!("mem-{}-del", m.id).into()))
                                .flex_none()
                                .h(px(20.))
                                .px(px(4.))
                                .flex()
                                .items_center()
                                .rounded(px(R_6))
                                .cursor_pointer()
                                .text_size(fs_12())
                                .when(armed, |d| d.text_color(danger()).child("Delete?"))
                                .when(!armed, |d| d.child(ui::hover_icon("x", &gid, text_subtle(), text_strong())))
                                .hover(|s| s.bg(bg_hover()))
                                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                                    if this.memory_armed.as_deref() == Some(id_del.as_str()) {
                                        this.delete_memory(id_del.clone(), cx);
                                    } else {
                                        this.memory_armed = Some(id_del.clone());
                                        this.memory_input = None;
                                    }
                                    cx.notify();
                                })),
                        ),
                );
            }
            if matches!(&self.memory_input, Some((_, MemTarget::Add(s))) if *s == store) {
                col = col.child(self.memory_field());
            }
        }
        if let Some(err) = &self.memory_error {
            col = col.child(div().px(px(20.)).py(px(4.)).text_size(fs_12()).text_color(danger()).child(err.clone()));
        }
        col = col.child(note("Off or deleted means not retrieved again. Earlier replies that repeat an item still reach the model as chat history; compare in a new chat.".into()));
        // M8
        col = col.child(title("reset-title", "Start fresh".into(), None))
            .child(note("Delete chosen chats and memory, after seeing exactly what goes and what stays.".into()))
            .child(div().px(px(20.)).pt(px(3.)).child(Self::small_button("reset-open", "Start fresh…".into(), false)
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.open_reset(None, cx)))));
        col.into_any_element()
    }

    fn small_button(id: impl Into<SharedString>, label: SharedString, danger_: bool) -> gpui::Stateful<gpui::Div> {
        use theme::*;
        div()
            .id(gpui::ElementId::Name(id.into()))
            .flex_none()
            .h(px(22.))
            .px(px(8.))
            .flex()
            .items_center()
            .rounded(px(R_6))
            .border_1()
            .border_color(if danger_ { danger() } else { border_strong() })
            .cursor_pointer()
            .text_size(fs_12())
            .text_color(if danger_ { danger() } else { text_strong() })
            .hover(|s| s.bg(bg_hover()))
            .child(label)
    }

    /// M8: Start fresh: tick chats and memory, Preview, then the exact list and
    /// what stays, with Delete N items or Cancel.
    fn render_reset(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let Some(ui) = self.reset.clone() else { return div().into_any_element() };
        let note = |text: String| div().px(px(20.)).py(px(3.)).text_size(fs_12()).text_color(text_subtle()).child(text);
        let head = |label: String| div().px(px(20.)).pt(px(10.)).pb(px(2.)).text_size(fs_12()).text_color(text_subtle()).child(label);
        let project = self.projects.iter().find(|p| p.id == ui.choice.project_id).map(|p| p.name.clone()).unwrap_or_default();
        let mut col = div().id("reset").flex().flex_col();
        col = col.child(div().flex().items_center().justify_between().h(px(30.)).px(px(20.)).text_size(fs_13())
            .text_color(text_default()).child("Start fresh")
            .child(Self::small_button("reset-close", "Cancel".into(), false)
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.close_reset(cx)))));
        if let Some(done) = &ui.done {
            col = col.child(div().px(px(20.)).py(px(4.)).text_size(fs_13()).text_color(success()).child(done.summary()));
            for k in &done.kept {
                col = col.child(note(format!("Kept: {}", k.text)));
            }
            col = col.child(note(done.backups.clone()));
            return col.into_any_element();
        }
        let tick = |id: String, label: String, on: bool, sub: bool, what: ResetTick, cx: &mut Context<Self>| {
            div()
                .id(gpui::ElementId::Name(id.into()))
                .flex()
                .items_start()
                .gap(px(8.))
                .pl(px(if sub { 38. } else { 20. }))
                .pr(px(20.))
                .py(px(3.))
                .cursor_pointer()
                .child(
                    div().flex_none().mt(px(2.)).size(px(14.)).rounded(px(R_4)).border_1()
                        .border_color(if on { accent() } else { border_strong() })
                        .when(on, |d| d.bg(accent()).flex().items_center().justify_center()
                            .child(ui::icon_sized("check", 10.).text_color(text_strong()))),
                )
                .child(div().flex_1().min_w_0().text_size(fs_13()).line_height(px(18.)).text_color(text_strong()).child(label))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.toggle_reset(what.clone(), cx)))
        };
        let c = &ui.choice;
        if ui.preview.is_some() {
            // The ticks fold away while a preview is shown, so its Delete stays
            // in view and nothing can be ticked behind it. Changing the
            // selection drops the preview; a new one is needed to delete.
            col = col.child(div().px(px(20.)).pt(px(4.)).child(Self::small_button("reset-change", "Change selection".into(), false)
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.change_reset_selection(cx)))));
        } else {
            col = col.child(head(format!("Chats in {project}")));
            match &ui.chats {
                None => col = col.child(note("Loading…".into())),
                Some(chats) if chats.is_empty() => col = col.child(note("No chats.".into())),
                Some(chats) => {
                    col = col.child(tick("reset-all-chats".into(), format!("All chats in {project} ({}, archived too)", chats.len()),
                                         c.all_chats, false, ResetTick::AllChats, cx));
                    for ch in chats {
                        let label = if ch.archived { format!("{} (archived)", ch.title) } else { ch.title.clone() };
                        col = col.child(tick(format!("reset-chat-{}", ch.id), label, c.chats.contains(&ch.id), true,
                                             ResetTick::Chat(ch.id.clone()), cx));
                    }
                }
            }
            if let Some(view) = self.memory.clone().filter(|v| v.project_id.as_deref() == Some(c.project_id.as_str())) {
                col = col.child(head("Personal memory".into()));
                if view.personal.is_empty() {
                    col = col.child(note("Nothing saved.".into()));
                } else {
                    col = col.child(tick("reset-all-personal".into(), format!("All personal memory ({}, every project)", view.personal.len()),
                                         c.all_personal, false, ResetTick::AllPersonal, cx));
                }
                for m in &view.personal {
                    col = col.child(tick(format!("reset-p-{}", m.id), m.text.clone(), c.personal.contains(&m.id), true,
                                         ResetTick::Personal(m.id.clone()), cx));
                }
                col = col.child(head(format!("Memory of {project}")));
                if view.project.is_empty() {
                    col = col.child(note("Nothing saved.".into()));
                } else {
                    col = col.child(tick("reset-all-project".into(), format!("All of {project}'s memory ({})", view.project.len()),
                                         c.all_project, false, ResetTick::AllProject, cx));
                }
                for m in &view.project {
                    col = col.child(tick(format!("reset-m-{}", m.id), m.text.clone(), c.project.contains(&m.id), true,
                                         ResetTick::Project(m.id.clone()), cx));
                }
            }
            col = col.child(div().pt(px(8.)).child(tick("reset-fresh".into(), "Start a new chat afterwards".into(), c.start_fresh, false,
                                                         ResetTick::StartFresh, cx)));
            col = col.child(div().px(px(20.)).pt(px(6.)).child(Self::small_button("reset-preview", "Preview".into(), false)
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.preview_reset(cx)))));
        }
        if let Some(err) = &ui.error {
            col = col.child(div().px(px(20.)).py(px(4.)).text_size(fs_12()).text_color(danger()).child(err.clone()));
        }
        if ui.working {
            col = col.child(note("Asking the runtime…".into()));
        }
        if let Some(p) = &ui.preview {
            let mut box_ = div().id("reset-preview").mx(px(12.)).mt(px(8.)).p(px(8.)).rounded(px(R_8)).border_1()
                .border_color(danger()).flex().flex_col();
            if let Some(w) = &ui.warning {
                box_ = box_.child(div().py(px(3.)).text_size(fs_12()).text_color(warning()).child(w.clone()));
            }
            if p.empty {
                box_ = box_.child(div().text_size(fs_12()).text_color(text_subtle()).child("The selection matches nothing, so there is nothing to delete."));
            } else {
                let n = p.items.len();
                box_ = box_.child(div().text_size(fs_12()).text_color(text_subtle()).pb(px(2.))
                    .child(format!("This deletes exactly {n} item{}:", if n == 1 { "" } else { "s" })));
                for i in &p.items {
                    box_ = box_.child(div().py(px(2.)).child(div().text_size(fs_13()).text_color(text_strong()).child(i.label()))
                        .child(div().text_size(fs_12()).text_color(text_subtle()).child(i.tag())));
                }
                if !p.blocked.is_empty() {
                    box_ = box_.child(div().py(px(2.)).text_size(fs_12()).text_color(danger())
                        .child("A reply is running in a selected chat. Stop it or let it finish, then preview again."));
                }
                let mut row = div().flex().gap(px(6.)).pt(px(6.));
                if p.blocked.is_empty() {
                    row = row.child(Self::small_button("reset-confirm", format!("Delete {n} item{}", if n == 1 { "" } else { "s" }).into(), true)
                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.confirm_reset(cx))));
                }
                row = row.child(Self::small_button("reset-cancel", "Cancel".into(), false)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.close_reset(cx))));
                box_ = box_.child(row);
                // What stays comes after Delete, so a short list keeps Delete in
                // view at the default window size; it is the same text as before.
                box_ = box_.child(div().pt(px(6.)).text_size(fs_12()).text_color(text_subtle()).child("What stays"));
                for k in &p.kept {
                    box_ = box_.child(div().py(px(2.)).text_size(fs_12()).text_color(text_subtle()).child(k.text.clone()));
                }
                box_ = box_.child(div().py(px(2.)).text_size(fs_12()).text_color(text_subtle()).child(p.not_touched.clone()))
                    .child(div().py(px(2.)).text_size(fs_12()).text_color(text_subtle()).child(p.backups.clone()));
            }
            col = col.child(box_);
        }
        col.into_any_element()
    }

    /// The inline field for adding or editing a memory item (like the new-project field).
    fn memory_field(&self) -> AnyElement {
        use theme::*;
        let Some((input, _)) = &self.memory_input else { return div().into_any_element() };
        div()
            .mx(px(20.))
            .my(px(4.))
            .h(px(32.))
            .px(px(10.))
            .flex()
            .items_center()
            .border_1()
            .border_color(border_strong())
            .rounded(px(R_8))
            .bg(bg_raised())
            .text_size(fs_13())
            .child(input.clone())
            .into_any_element()
    }

    /// `.thread`: max 760, padding 48 32 32. Notices sit in the thread like the page's.
    fn render_transcript(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let archived = self.chat.as_ref().map(|c| c.archived).unwrap_or(false);
        let mut notices: Vec<AnyElement> = vec![];
        if self.msgs.is_empty() && !self.loading_chat {
            notices.push(div().flex().flex_col().gap(px(12.))
                .child(div().text_size(px(28.)).text_color(text_strong()).child("What would you like to work on?"))
                .child(div().text_size(fs_14()).child("Ask a question, describe a change, or name a project. Umbrous will find the workspace when needed."))
                .into_any_element());
        }
        if !archived && self.project_id.is_some() && self.access.as_ref().map(|a| a.workspace.is_none()).unwrap_or(false) {
            notices.push(
                ui::notice(None)
                    .child(div().flex_1().min_w_0().child("No folder selected yet. Tell me which project to work on and I’ll look for it, or connect a folder."))
                    .child(ui::pill("connect-workspace").child("Connect folder")
                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.choose_level("code_ask_commands", cx))))
                    .into_any_element(),
            );
        }
        if archived {
            notices.push(
                ui::notice(Some(ui::NoticeKind::Warn))
                    .child(div().flex_1().min_w_0().child("This chat is archived. All its messages are kept and shown here read-only. Restore it to continue."))
                    .child(ui::pill("restore").child("Restore").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.set_archived(false, cx))))
                    .into_any_element(),
            );
        }
        if let (Some(it), false) = (self.interrupted.clone(), archived) {
            let can = self.can_send();
            notices.push(
                ui::notice(Some(ui::NoticeKind::Warn))
                    .child(
                        div().flex_1().min_w_0()
                            .child(div().child("Task interrupted: Umbrous stopped while this task was running, so its outcome is unknown. Nothing was replayed."))
                            .child(div().mt(px(4.)).text_color(text_subtle()).child(format!("Your message: “{}”", it.text.chars().take(200).collect::<String>())))
                            .when(!it.report.is_empty(), |d| d.child(div().mt(px(4.)).text_color(text_subtle()).child(it.report.clone())))
                            .child(div().mt(px(4.)).text_color(text_subtle()).child(it.note.clone())),
                    )
                    .when(can, |d| {
                        d.child(ui::pill("resume-task").child("Resume").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.resume_task(cx))))
                    })
                    .into_any_element(),
            );
        }
        if let Some((text, kind, retry)) = self.notice.clone() {
            let k = match kind {
                MetaKind::Bad => Some(ui::NoticeKind::Bad),
                MetaKind::Warn => Some(ui::NoticeKind::Warn),
                MetaKind::Normal => None,
            };
            notices.push(
                ui::notice(k)
                    .child(div().flex_1().min_w_0().child(text))
                    .when(retry, |d| {
                        d.child(ui::pill("retry-ollama").child("Retry").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.load_models(false, cx))))
                    })
                    .into_any_element(),
            );
        }
        if let Some(card) = self.render_approval(cx) {
            notices.push(card);
        }
        let notice_col = |notices: Vec<AnyElement>, top: f32| {
            // block layout, not a centring flex row: a flex row measured the notices' text
            // without a definite width (one line per text), so a wrapping notice was given
            // too little height and ran on under the composer
            div()
                .w_full()
                .px(px(SP_32))
                .pt(px(top))
                .child(div().w_full().max_w(px(696.)).mx_auto().flex().flex_col().gap(px(SP_16)).children(notices))
        };
        if self.msgs.is_empty() {
            return div().flex_1().min_h_0().flex().flex_col().justify_end().pb(px(SP_24)).child(notice_col(notices, SP_24)).into_any_element();
        }
        let host = cx.entity();
        let state = self.list.clone();
        // the list's width at the last layout (read here: the list is borrowed while it renders rows)
        let list_w = self.list.viewport_bounds().size.width;
        // w_full: with an auto width here the notices' text was measured without a
        // definite width (one line per text), so a wrapping notice got too little height
        div()
            .flex_1()
            .min_h_0()
            .w_full()
            .flex()
            .flex_col()
            .child(
                list(state, move |ix, window, cx| {
                    let host2 = host.clone();
                    let ws = host.read(cx);
                    match ws.rows.get(ix).and_then(|r| ws.msgs.get(r.msg).map(|m| (*r, m))) {
                        Some((row, m)) => transcript::render_row(&host2, row, m, &ws.selection, ix == 0, window, list_w),
                        None => div().into_any_element(),
                    }
                })
                .flex_1()
                .min_h_0()
                .w_full(),
            )
            // the notices take their full height and the list gets what is left, so a
            // long notice (M5's interrupted task) is never drawn under the composer
            .when(!notices.is_empty(), |d| d.child(notice_col(notices, SP_8).flex_none()))
            .into_any_element()
    }

    /// `.composer-dock` + `.composer`: raised box, strong border, radius 12, padding 12.
    fn render_composer(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let up = self.provider_up();
        let pill_label = match (&self.models, up) {
            (None, _) => "Checking models…".to_string(),
            (Some(_), false) => if self.model.as_deref().map(|m| m.starts_with("codex/")).unwrap_or(false) { "ChatGPT unavailable" } else { "Ollama not running" }.to_string(),
            (Some(m), true) if m.models.is_empty() => "No models available".to_string(),
            (Some(_), true) => self.selected_model_label(),
        };
        let running = self.run.is_some();
        let can_send = self.can_send() && !self.composer.read(cx).text().trim().is_empty();
        // the pill turns warning-coloured while Ollama is down; otherwise `.pill` colours
        let ollama_down = !up && self.models.is_some();
        let pill_color = if ollama_down { warning() } else { text_default() };
        let pill_hover = if ollama_down { warning() } else { text_strong() };

        let menu = self.render_model_menu(cx);

        // `.send`: 28px circle; strong when enabled, selected/subtle when disabled
        let active = can_send || running;
        let send = div()
            .id("send")
            .ml_auto()
            .size(px(28.))
            .rounded_full()
            .flex()
            .items_center()
            .justify_center()
            .bg(if active { text_strong() } else { bg_selected() })
            .when(active, |d| d.cursor_pointer())
            .child(ui::icon(if running { "stop" } else { "arrow-up" }).text_color(if active { bg_app() } else { text_subtle() }))
            .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                if this.run.is_some() {
                    this.stop_reply(cx);
                } else {
                    let text = this.composer.read(cx).text().to_string();
                    this.send(text, window, cx);
                }
            }));

        div()
            .flex_none()
            .px(px(SP_24))
            .pb(px(SP_24))
            .flex()
            .justify_center()
            .child(
                div()
                    .w_full()
                    .max_w(px(760.))
                    .border_1()
                    .border_color(border_strong())
                    .bg(bg_raised())
                    .rounded(px(R_12))
                    .p(px(12.))
                    // block, not a flex column: see composer.rs (the text must be measured
                    // at the box's width, or wrapped lines spill over the bar)
                    .child(div().w_full().min_h(px(40.)).child(self.composer.clone()))
                    .child(
                        // `.bar`
                        div()
                            .mt(px(4.))
                            .relative()
                            .flex()
                            .items_center()
                            .gap(px(4.))

                            // this chat's access, on the left like Codex's permission pill
                            .child(self.render_access_control(cx))
                            .child(
                                ui::pill("model-pill")
                                    .when(ollama_down, |d| d.text_color(warning()).hover(|s| s.bg(bg_hover()).text_color(warning())))
                                    .child(ui::hover_icon("cpu", &"model-pill".into(), pill_color, pill_hover))
                                    .child(pill_label)
                                    .child(ui::hover_icon("chevron-down", &"model-pill".into(), pill_color, pill_hover))
                                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                        this.access_menu = false;
                                        if this.model_menu {
                                            this.model_menu = false;
                                            cx.notify();
                                        } else {
                                            this.load_models(true, cx);
                                        }
                                    })),
                            )
                            .children(self.render_model_controls(cx))
                            .child(send)
                            .children(menu),
                    ),
            )
            .into_any_element()
    }

    /// Starting / failed: centred like the page's `.empty` state.
    fn render_status(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let (title, message, detail, failed, code) = match &self.phase {
            Phase::Starting => ("Starting Umbrous…".to_string(), "Starting the local runtime on this Mac.".to_string(), vec![], false, String::new()),
            Phase::Failed(f) => (f.title.clone(), f.message.clone(), f.detail.clone(), true, f.code.clone()),
            Phase::Ready => unreachable!(),
        };
        let detail: Vec<String> = detail.into_iter().filter(|l| !l.trim().is_empty()).collect();
        let mut col = div()
            .w(px(560.))
            .flex()
            .flex_col()
            .items_center()
            .child(ui::icon_sized(if failed { "x" } else { "logo" }, 24.).text_color(if failed { danger() } else { text_strong() }))
            .child(div().mt(px(16.)).mb(px(4.)).text_size(fs_18()).font_weight(FontWeight::MEDIUM).text_color(text_strong()).child(title))
            .child(div().text_size(fs_13()).text_color(text_subtle()).text_center().child(message));
        if !detail.is_empty() {
            col = col.child(
                div()
                    .id("detail")
                    .mt(px(SP_16))
                    .w_full()
                    .max_h(px(220.))
                    .overflow_y_scroll()
                    .px(px(12.))
                    .py(px(8.))
                    .rounded(px(R_8))
                    .bg(bg_raised())
                    .border_1()
                    .border_color(border())
                    .font_family(MONO)
                    .text_size(fs_12())
                    .text_color(text_default())
                    .child(detail.join("\n")),
            );
        }
        if failed {
            col = col.child(
                div()
                    .mt(px(SP_16))
                    .flex()
                    .gap(px(4.))
                    .child(ui::pill("retry").child(if code == "crashed" { "Restart" } else { "Retry" }).on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.retry(cx))))
                    .child(ui::pill("logs").child("Show logs").on_click(|_, _, cx| {
                        if let Some(d) = logfile::log_dir() {
                            cx.reveal_path(&d);
                        }
                    }))
                    .child(ui::pill("quit").child("Quit").on_click(|_, _, cx| cx.quit())),
            );
        }
        ui::panel().flex_1().h_full().items_center().justify_center().child(col).into_any_element()
    }
}

/// Height of the strip that holds the macOS window controls (see docs/desktop.md).
pub const TITLEBAR: f32 = 28.;

impl Render for Workspace {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        use theme::*;
        let t0 = Instant::now();
        let wide = window.viewport_size().width >= px(1180.); // the page's @media (max-width:1180px)
        // reveal_end scrolls with the heights the list measured last frame, so a row that
        // just grew (a reply's closing meta line) can end up below the view. Only after
        // our own reveal, and for a few frames at most, scroll again once it is measured;
        // any scroll input clears this before it can undo the gesture.
        let left = self.reveal_frames.get();
        if left > 0 {
            match self.rows.len().checked_sub(1).map(|last| (last, self.list.bounds_for_item(last))) {
                Some((_, Some(b))) if b.bottom() <= self.list.viewport_bounds().bottom() + px(0.5) => self.reveal_frames.set(0),
                Some((last, _)) => {
                    self.list.scroll_to_reveal_item(last);
                    self.reveal_frames.set(left - 1);
                    window.request_animation_frame();
                }
                None => self.reveal_frames.set(0),
            }
        }
        let root = div()
            .id("workspace")
            .key_context("Workspace")
            .track_focus(&self.focus)
            .on_action(cx.listener(Self::copy))
            .on_action(cx.listener(Self::select_all))
            .on_action(cx.listener(Self::escape))
            .on_action(cx.listener(|this, _: &NewChat, window, cx| {
                this.new_chat(cx);
                window.focus(&this.composer.focus_handle(cx));
            }))
            .on_action(cx.listener(|this, _: &StopReply, _, cx| this.stop_reply(cx)))
            .on_action(cx.listener(|this, _: &ToggleSidebar, _, cx| {
                this.sidebar = !this.sidebar;
                cx.notify();
            }))
            .on_action(cx.listener(|this, _: &ToggleDetails, _, cx| {
                this.details = !this.details;
                cx.notify();
            }))
            .on_action(cx.listener(|this, _: &ReloadModels, _, cx| this.load_models(false, cx)))
            .on_mouse_up(MouseButton::Left, cx.listener(|this, _, _, _| this.selection.dragging = false))
            .on_mouse_up_out(MouseButton::Left, cx.listener(|this, _, _, _| this.selection.dragging = false))
            .size_full()
            .flex()
            .flex_col()
            // body: canvas background, 13px/1.5 default text, SF Pro
            .bg(bg_canvas())
            .text_color(text_default())
            .font_family(FONT)
            .text_size(fs_13())
            .line_height(px(19.5))
            // the window controls' strip (the browser drew its own title bar here)
            .child(div().h(px(TITLEBAR)).flex_none().w_full());

        if matches!(self.phase, Phase::Ready) && !self.projects.is_empty() && !self.logged_first_frame {
            self.logged_first_frame = true;
            logfile::event("ui.first_full_frame", &[("ms_since_start", self.started.elapsed().as_millis().to_string())]);
        }
        // `.app`: three panels, gap 6, padding 6 (top padding is the strip above)
        let app = div().flex_1().min_h_0().flex().gap(px(6.)).px(px(6.)).pb(px(6.));
        let app = if !matches!(self.phase, Phase::Ready) {
            app.child(self.render_status(cx))
        } else {
            let main = ui::panel()
                .flex_1()
                .min_w_0()
                .h_full()
                .relative()
                .child(self.render_header(cx))
                .child(self.render_transcript(cx))
                .child(self.render_composer(cx))
                .when(self.msgs.is_empty() && !self.loading_chat, |d| d.child(div().flex_1().min_h_0()))
                .children(self.render_options_menu(cx));
            app.when(self.sidebar, |d| d.child(self.render_sidebar(cx)))
                .child(main)
                .when(self.details && wide, |d| d.child(self.render_right_panel(cx)))
        };
        let confirm = self.render_full_access_confirm(cx);
        let settings = self.render_settings(cx);
        let reset = self.render_reset_dialog(cx);
        let deletion = self.render_deletion(cx);
        let el = root.relative().child(app).children(settings).children(reset).children(deletion).children(confirm);
        let ms = t0.elapsed().as_secs_f64() * 1000.0;
        self.perf.render_count += 1;
        self.perf.render_ms_total += ms;
        self.perf.render_ms_max = self.perf.render_ms_max.max(ms);
        el
    }
}
