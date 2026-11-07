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
use crate::transcript::{self, MetaKind, Msg, Pos, Role, Row, Selection, SelectionHost};

const STREAM_BATCH: Duration = Duration::from_millis(33);

pub enum Phase {
    Starting,
    Failed(Failure),
    Ready,
}

struct Run {
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
    sidebar: bool,
    details: bool,
    focus: FocusHandle,
    parse_in_flight: bool,
    loading_chat: bool,
    pub started: Instant,
    logged_first_frame: bool,
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
            if window.is_window_active() && matches!(this.phase, Phase::Ready) {
                this.load_models(false, cx); // Ollama may have started or stopped meanwhile
            }
        }));
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
            list: ListState::new(0, ListAlignment::Bottom, px(800.)),
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
            sidebar: true,
            details: true,
            focus: cx.focus_handle(),
            parse_in_flight: false,
            loading_chat: false,
            started: Instant::now(),
            logged_first_frame: false,
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
        self.api_call(cx, |api| api.get::<api::Projects>("/api/projects"), move |this, r, cx| {
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
        let show_archived = self.show_archived;
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
            |this, r, _| {
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

    pub fn select_project(&mut self, id: String, cx: &mut Context<Self>) {
        if self.project_id.as_deref() != Some(&id) {
            self.project_id = Some(id);
            self.show_archived = false;
            self.new_chat(cx);
            self.load_chats(cx);
        }
    }

    pub fn new_chat(&mut self, cx: &mut Context<Self>) {
        self.stop_reply(cx);
        self.chat = None;
        self.msgs.clear();
        self.rebuild_rows();
        self.selection = Selection::default();
        self.notice = None;
        self.options_menu = false;
        self.composer.update(cx, |c, cx| c.set_disabled(false, cx));
        cx.notify();
    }

    pub fn open_chat(&mut self, id: String, cx: &mut Context<Self>) {
        if self.chat.as_ref().map(|c| c.id == id).unwrap_or(false) {
            return;
        }
        self.stop_reply(cx);
        self.loading_chat = true;
        self.options_menu = false;
        let t0 = Instant::now();
        // fetch and parse every message off the UI thread
        self.api_call(
            cx,
            move |api| {
                let t: api::Transcript = api.get(&format!("/api/chats/{id}"))?;
                let msgs: Vec<Msg> = t.messages.iter().map(stored_msg).collect();
                Ok((t.chat, msgs))
            },
            move |this, r, cx| {
                this.loading_chat = false;
                match r {
                    Ok((chat, msgs)) => {
                        let archived = chat.archived;
                        this.chat = Some(chat);
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
                    let archived = one.chat.archived;
                    this.chat = Some(one.chat);
                    this.composer.update(cx, |c, cx| c.set_disabled(archived, cx));
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
        self.api_call(cx, |api| api.get::<api::Models>("/api/models"), move |this, r, _| {
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
            meta: format!("{model} is answering…"),
            meta_kind: MetaKind::Normal,
            streaming: true,
        });
        self.append_rows(n);
        self.append_rows(n + 1);
        self.list.scroll_to_reveal_item(self.rows.len().saturating_sub(1));
        let stop = Arc::new(AtomicBool::new(false));
        self.run = Some(Run { chat_id: existing.clone().unwrap_or_default(), run_id: None, stop: stop.clone() });
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
        cx.spawn(async move |this, cx| {
            loop {
                let Some(first) = rx.next().await else { break };
                let mut batch = vec![first];
                while let Ok(more) = rx.try_recv() {
                    batch.push(more);
                }
                let ended = batch.iter().any(|m| matches!(m, StreamMsg::Ended(_)));
                if this.update(cx, |this, cx| this.apply_stream(batch, cx)).is_err() || ended {
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
        let ix = self.msgs.len().saturating_sub(1);
        let mut ended = false;
        let mut refresh_lists = false;
        for m in batch {
            match m {
                StreamMsg::Event(ev) => {
                    self.perf.stream_events += 1;
                    let ty = ev.get("type").and_then(Value::as_str).unwrap_or("");
                    let msg = &mut self.msgs[ix];
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
                            refresh_lists = true;
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
                    let msg = &mut self.msgs[ix];
                    if let Some(e) = err {
                        if msg.meta.ends_with("answering…") {
                            msg.meta = e.message;
                            msg.meta_kind = MetaKind::Bad;
                        }
                    } else if msg.meta.ends_with("answering…") {
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
        let text = m.text.clone();
        let task = cx.background_spawn(async move {
            let t = Instant::now();
            let blocks = markdown::parse(&text);
            (blocks, text.len(), t.elapsed().as_secs_f64() * 1000.0)
        });
        cx.spawn(async move |this, cx| {
            let (blocks, len, ms) = task.await;
            this.update(cx, |this, cx| {
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
    }

    fn append_rows(&mut self, ix: usize) {
        let start = self.rows.len();
        let new = transcript::rows_for(ix, &self.msgs[ix]);
        let n = new.len();
        self.rows.extend(new);
        self.msg_rows.push(start..start + n);
        self.list.splice(start..start, n);
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
        if self.model_menu || self.options_menu || self.project_input.is_some() {
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

fn icon_button(id: &'static str, label: impl Into<SharedString>) -> gpui::Stateful<gpui::Div> {
    div()
        .id(id)
        .px(px(8.))
        .py(px(4.))
        .rounded(px(6.))
        .text_color(theme::muted())
        .cursor_pointer()
        .hover(|s| s.bg(theme::hover()).text_color(theme::text()))
        .child(label.into())
}

fn section_label(t: &str) -> gpui::Div {
    div().px(px(12.)).pt(px(14.)).pb(px(6.)).text_size(px(12.)).text_color(theme::faint()).child(t.to_string())
}

fn row(label: &str, value: impl Into<SharedString>) -> gpui::Div {
    div()
        .flex()
        .justify_between()
        .gap(px(12.))
        .py(px(4.))
        .child(div().text_color(theme::faint()).child(label.to_string()))
        .child(div().text_color(theme::text()).truncate().child(value.into()))
}

impl Workspace {
    fn render_sidebar(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let mut projects = div().flex().flex_col();
        for p in &self.projects {
            let selected = self.project_id.as_deref() == Some(&p.id);
            let id = p.id.clone();
            projects = projects.child(
                div()
                    .id(SharedString::from(format!("p-{}", p.id)))
                    .mx(px(8.))
                    .px(px(10.))
                    .py(px(6.))
                    .rounded(px(7.))
                    .flex()
                    .justify_between()
                    .cursor_pointer()
                    .when(selected, |d| d.bg(theme::active()))
                    .hover(|s| s.bg(theme::hover()))
                    .child(div().truncate().child(format!("▢  {}", p.name)))
                    .child(div().text_size(px(12.)).text_color(theme::faint()).child(p.chats.to_string()))
                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.select_project(id.clone(), cx))),
            );
        }
        if let Some(input) = &self.project_input {
            projects = projects.child(
                div()
                    .mx(px(8.))
                    .px(px(10.))
                    .py(px(6.))
                    .rounded(px(7.))
                    .border_1()
                    .border_color(theme::accent())
                    .child(input.clone()),
            );
            if let Some(err) = &self.project_error {
                projects = projects.child(div().mx(px(18.)).text_size(px(12.)).text_color(theme::bad()).child(err.clone()));
            }
        }

        let mut sessions = div().flex().flex_col();
        let current = self.chat.as_ref().map(|c| c.id.clone());
        let list: Vec<&api::ChatInfo> = self.chats.iter().chain(self.archived.iter()).collect();
        if list.is_empty() {
            sessions = sessions.child(div().px(px(20.)).py(px(6.)).text_size(px(13.)).text_color(theme::faint()).child(
                if self.projects.iter().any(|p| Some(&p.id) == self.project_id.as_ref() && p.archived_chats > 0) {
                    "All chats here are archived."
                } else {
                    "No chats yet."
                },
            ));
        }
        for c in list {
            let id = c.id.clone();
            let selected = current.as_deref() == Some(&c.id);
            sessions = sessions.child(
                div()
                    .id(SharedString::from(format!("c-{}", c.id)))
                    .mx(px(8.))
                    .px(px(10.))
                    .py(px(6.))
                    .rounded(px(7.))
                    .flex()
                    .gap(px(6.))
                    .cursor_pointer()
                    .when(selected, |d| d.bg(theme::active()))
                    .hover(|s| s.bg(theme::hover()))
                    .child(div().flex_1().min_w_0().truncate().when(c.archived, |d| d.text_color(theme::muted())).child(c.title.clone()))
                    .when(c.archived, |d| d.child(div().text_size(px(11.)).text_color(theme::faint()).child("archived")))
                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.open_chat(id.clone(), cx))),
            );
        }
        let archived_count = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.archived_chats).unwrap_or(0);
        if archived_count > 0 {
            let label = if self.show_archived { "Hide archived".to_string() } else { format!("Show archived ({archived_count})") };
            sessions = sessions.child(
                div()
                    .id("toggle-archived")
                    .mx(px(18.))
                    .py(px(6.))
                    .text_size(px(12.))
                    .text_color(theme::faint())
                    .cursor_pointer()
                    .hover(|s| s.text_color(theme::text()))
                    .child(label)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.show_archived = !this.show_archived;
                        this.load_chats(cx);
                    })),
            );
        }

        div()
            .id("sidebar")
            .w(px(272.))
            .flex_shrink_0()
            .h_full()
            .flex()
            .flex_col()
            .bg(theme::panel())
            .border_r_1()
            .border_color(theme::border())
            .overflow_y_scroll()
            .child(
                div()
                    .h(px(52.))
                    .pl(px(92.)) // traffic lights
                    .pr(px(12.))
                    .flex()
                    .items_center()
                    .font_weight(FontWeight::SEMIBOLD)
                    .child("Umbrous"),
            )
            .child(
                div()
                    .id("new-chat")
                    .mx(px(8.))
                    .px(px(10.))
                    .py(px(7.))
                    .rounded(px(7.))
                    .flex()
                    .justify_between()
                    .cursor_pointer()
                    .hover(|s| s.bg(theme::hover()))
                    .child("⊕  New chat")
                    .child(div().text_size(px(12.)).text_color(theme::faint()).child("⌘N"))
                    .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                        this.new_chat(cx);
                        window.focus(&this.composer.focus_handle(cx));
                    })),
            )
            .child(
                div()
                    .flex()
                    .justify_between()
                    .items_center()
                    .pr(px(12.))
                    .child(section_label("Projects"))
                    .child(icon_button("add-project", "+").on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                        let input = cx.new(|cx| {
                            let mut c = Composer::new(cx);
                            c.set_placeholder("Project name", cx);
                            c
                        });
                        let sub = cx.subscribe_in(&input, window, |this, _, ev: &ComposerEvent, window, cx| match ev {
                            ComposerEvent::Submit(name) => this.add_project(name.clone(), window, cx),
                        });
                        this._subs.push(sub);
                        window.focus(&input.focus_handle(cx));
                        this.project_input = Some(input);
                        this.project_error = None;
                        cx.notify();
                    }))),
            )
            .child(projects)
            .child(section_label("Sessions"))
            .child(sessions)
            .into_any_element()
    }

    fn render_header(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let title = self.chat.as_ref().map(|c| c.title.clone()).unwrap_or_else(|| "New chat".into());
        let has_chat = self.chat.is_some();
        div()
            .h(px(52.))
            .flex_shrink_0()
            .px(px(16.))
            .flex()
            .items_center()
            .justify_between()
            .border_b_1()
            .border_color(theme::border())
            .child(div().flex_1().min_w_0().truncate().font_weight(FontWeight::MEDIUM).child(title))
            .child(
                div()
                    .flex()
                    .gap(px(4.))
                    .when(has_chat, |d| {
                        d.child(icon_button("options", "⋯").on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                            this.options_menu = !this.options_menu;
                            cx.notify();
                        })))
                    })
                    .child(icon_button("toggle-details", "▤").on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.details = !this.details;
                        cx.notify();
                    }))),
            )
            .into_any_element()
    }

    fn render_options_menu(&mut self, cx: &mut Context<Self>) -> Option<AnyElement> {
        if !self.options_menu {
            return None;
        }
        let archived = self.chat.as_ref().map(|c| c.archived).unwrap_or(false);
        Some(
            div()
                .absolute()
                .top(px(46.))
                .right(px(48.))
                .w(px(220.))
                .p(px(4.))
                .rounded(px(9.))
                .bg(theme::panel_2())
                .border_1()
                .border_color(theme::border())
                .shadow_lg()
                .flex()
                .flex_col()
                .child(
                    div()
                        .id("opt-archive")
                        .px(px(10.))
                        .py(px(6.))
                        .rounded(px(6.))
                        .cursor_pointer()
                        .hover(|s| s.bg(theme::hover()))
                        .child(if archived { "Restore chat" } else { "Archive chat" })
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_archived(!archived, cx))),
                )
                .child(
                    div()
                        .px(px(10.))
                        .py(px(6.))
                        .text_color(theme::faint())
                        .flex()
                        .justify_between()
                        .child("Delete chat")
                        .child(div().text_size(px(11.)).child("Not available yet")),
                )
                .into_any_element(),
        )
    }

    fn render_transcript(&mut self, cx: &mut Context<Self>) -> AnyElement {
        if self.msgs.is_empty() {
            let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_default();
            let text = if self.loading_chat {
                "Loading…".to_string()
            } else {
                format!("New chat in {project}. It talks to a model running in Ollama on this Mac; nothing leaves this machine. It is saved on this Mac once you send the first message.")
            };
            return div()
                .flex_1()
                .flex()
                .items_center()
                .justify_center()
                .px(px(48.))
                .child(div().max_w(px(520.)).text_color(theme::muted()).text_center().child(text))
                .into_any_element();
        }
        let host = cx.entity();
        let state = self.list.clone();
        list(state, move |ix, _window, cx| {
            let host2 = host.clone();
            let ws = host.read(cx);
            match ws.rows.get(ix).and_then(|r| ws.msgs.get(r.msg).map(|m| (*r, m))) {
                Some((row, m)) => transcript::render_row(&host2, row, m, &ws.selection),
                None => div().into_any_element(),
            }
        })
        .flex_1()
        .size_full()
        .into_any_element()
    }

    fn render_composer(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let up = self.ollama_up();
        let pill = match (&self.models, up) {
            (None, _) => "Checking Ollama…".to_string(),
            (Some(_), false) => "Ollama not running".to_string(),
            (Some(m), true) if m.models.is_empty() => "No models installed".to_string(),
            (Some(_), true) => self.model.clone().unwrap_or_else(|| "Pick a model".into()),
        };
        let running = self.run.is_some();
        let can_send = self.can_send() && !self.composer.read(cx).text().trim().is_empty();
        let archived = self.chat.as_ref().map(|c| c.archived).unwrap_or(false);

        let mut wrap = div().w_full().flex().flex_col().items_center().px(px(32.)).pb(px(18.)).pt(px(8.)).gap(px(8.));
        if archived {
            wrap = wrap.child(
                div()
                    .w_full()
                    .max_w(px(760.))
                    .px(px(12.))
                    .py(px(8.))
                    .rounded(px(8.))
                    .bg(theme::panel_2())
                    .flex()
                    .justify_between()
                    .items_center()
                    .child(div().text_color(theme::muted()).child("This chat is archived and read-only. Every message is kept."))
                    .child(icon_button("restore", "Restore").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.set_archived(false, cx)))),
            );
        }
        if let Some((text, kind, retry)) = self.notice.clone() {
            wrap = wrap.child(
                div()
                    .w_full()
                    .max_w(px(760.))
                    .px(px(12.))
                    .py(px(8.))
                    .rounded(px(8.))
                    .border_1()
                    .border_color(theme::border())
                    .flex()
                    .justify_between()
                    .items_center()
                    .gap(px(12.))
                    .child(div().flex_1().text_color(if kind == MetaKind::Bad { theme::bad() } else { theme::warn() }).child(text))
                    .when(retry, |d| {
                        d.child(icon_button("retry-ollama", "Retry").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.load_models(false, cx))))
                    }),
            );
        }
        let mut menu = None;
        if self.model_menu {
            let mut m = div()
                .absolute()
                .bottom(px(44.))
                .left(px(8.))
                .w(px(340.))
                .p(px(4.))
                .rounded(px(9.))
                .bg(theme::panel_2())
                .border_1()
                .border_color(theme::border())
                .shadow_lg()
                .flex()
                .flex_col();
            match &self.models {
                Some(ms) if !ms.reachable => m = m.child(div().p(px(8.)).text_color(theme::muted()).child("Ollama is not running. Open the Ollama app, then reopen this menu.")),
                Some(ms) if ms.models.is_empty() => m = m.child(div().p(px(8.)).text_color(theme::muted()).child("No models installed. In Terminal: ollama pull qwen2.5:3b")),
                Some(ms) => {
                    for (i, md) in ms.models.iter().enumerate() {
                        let name = md.name.clone();
                        let sub = [md.parameter_size.clone(), md.context_length.map(|c| format!("ctx {}k", c / 1024)).unwrap_or_default(),
                                   if md.tool_capable { "tool calls verified".into() } else { "plain chat".to_string() }]
                            .into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · ");
                        let selected = self.model.as_deref() == Some(&md.name);
                        m = m.child(
                            div()
                                .id(("model", i))
                                .px(px(10.))
                                .py(px(6.))
                                .rounded(px(6.))
                                .cursor_pointer()
                                .when(selected, |d| d.bg(theme::active()))
                                .hover(|s| s.bg(theme::hover()))
                                .child(md.name.clone())
                                .child(div().text_size(px(12.)).text_color(theme::faint()).child(sub))
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
            m = m.child(div().px(px(10.)).py(px(6.)).text_size(px(11.)).text_color(theme::faint()).child("Only installed models are listed. Umbrous never switches models for you."));
            menu = Some(m);
        }

        let send_label = if running { "■" } else { "↑" };
        wrap.child(
            div()
                .relative()
                .w_full()
                .max_w(px(760.))
                .rounded(px(12.))
                .bg(theme::panel())
                .border_1()
                .border_color(theme::border())
                .px(px(14.))
                .pt(px(12.))
                .pb(px(8.))
                .flex()
                .flex_col()
                .gap(px(10.))
                .child(self.composer.clone())
                .child(
                    div()
                        .flex()
                        .justify_between()
                        .items_center()
                        .child(
                            div()
                                .id("model-pill")
                                .px(px(10.))
                                .py(px(4.))
                                .rounded(px(7.))
                                .border_1()
                                .border_color(theme::border())
                                .text_size(px(12.5))
                                .cursor_pointer()
                                .text_color(if up { theme::text() } else { theme::warn() })
                                .hover(|s| s.bg(theme::hover()))
                                .child(format!("◎  {pill}  ⌄"))
                                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                    if this.model_menu {
                                        this.model_menu = false;
                                        cx.notify();
                                    } else {
                                        this.load_models(true, cx);
                                    }
                                })),
                        )
                        .child(
                            div()
                                .id("send")
                                .size(px(28.))
                                .rounded_full()
                                .flex()
                                .items_center()
                                .justify_center()
                                .bg(if can_send || running { theme::accent() } else { theme::active() })
                                .text_color(if can_send || running { gpui::rgb(0xffffff) } else { theme::faint() })
                                .when(can_send || running, |d| d.cursor_pointer())
                                .child(send_label)
                                .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
                                    if this.run.is_some() {
                                        this.stop_reply(cx);
                                    } else {
                                        let text = this.composer.read(cx).text().to_string();
                                        this.send(text, window, cx);
                                    }
                                })),
                        ),
                )
                .children(menu),
        )
        .into_any_element()
    }

    fn render_details(&mut self, _cx: &mut Context<Self>) -> AnyElement {
        let project = self.projects.iter().find(|p| Some(&p.id) == self.project_id.as_ref()).map(|p| p.name.clone()).unwrap_or_default();
        let ollama = match &self.models {
            Some(m) if m.reachable => format!("Running · {}", m.version.clone().unwrap_or_default()),
            Some(_) => "Not running".into(),
            None => "…".into(),
        };
        let (version, schema, bind) = self
            .health
            .as_ref()
            .map(|h| (format!("{} · {}", h.version, h.milestone), format!("v{}", h.schema_version), h.bind.clone()))
            .unwrap_or_default();
        div()
            .id("details")
            .w(px(288.))
            .flex_shrink_0()
            .h_full()
            .bg(theme::panel())
            .border_l_1()
            .border_color(theme::border())
            .flex()
            .flex_col()
            .overflow_y_scroll()
            .text_size(px(13.))
            .child(div().h(px(52.)).px(px(16.)).flex().items_center().font_weight(FontWeight::MEDIUM).child("Details"))
            .child(
                div()
                    .px(px(16.))
                    .flex()
                    .flex_col()
                    .child(div().pt(px(10.)).pb(px(4.)).text_size(px(12.)).text_color(theme::faint()).child("Session"))
                    .child(row("Model", self.model.clone().unwrap_or_else(|| "—".into())))
                    .child(
                        div().flex().justify_between().py(px(4.))
                            .child(div().text_color(theme::faint()).child("Runs on"))
                            .child(div().flex().items_center().gap(px(6.)).child(div().size(px(7.)).rounded_full().bg(theme::good())).child("Local")),
                    )
                    .child(row("Project", project))
                    .child(div().pt(px(18.)).pb(px(4.)).text_size(px(12.)).text_color(theme::faint()).child("Runtime"))
                    .child(row("Status", match &self.phase { Phase::Ready => "Connected", Phase::Starting => "Starting", Phase::Failed(_) => "Stopped" }))
                    .child(row("Version", version))
                    .child(row("Schema", schema))
                    .child(row("Address", bind))
                    .child(row("Ollama", ollama)),
            )
            .into_any_element()
    }

    fn render_status(&mut self, cx: &mut Context<Self>) -> AnyElement {
        let (title, message, detail, failed, code) = match &self.phase {
            Phase::Starting => ("Starting Umbrous…".to_string(), "Starting the local runtime on this Mac.".to_string(), vec![], false, String::new()),
            Phase::Failed(f) => (f.title.clone(), f.message.clone(), f.detail.clone(), true, f.code.clone()),
            Phase::Ready => unreachable!(),
        };
        let mut col = div()
            .w(px(560.))
            .flex()
            .flex_col()
            .items_center()
            .gap(px(10.))
            .child(div().text_size(px(20.)).font_weight(FontWeight::SEMIBOLD).text_color(if failed { theme::bad() } else { theme::text() }).child(title))
            .child(div().text_color(theme::muted()).text_center().child(message));
        let detail: Vec<String> = detail.into_iter().filter(|l| !l.trim().is_empty()).collect();
        if !detail.is_empty() {
            col = col.child(
                div()
                    .id("detail")
                    .w_full()
                    .max_h(px(220.))
                    .overflow_y_scroll()
                    .p(px(10.))
                    .rounded(px(8.))
                    .bg(theme::code_bg())
                    .border_1()
                    .border_color(theme::border())
                    .font_family(theme::MONO)
                    .text_size(px(12.))
                    .child(detail.join("\n")),
            );
        }
        if failed {
            col = col.child(
                div()
                    .flex()
                    .gap(px(8.))
                    .child(icon_button("retry", if code == "crashed" { "Restart" } else { "Retry" }).on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.retry(cx))))
                    .child(icon_button("logs", "Show logs").on_click(|_, _, cx| {
                        if let Some(d) = logfile::log_dir() {
                            cx.reveal_path(&d);
                        }
                    }))
                    .child(icon_button("quit", "Quit").on_click(|_, _, cx| cx.quit())),
            );
        }
        div().size_full().flex().items_center().justify_center().child(col).into_any_element()
    }
}

impl Render for Workspace {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let t0 = Instant::now();
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
            .bg(theme::bg())
            .text_color(theme::text())
            .font_family(theme::FONT)
            .text_size(px(14.))
            .line_height(px(22.));

        if matches!(self.phase, Phase::Ready) && !self.projects.is_empty() && !self.logged_first_frame {
            self.logged_first_frame = true;
            logfile::event("ui.first_full_frame", &[("ms_since_start", self.started.elapsed().as_millis().to_string())]);
        }
        let el = if !matches!(self.phase, Phase::Ready) {
            root.child(self.render_status(cx)).into_any_element()
        } else {
            let main = div()
                .flex_1()
                .min_w_0()
                .h_full()
                .flex()
                .flex_col()
                .relative()
                .child(self.render_header(cx))
                .child(div().flex_1().min_h_0().flex().flex_col().child(self.render_transcript(cx)))
                .child(self.render_composer(cx))
                .children(self.render_options_menu(cx));
            root.when(self.sidebar, |d| d.child(self.render_sidebar(cx)))
                .child(main)
                .when(self.details, |d| d.child(self.render_details(cx)))
                .into_any_element()
        };
        let ms = t0.elapsed().as_secs_f64() * 1000.0;
        self.perf.render_count += 1;
        self.perf.render_ms_total += ms;
        self.perf.render_ms_max = self.perf.render_ms_max.max(ms);
        el
    }
}
