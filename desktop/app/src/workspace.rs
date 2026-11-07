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

const STREAM_BATCH: Duration = Duration::from_millis(33);

pub enum Phase {
    Starting,
    Failed(Failure),
    Ready,
}

struct Run {
    /// unique per send; stream events carry it, and only the current run's are applied
    token: u64,
    /// index of the reply message in `msgs` (valid while `view` is unchanged)
    msg_ix: usize,
    chat_id: String,
    run_id: Option<String>,
    stop: Arc<AtomicBool>,
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
    pub chat: Option<api::ChatInfo>,
    pub msgs: Vec<Msg>,
    pub list: ListState,
    /// keep the newest text in view while a reply streams; off once you scroll up
    follow: std::rc::Rc<std::cell::Cell<bool>>,
    /// list rows (see transcript::Row) and each message's row range
    rows: Vec<Row>,
    msg_rows: Vec<std::ops::Range<usize>>,
    selection: Selection,
    pub models: Option<api::Models>,
    pub model: Option<String>,
    model_menu: bool,
    options_menu: bool,
    pub composer: Entity<Composer>,
    project_input: Option<Entity<Composer>>,
    project_error: Option<String>,
    run: Option<Run>,
    notice: Option<(String, MetaKind, bool)>, // text, kind, offers Retry (Ollama)
    /// The selected project's workspace access (M3). This app cannot show approval
    /// requests yet, so a project on Ask before read gets an explanation and an
    /// explicit switch, never a silent change or a request left waiting.
    pub access: Option<api::Access>,
    access_seq: u64,
    pub access_error: Option<String>,
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
        let f = follow.clone();
        list.set_scroll_handler(move |ev, _, _| f.set(ev.visible_range.end >= ev.count));
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
            chat: None,
            msgs: vec![],
            list,
            follow,
            rows: vec![],
            msg_rows: vec![],
            selection: Selection::default(),
            models: None,
            model: None,
            model_menu: false,
            options_menu: false,
            composer,
            project_input: None,
            project_error: None,
            run: None,
            notice: None,
            access: None,
            access_seq: 0,
            access_error: None,
            sidebar: true,
            details: true,
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
                    this.project_id = id;
                }
                this.load_chats(cx);
            }
        });
    }

    pub fn load_chats(&mut self, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        self.load_access(cx);
        let show_archived = self.show_archived;
        self.chats_seq += 1;
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
                    // keep the open chat's title and state in step with the list
                    if let Some(cur) = this.chat.as_mut() {
                        if let Some(fresh) = a.iter().chain(b.iter()).find(|c| c.id == cur.id) {
                            *cur = fresh.clone();
                        }
                    }
                    this.chats = a;
                    this.archived = b;
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

    pub fn asks_before_read(&self) -> bool {
        self.access
            .as_ref()
            .filter(|a| self.project_id.as_deref() == Some(a.project_id.as_str()))
            .and_then(|a| a.workspace.as_ref())
            .map(|w| w.profile == "ask_before_read")
            .unwrap_or(false)
    }

    /// The explicit way out of Ask before read in this app: you press the button,
    /// the runtime changes the profile, and the notice goes away once it has.
    pub fn switch_to_workspace_read(&mut self, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        logfile::event("ui.profile_switch", &[("to", "workspace_read".into())]);
        let path = format!("/api/projects/{pid}/profile");
        self.access_error = None;
        self.api_call(cx, move |api| api.post::<api::Access>(&path, json!({"profile": "workspace_read"})), move |this, r, _| {
            match r {
                Ok(a) if this.project_id.as_deref() == Some(a.project_id.as_str()) => {
                    this.access_seq += 1; // a list answer already on its way is older than this
                    this.access = Some(a);
                }
                Ok(_) => {}
                Err(e) => this.access_error = Some(e.message),
            }
        });
    }

    pub fn select_project(&mut self, id: String, cx: &mut Context<Self>) {
        if self.project_id.as_deref() != Some(&id) {
            self.project_id = Some(id);
            self.show_archived = false;
            self.access = None;
            self.access_error = None;
            self.new_chat(cx);
            self.load_chats(cx);
        }
    }

    /// Replace the transcript: stop following any reply that is streaming (the
    /// runtime saves what arrived as "stopped"), and invalidate every pending
    /// stream, parse or transcript response made for the old view.
    fn begin_view(&mut self, cx: &mut Context<Self>) -> u64 {
        self.detach_run(cx);
        self.view += 1;
        self.parse_in_flight = false;
        self.opening = None;
        self.loading_chat = false;
        self.selection = Selection::default();
        self.options_menu = false;
        self.view
    }

    /// Stop a running reply and forget it. Its stream thread ends at its next
    /// event, which closes the connection; the runtime then stops the model
    /// and keeps the partial reply.
    fn detach_run(&mut self, cx: &mut Context<Self>) {
        if let Some(run) = self.run.take() {
            run.stop.store(true, Ordering::SeqCst);
            if let Some(run_id) = run.run_id.clone() {
                self.api_call(cx, move |api| api.post::<Value>("/api/chat/cancel", json!({ "run_id": run_id })), |_, _, _| {});
            }
            logfile::event("ui.run_detached", &[]);
        }
    }

    pub fn new_chat(&mut self, cx: &mut Context<Self>) {
        self.begin_view(cx);
        self.chat = None;
        self.context = None;
        self.msgs.clear();
        self.rebuild_rows();
        self.selection = Selection::default();
        self.notice = None;
        self.options_menu = false;
        self.composer.update(cx, |c, cx| c.set_disabled(false, cx));
        cx.notify();
    }

    pub fn open_chat(&mut self, id: String, cx: &mut Context<Self>) {
        self.open_chat_inner(id, false, cx)
    }

    fn open_chat_inner(&mut self, id: String, force: bool, cx: &mut Context<Self>) {
        // A → B → A: while B is loading, A is still on screen but no longer what
        // you asked for, so clicking A again must open A (and cancel B).
        let showing = self.opening.is_none() && self.run.is_none()
            && self.chat.as_ref().map(|c| c.id == id).unwrap_or(false);
        let already_opening = self.opening.as_deref() == Some(id.as_str());
        if !force && (showing || already_opening) {
            return;
        }
        let view = self.begin_view(cx);
        self.opening = Some(id.clone());
        self.loading_chat = true;
        let t0 = Instant::now();
        // fetch and parse every message off the UI thread
        self.api_call(
            cx,
            move |api| {
                let t: api::Transcript = api.get(&format!("/api/chats/{id}"))?;
                let mut msgs: Vec<Msg> = t.messages.iter().map(stored_msg).collect();
                // M4: replies that were tasks get their report line back
                if t.messages.iter().any(|m| m.task_id.is_some()) {
                    if let Ok(list) = api.get::<api::TaskList>(&format!("/api/tasks?chat_id={id}")) {
                        for (m, msg) in t.messages.iter().zip(msgs.iter_mut()) {
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
                Ok((t.chat, msgs))
            },
            move |this, r, cx| {
                if view != this.view {
                    return; // you went somewhere else meanwhile
                }
                this.loading_chat = false;
                this.opening = None;
                match r {
                    Ok((chat, msgs)) => {
                        let archived = chat.archived;
                        this.chat = Some(chat);
                        this.context = None;
                        this.msgs = msgs;
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
        self.options_menu = false;
        if archive && self.run.is_some() {
            self.stop_reply(cx); // the reply is saved first; the runtime refuses archive while it runs
        }
        let path = format!("/api/chats/{}/{}", chat.id, if archive { "archive" } else { "unarchive" });
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

    // ---------- models ----------
    pub fn load_models(&mut self, open_menu: bool, cx: &mut Context<Self>) {
        self.models_seq += 1;
        let seq = self.models_seq;
        self.api_call(cx, |api| api.get::<api::Models>("/api/models"), move |this, r, _| {
            if seq != this.models_seq {
                return;
            }
            let m = r.unwrap_or_else(|e| api::Models { reachable: false, version: None, error: Some(e.message), models: vec![] });
            if !m.reachable {
                this.notice = Some((m.error.clone().unwrap_or_else(|| "Ollama is not running.".into()), MetaKind::Warn, true));
            } else if matches!(this.notice, Some((_, _, true))) {
                this.notice = None;
            }
            if m.reachable {
                if let Some(sel) = &this.model {
                    if !m.models.iter().any(|x| &x.name == sel) {
                        this.notice = Some((format!("The model {sel} is no longer installed. Pick a model to continue."), MetaKind::Warn, false));
                        this.model = None;
                    }
                }
                if this.model.is_none() && this.notice.is_none() {
                    this.model = m.models.iter().find(|x| x.tool_capable).or(m.models.first()).map(|x| x.name.clone());
                }
            }
            this.models = Some(m);
            if open_menu {
                this.model_menu = true;
            }
        });
    }

    fn ollama_up(&self) -> bool {
        self.models.as_ref().map(|m| m.reachable).unwrap_or(false)
    }

    fn can_send(&self) -> bool {
        matches!(self.phase, Phase::Ready)
            && self.ollama_up()
            && self.model.is_some()
            && self.run.is_none()
            && !self.chat.as_ref().map(|c| c.archived).unwrap_or(false)
    }

    // ---------- sending and streaming ----------
    pub fn send(&mut self, text: String, _window: &mut Window, cx: &mut Context<Self>) {
        if !self.can_send() || text.trim().is_empty() {
            return;
        }
        let model = self.model.clone().unwrap();
        let project_id = self.project_id.clone().unwrap_or_else(|| "prj_general".into());
        let existing = self.chat.as_ref().map(|c| c.id.clone());
        let existing_for_ui = existing.clone();
        self.composer.update(cx, |c, cx| c.set_text("", cx));
        self.notice = None;
        // show the message and an empty reply at once; the runtime saves both together
        let n = self.msgs.len();
        self.msgs.push(Msg {
            role: Role::User,
            blocks: Arc::new(markdown::plain(&text)),
            parsed_len: text.len(),
            text: text.clone(),
            meta: String::new(),
            meta_kind: MetaKind::Normal,
            streaming: false,
        });
        self.msgs.push(Msg {
            role: Role::Assistant,
            text: String::new(),
            blocks: Arc::new(vec![]),
            parsed_len: 0,
            meta: format!("Waiting for {model}…"),
            meta_kind: MetaKind::Normal,
            streaming: true,
        });
        self.follow.set(true); // sending always brings the conversation's end into view
        self.append_rows(n);
        self.append_rows(n + 1);
        let stop = Arc::new(AtomicBool::new(false));
        self.run_seq += 1;
        let token = self.run_seq;
        self.run = Some(Run { token, msg_ix: n + 1, chat_id: existing.clone().unwrap_or_default(), run_id: None, stop: stop.clone() });
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
                    None => match api2.post::<api::OneChat>("/api/chats", json!({ "project_id": project_id })) {
                        Ok(c) => {
                            let _ = created_tx.unbounded_send(StreamMsg::Event(json!({"type": "chat_created", "chat": {
                                "id": c.chat.id, "project_id": c.chat.project_id, "title": c.chat.title,
                                "updated_at": c.chat.updated_at, "archived": c.chat.archived, "messages": c.chat.messages}})));
                            c.chat.id
                        }
                        Err(e) => {
                            let _ = tx.unbounded_send(StreamMsg::Ended(Some(e)));
                            return;
                        }
                    },
                };
                let r = api2.chat_stream(&chat_id, &model, &text, |ev| {
                    let _ = tx.unbounded_send(StreamMsg::Event(ev));
                    !stop.load(Ordering::SeqCst)
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
                    } else if ended {
                        // A reply you left. The runtime has saved what arrived; refresh
                        // the lists, and the transcript if you are looking at that chat.
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
        let mut ended = false;
        let mut refresh_lists = false;
        for m in batch {
            match m {
                StreamMsg::Event(ev) => {
                    self.perf.stream_events += 1;
                    let ty = ev.get("type").and_then(Value::as_str).unwrap_or("");
                    let Some(msg) = self.msgs.get_mut(ix) else { return };
                    match ty {
                        "chat_created" => {
                            if let Ok(c) = serde_json::from_value::<api::ChatInfo>(ev["chat"].clone()) {
                                if let Some(run) = self.run.as_mut() {
                                    run.chat_id = c.id.clone();
                                }
                                self.chat = Some(c);
                            }
                        }
                        "start" => {
                            if let Some(run) = self.run.as_mut() {
                                run.run_id = ev.get("run_id").and_then(Value::as_str).map(str::to_string);
                            }
                            if let Some(m) = ev.get("model_requested").and_then(Value::as_str) {
                                msg.meta = format!("{m} is answering…");
                            }
                            let c = &ev["context"];
                            if let (Some(est), Some(win)) = (c["estimated_tokens"].as_u64(), c["window"].as_u64()) {
                                self.context = Some((est, win));
                            }
                        }
                        "delta" => msg.text.push_str(ev.get("text").and_then(Value::as_str).unwrap_or("")),
                        "done" => {
                            let g = |k: &str| ev.get(k).and_then(Value::as_u64);
                            let model = ev.get("model").and_then(Value::as_str).unwrap_or("").to_string();
                            let mut parts = vec![model];
                            if let (Some(p), Some(r)) = (g("prompt_tokens"), g("reply_tokens")) {
                                parts.push(format!("{p} in · {r} out"));
                            }
                            if let Some(ms) = g("ms") {
                                parts.push(format!("{:.1}s", ms as f64 / 1000.0));
                            }
                            msg.meta = parts.join(" · ");
                            msg.meta_kind = MetaKind::Normal;
                            // M4: a task's report, in one line (the trace is in the browser page)
                            if let Some((line, attention)) = api::report_line(&ev["report"]) {
                                msg.meta = format!("{} · {line}", msg.meta);
                                if attention {
                                    msg.meta_kind = MetaKind::Warn;
                                }
                            }
                            refresh_lists = true;
                        }
                        "tool_call" => {
                            // M4: say which step is running, so Stop has something to interrupt that you can see
                            let step = ev.get("step").and_then(Value::as_u64).unwrap_or(0);
                            let tool = ev.get("tool").and_then(Value::as_str).unwrap_or("a tool");
                            let path = ev["args"].get("path").and_then(Value::as_str).filter(|p| !p.is_empty()).unwrap_or(".");
                            let what = match tool {
                                "read_file" => format!("reading {path}"),
                                "list_files" => format!("listing {path}"),
                                "search_files" => format!("searching {path}"),
                                _ => "running a tool".into(),
                            };
                            msg.meta = format!("Step {step}: {}…", what.chars().take(80).collect::<String>());
                        }
                        "error" => {
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
                    if let Some(e) = err {
                        if msg.meta.ends_with('…') {
                            msg.meta = e.message;
                            msg.meta_kind = MetaKind::Bad;
                        }
                    } else if msg.meta.ends_with('…') {
                        msg.meta = "Stopped.".into();
                        msg.meta_kind = MetaKind::Warn;
                    }
                }
            }
        }
        if ended {
            self.msgs[ix].streaming = false;
            self.run = None;
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

    #[cfg(feature = "e2e")]
    pub fn is_running(&self) -> bool {
        self.run.is_some()
    }

    pub fn stop_reply(&mut self, cx: &mut Context<Self>) {
        if let Some(run) = self.run.as_ref() {
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
        if self.model_menu || self.options_menu || self.project_input.is_some() || self.card_menu {
            self.card_menu = false;
            self.model_menu = false;
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
        match m.status.as_str() {
            "complete" => ([model, toks].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · "), MetaKind::Normal),
            "stopped" => (format!("{model} · Stopped by you. Incomplete."), MetaKind::Warn),
            other => (format!("{model} · Incomplete ({other})."), MetaKind::Warn),
        }
    };
    Msg { role, text: m.content.clone(), parsed_len: m.content.len(), blocks: Arc::new(blocks), meta, meta_kind: kind, streaming: false }
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
            projects = projects.child(
                ui::nav(row_id.clone(), current)
                    .font_weight(FontWeight::NORMAL)
                    .child(ui::hover_icon("folder", &row_id, if current { text_strong() } else { text_default() }, text_strong()))
                    .child(ui::nav_title(p.name.clone()))
                    .when(p.chats > 0, |d| d.child(ui::nav_count(p.chats.to_string())))
                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.select_project(id.clone(), cx))),
            );
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
            let text = if archived_count > 0 {
                format!("No active chats in {pname}. {archived_count} archived.")
            } else {
                format!("No chats in {pname} yet.")
            };
            sessions = sessions.child(div().text_size(fs_12()).text_color(text_subtle()).px(px(10.)).py(px(4.)).child(text));
        }
        for c in self.chats.iter().chain(self.archived.iter()) {
            let id = c.id.clone();
            let is_current = current.as_deref() == Some(&c.id);
            let streaming = self.run.as_ref().map(|r| r.chat_id == c.id).unwrap_or(false);
            let mut row = ui::nav(SharedString::from(format!("c-{}", c.id)), is_current);
            row = if c.archived {
                // `.nav.archived .t`: subtle, italic
                row.child(ui::nav_title(c.title.clone()).text_color(text_subtle()).italic()).child(ui::nav_count("archived"))
            } else {
                row.child(ui::nav_title(c.title.clone()))
            };
            if !c.archived {
                row = if streaming { row.child(ui::dot(accent())) } else { row.child(ui::nav_count(ago(&c.updated_at))) };
            }
            sessions = sessions.child(row.on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.open_chat(id.clone(), cx))));
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
                        .px(px(10.))
                        .py(px(7.))
                        .text_color(text_subtle())
                        .child("Delete chat")
                        .child(div().text_size(fs_12()).child("Not available yet")),
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
        let row = |id: &'static str| {
            div().id(id).group(id).flex().items_center().gap(px(14.)).h(px(30.)).px(px(20.))
        };
        let title_row = |title: SharedString, button: gpui::Stateful<gpui::Div>| {
            div()
                .flex()
                .items_center()
                .justify_between()
                .h(px(30.))
                .px(px(20.))
                .text_size(fs_13())
                .text_color(text_default())
                .child(title)
                .child(button.mr(px(-6.)))
        };
        // Changes: what this session changed in your files. Nothing can change
        // files yet (tools arrive in M3), so it reads +0 −0 rather than invent numbers.
        let changes = row("card-changes")
            .child(ui::icon("diff").text_color(text_strong()))
            .child(div().flex_1().text_size(fs_13()).text_color(text_strong()).child("Changes"))
            .child(div().flex().gap(px(6.)).text_size(fs_13()).child(div().text_color(success()).child("+0")).child(div().text_color(danger()).child("-0")));
        let web = row("card-web")
            .child(ui::icon("globe").text_color(text_default()))
            .child(div().text_size(fs_13()).text_color(text_default()).child("Web search"));
        // View all lists every source once there are more than fit here (M3)
        let all = row("card-all")
            .child(ui::hover_icon("connectors", &"card-all".into(), text_subtle(), text_default()))
            .child(div().text_size(fs_13()).text_color(text_subtle()).group_hover("card-all", |s| s.text_color(text_default())).child("View all"))
;
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
            .child(changes)
            .child(div().mx(px(20.)).mt(px(7.)).mb(px(6.)).h(px(1.)).bg(card_divider()))
            // Adding sources needs the tools that arrive in M3; the + is shown, as in the reference
            .child(title_row("Sources".into(), ui::icon_btn("card-add-source", "plus", false)))
            .child(web)
            .child(all)
            .children(menu)
            .into_any_element()
    }

    /// `.thread`: max 760, padding 48 32 32. Notices sit in the thread like the page's.
    fn render_transcript(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let archived = self.chat.as_ref().map(|c| c.archived).unwrap_or(false);
        let mut notices: Vec<AnyElement> = vec![];
        if self.msgs.is_empty() && !self.loading_chat {
            let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_default();
            notices.push(
                ui::notice(None)
                    .child(div().flex_1().min_w_0().child(format!(
                        "New chat in {project}. It talks to a model running in Ollama on this Mac; nothing leaves this machine. It is saved on this Mac once you send the first message."
                    )))
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
        if self.asks_before_read() {
            let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_default();
            let folder = self.access.as_ref().and_then(|a| a.workspace.as_ref()).map(|w| w.name.clone()).unwrap_or_default();
            let error = self.access_error.clone();
            notices.push(
                ui::notice(Some(ui::NoticeKind::Warn))
                    .child(
                        div().flex_1().min_w_0().flex().flex_col().gap(px(4.))
                            .child(format!(
                                "{project} uses Ask before read. This app cannot show approval requests yet, so chats here cannot read files in {folder}: each read is refused right away. Workspace read lets chats read ordinary files in that folder without asking; excluded files, links and paths outside it stay blocked, and every read still gets a receipt. Or keep Ask before read and answer requests in the browser page."
                            ))
                            .when_some(error, |d, e| d.child(div().text_color(danger()).child(e))),
                    )
                    .child(
                        ui::pill("switch-workspace-read")
                            .child("Switch to Workspace read")
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.switch_to_workspace_read(cx))),
                    )
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
        let notice_col = |notices: Vec<AnyElement>, top: f32| {
            div()
                .w_full()
                .flex()
                .justify_center()
                .px(px(SP_32))
                .pt(px(top))
                .child(div().w_full().max_w(px(696.)).flex().flex_col().gap(px(SP_16)).children(notices))
        };
        if self.msgs.is_empty() {
            return div().flex_1().min_h_0().child(notice_col(notices, SP_48)).into_any_element();
        }
        let host = cx.entity();
        let state = self.list.clone();
        div()
            .flex_1()
            .min_h_0()
            .flex()
            .flex_col()
            .child(
                list(state, move |ix, _window, cx| {
                    let host2 = host.clone();
                    let ws = host.read(cx);
                    match ws.rows.get(ix).and_then(|r| ws.msgs.get(r.msg).map(|m| (*r, m))) {
                        Some((row, m)) => transcript::render_row(&host2, row, m, &ws.selection, ix == 0),
                        None => div().into_any_element(),
                    }
                })
                .flex_1()
                .size_full(),
            )
            .when(!notices.is_empty(), |d| d.child(notice_col(notices, SP_8)))
            .into_any_element()
    }

    /// `.composer-dock` + `.composer`: raised box, strong border, radius 12, padding 12.
    fn render_composer(&mut self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        let up = self.ollama_up();
        let pill_label = match (&self.models, up) {
            (None, _) => "Checking Ollama…".to_string(),
            (Some(_), false) => "Ollama not running".to_string(),
            (Some(m), true) if m.models.is_empty() => "No models installed".to_string(),
            (Some(_), true) => self.model.clone().unwrap_or_else(|| "Pick a model".into()),
        };
        let running = self.run.is_some();
        let can_send = self.can_send() && !self.composer.read(cx).text().trim().is_empty();
        // the pill turns warning-coloured while Ollama is down; otherwise `.pill` colours
        let ollama_down = !up && self.models.is_some();
        let pill_color = if ollama_down { warning() } else { text_default() };
        let pill_hover = if ollama_down { warning() } else { text_strong() };

        let mut menu = None;
        if self.model_menu {
            // `.model-menu`: bottom 36, left 32, 380 wide, raised, strong border, radius 12, padding 6
            let mut m = div()
                .absolute()
                .bottom(px(36.))
                .left(px(32.))
                .w(px(380.))
                .p(px(6.))
                .rounded(px(R_12))
                .bg(bg_raised())
                .border_1()
                .border_color(border_strong())
                .shadow_lg()
                .flex()
                .flex_col();
            let empty = |t: &str| div().p(px(8.)).text_size(fs_12()).text_color(text_subtle()).child(t.to_string());
            match &self.models {
                Some(ms) if !ms.reachable => m = m.child(empty("Ollama is not running. Start it, then reopen this menu.")),
                Some(ms) if ms.models.is_empty() => m = m.child(empty("No models installed. In a terminal: ollama pull qwen2.5:3b")),
                Some(ms) => {
                    for (i, md) in ms.models.iter().enumerate() {
                        let name = md.name.clone();
                        let label = if md.tool_capable { "tool calls verified" } else { "plain chat only" };
                        let sub = [md.parameter_size.clone(), md.context_length.map(|c| format!("ctx {}", fmt_k(c))).unwrap_or_default(), label.to_string()]
                            .into_iter()
                            .filter(|s| !s.is_empty())
                            .collect::<Vec<_>>()
                            .join(" · ");
                        let on = self.model.as_deref() == Some(&md.name);
                        m = m.child(
                            div()
                                .id(("mm-row", i))
                                .flex()
                                .flex_col()
                                .px(px(8.))
                                .py(px(6.))
                                .rounded(px(R_8))
                                .cursor_pointer()
                                .when(on, |d| d.bg(bg_selected()))
                                .hover(|s| s.bg(bg_selected()))
                                .child(div().text_size(fs_13()).text_color(text_strong()).child(md.name.clone()))
                                .child(div().text_size(fs_12()).text_color(text_subtle()).child(sub))
                                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                                    this.model = Some(name.clone());
                                    this.model_menu = false;
                                    if matches!(this.notice, Some((_, MetaKind::Warn, false))) {
                                        this.notice = None;
                                    }
                                    cx.notify();
                                })),
                        );
                    }
                }
                None => {}
            }
            m = m.child(
                div()
                    .mt(px(4.))
                    .p(px(8.))
                    .border_t_1()
                    .border_color(border())
                    .text_size(fs_12())
                    .text_color(text_subtle())
                    .child("Only installed models are listed. Umbrous never switches models for you."),
            );
            menu = Some(m);
        }

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
                    .flex()
                    .flex_col()
                    .gap(px(4.))
                    .child(div().min_h(px(40.)).child(self.composer.clone()))
                    .child(
                        // `.bar`
                        div()
                            .relative()
                            .flex()
                            .items_center()
                            .gap(px(4.))
                            .child(ui::icon_btn("attach", "clip", false))
                            .child(
                                ui::pill("model-pill")
                                    .when(ollama_down, |d| d.text_color(warning()).hover(|s| s.bg(bg_hover()).text_color(warning())))
                                    .child(ui::hover_icon("cpu", &"model-pill".into(), pill_color, pill_hover))
                                    .child(pill_label)
                                    .child(ui::hover_icon("chevron-down", &"model-pill".into(), pill_color, pill_hover))
                                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                        if this.model_menu {
                                            this.model_menu = false;
                                            cx.notify();
                                        } else {
                                            this.load_models(true, cx);
                                        }
                                    })),
                            )
                            .child(ui::pill("tools-pill").child(ui::hover_icon("sliders", &"tools-pill".into(), text_default(), text_strong())).child("Tools · 4"))
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
                .children(self.render_options_menu(cx));
            app.when(self.sidebar, |d| d.child(self.render_sidebar(cx)))
                .child(main)
                .when(self.details && wide, |d| d.child(self.render_right_panel(cx)))
        };
        let el = root.child(app);
        let ms = t0.elapsed().as_secs_f64() * 1000.0;
        self.perf.render_count += 1;
        self.perf.render_ms_total += ms;
        self.perf.render_ms_max = self.perf.render_ms_max.max(ms);
        el
    }
}
