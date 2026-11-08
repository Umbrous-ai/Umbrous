//! Guided project setup, observable compatibility checks, and project grants.
use super::*;
use theme::*;

pub(super) fn permission_text(profile: &str) -> &'static str {
    match profile {
        "workspace_read" => "Can read files",
        "ask_before_read" => "Asks before reading files",
        "code_ask_commands" => "Can edit; asks before commands",
        "workspace_code" => "Can run commands without asking",
        _ => "Custom permissions",
    }
}

impl Workspace {
    pub(super) fn load_chat_activity(&mut self, cx: &mut Context<Self>) {
        let Some(chat) = self.chat.as_ref() else { self.chat_context = None; return };
        let id = chat.id.clone();
        let view = self.view;
        let path = format!("/api/tasks?chat_id={id}");
        self.api_call(cx, move |api| api.get::<Value>(&path), move |this, result, _| {
            if this.view != view || this.chat.as_ref().map(|c| &c.id) != Some(&id) { return }
            this.chat_context = Some(match result {
                Ok(v) => v["activity"].clone(),
                Err(e) => json!({"error":e.message}),
            });
        });
    }

    pub fn setup_stage(&self) -> Option<u8> { self.setup_step }
    pub fn setup_model_next(&mut self, cx: &mut Context<Self>) {
        if self.model.is_some() { self.setup_step = Some(1); } else { self.load_models(true, cx); }
        cx.notify();
    }
    pub fn activity_snapshot(&self) -> Value { self.chat_context.clone().unwrap_or(Value::Null) }

    pub(super) fn ensure_session_scope(&mut self, cx: &mut Context<Self>) {
        if self.loading_chat { return; }
        self.loading_chat = true;
        let view = self.view;
        self.api_call(cx, |api| api.post::<api::OneChat>("/api/chats", json!({})), move |this, result, cx| {
            if this.view != view { return; }
            this.loading_chat = false;
            match result {
                Ok(one) => { this.project_id = Some(one.chat.project_id.clone()); this.chat = Some(one.chat); this.load_chats(cx); }
                Err(e) => this.access_error = Some(e.message),
            }
        });
    }

    pub fn start_project_setup(&mut self, cx: &mut Context<Self>) {
        self.setup_step = Some(0);
        self.access_error = None;
        if self.project_id.is_none() {
            self.ensure_session_scope(cx);
        }
        cx.notify();
    }

    pub fn check_tools(&mut self, cx: &mut Context<Self>) {
        let Some(model) = self.model.clone() else { return };
        if self.probe_busy.is_some() { return }
        self.probe_seq += 1;
        let seq = self.probe_seq;
        self.probe_busy = Some(model.clone());
        self.probe_cancel_requested = false;
        if let Some(id) = self.probe_state.as_ref().filter(|v| v["stage"] == "connection" && v["model"].as_str() == Some(model.as_str())).and_then(|v| v["id"].as_str()).map(str::to_string) {
            self.api_call(cx, move |api| api.post::<Value>("/api/models/probe/status", json!({"id":id})), move |this, result, cx| this.receive_probe(seq, result, cx));
            cx.notify();
            return;
        }
        self.probe_state = Some(json!({"stage":"connecting", "detail":"Connecting to the model provider…", "model":model}));
        self.api_call(cx, move |api| api.post::<Value>("/api/models/probe/start", json!({"model":model})), move |this, result, cx| {
            this.receive_probe(seq, result, cx);
        });
        cx.notify();
    }

    fn receive_probe(&mut self, seq: u64, result: api::Result<Value>, cx: &mut Context<Self>) {
        if seq != self.probe_seq { return }
        match result {
            Err(e) => {
                let model = self.probe_busy.take();
                let id = if e.code == "unreachable" { self.probe_state.as_ref().and_then(|v| v["id"].as_str()).map(str::to_string) } else { None };
                self.probe_state = Some(json!({"id":id,"done":true,"passed":false,"stage":"connection", "detail":e.message, "model":model}));
            }
            Ok(v) => {
                let done = v["done"].as_bool().unwrap_or(false);
                let id = v["id"].as_str().unwrap_or("").to_string();
                let passed = v["passed"].as_bool().unwrap_or(false);
                let same_model = v["model"].as_str() == self.model.as_deref();
                self.probe_state = Some(v);
                if done {
                    self.probe_busy = None;
                    if passed && same_model && self.setup_step == Some(3) { self.setup_step = Some(4); }
                    self.load_models(false, cx);
                } else if self.probe_cancel_requested {
                    self.probe_cancel_requested = false;
                    self.api_call(cx, move |api| api.post::<Value>("/api/models/probe/cancel", json!({"id":id})), move |this, result, cx| this.receive_probe(seq, result, cx));
                } else {
                    cx.spawn(async move |this, cx| {
                        cx.background_executor().timer(Duration::from_millis(500)).await;
                        this.update(cx, |this, cx| {
                            if seq != this.probe_seq || this.probe_busy.is_none() { return }
                            this.api_call(cx, move |api| api.post::<Value>("/api/models/probe/status", json!({"id":id})), move |this, result, cx| this.receive_probe(seq, result, cx));
                        }).ok();
                    }).detach();
                }
            }
        }
    }

    pub fn cancel_probe(&mut self, cx: &mut Context<Self>) {
        if self.probe_busy.is_none() { return }
        // The next response supplies the ID even if Cancel was pressed during start.
        self.probe_cancel_requested = true;
        cx.notify();
    }

    pub(super) fn render_project_setup(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        let step = self.setup_step?;
        let mut card = div().w_full().p(px(16.)).rounded(px(R_12)).border_1().border_color(border()).bg(bg_raised()).flex().flex_col().gap(px(12.))
            .child(div().text_size(fs_16()).text_color(text_strong()).child(if step == 4 { "Ready—what would you like to do?" } else { "Start working with a folder" }))
            .child(div().text_size(fs_12()).child("Model → Folder → Access → Compatibility"));
        match step {
            0 => {
                card = card.child(div().child("Choose the model you want to work with using the model control below."))
                    .child(ui::pill("setup-model-next").child(format!("Use {}", self.selected_model_label())).accessible_click(format!("Use {}", self.selected_model_label()), true, cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.setup_model_next(cx);
                    })));
            }
            1 => {
                card = card.child(div().child("Choose the folder this chat can access."))
                    .child(ui::pill("setup-folder").child("Choose folder…").accessible_click("Choose folder", true, cx.listener(|this, _: &ClickEvent, _, cx| this.choose_folder("workspace_read", cx))));
                if let Some(folder) = self.folder() {
                    card = card.child(div().text_size(fs_12()).child(folder.root.clone()))
                        .child(ui::pill("setup-use-folder").child("Use this folder").accessible_click("Use this folder", true, cx.listener(|this, _: &ClickEvent, _, cx| { this.setup_step = Some(2); cx.notify(); })));
                }
            }
            2 => {
                for (i, (profile, _, _)) in access_controls::LEVELS.iter().enumerate() {
                    let profile = *profile;
                    card = card.child(ui::pill(["setup-read", "setup-write", "setup-full"][i]).child(permission_text(profile)).accessible_click(permission_text(profile), true, cx.listener(move |this, _: &ClickEvent, _, cx| this.choose_level(profile, cx))));
                }
                card = card.child(div().text_size(fs_12()).text_color(warning()).child("Commands run as you and are not sandboxed, including commands you approve."));
            }
            3 => { card = card.child(div().child("Checking tool compatibility. Ordinary chat is still available.")); }
            _ => { card = card.child(div().child("Your folder, access, and model compatibility are set. Type your request below.")); }
        }
        if let Some(error) = &self.access_error { card = card.child(div().text_color(danger()).child(error.clone())); }
        card = card.child(ui::pill("setup-chat-only").child(if step == 4 { "Done" } else { "Continue chatting" }).accessible_click(if step == 4 { "Done" } else { "Continue chatting" }, true, cx.listener(|this, _: &ClickEvent, _, cx| { this.setup_step = None; cx.notify(); })));
        Some(card.into_any_element())
    }

    pub(super) fn render_compatibility(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        let current = self.probe_state.as_ref().filter(|v| v["model"].as_str() == self.model.as_deref());
        let busy = self.probe_busy.is_some();
        let failed = current.map(|v| v["done"] == true && v["passed"] != true).unwrap_or(false);
        if !busy && !failed && !self.tools_blocked_by_check() { return None }
        let detail = if busy { self.probe_state.as_ref().and_then(|v| v["detail"].as_str()).unwrap_or("Connecting…") }
            else { current.and_then(|v| v["detail"].as_str()).unwrap_or("This model's tool compatibility has not been verified. You can still use ordinary chat.") };
        let mut card = div().w_full().p(px(12.)).rounded(px(R_12)).border_1().border_color(border()).flex().flex_col().gap(px(8.))
            .child(div().relative().text_color(if failed { warning() } else { text_default() }).child(detail.to_string()).child(crate::accessibility::text("main", "compatibility-status", detail.to_string())));
        if busy {
            card = card.child(div().text_size(fs_12()).child("Connecting → Tool call → Tool result. This can take up to six minutes."))
                .child(ui::pill("cancel-compatibility").child(if self.probe_cancel_requested { "Cancelling…" } else { "Cancel check" }).accessible_click("Cancel compatibility check", !self.probe_cancel_requested, cx.listener(|this, _: &ClickEvent, _, cx| this.cancel_probe(cx))));
        } else {
            let retry_connection = current.map(|v| matches!(v["stage"].as_str(), Some("connection" | "error" | "provider_unavailable" | "timeout"))).unwrap_or(false);
            card = card.child(div().flex().flex_wrap().gap(px(8.))
                .child(ui::pill("retry-compatibility").child(if retry_connection { "Retry connection" } else { "Check compatibility" }).accessible_click(if retry_connection { "Retry connection" } else { "Check compatibility" }, true, cx.listener(|this, _: &ClickEvent, _, cx| this.check_tools(cx))))
                .child(ui::pill("compatibility-model").child("Choose another model").accessible_click("Choose another model", true, cx.listener(|this, _: &ClickEvent, _, cx| this.load_models(true, cx)))));
        }
        Some(card.into_any_element())
    }

    pub(super) fn render_folder_pill(&self, _cx: &mut Context<Self>) -> Option<AnyElement> {
        let folder = self.folder()?;
        let path = folder.root.clone();
        Some(ui::pill("composer-folder").max_w(px(150.)).overflow_hidden().child(ui::icon("folder")).child(folder.name.clone())
            .on_click(move |_, _, cx| cx.reveal_path(std::path::Path::new(&path))).into_any_element())
    }

    pub(super) fn render_access_summary(&self) -> Option<AnyElement> {
        let profile = self.effective_profile()?;
        let text = permission_text(&profile);
        let commands = matches!(profile.as_str(), "code_ask_commands" | "workspace_code");
        Some(div().mt(px(6.)).text_size(fs_12()).text_color(text_subtle()).child(if commands { format!("{text}. Commands are not sandboxed.") } else { text.into() }).into_any_element())
    }

    pub fn open_permissions(&mut self, cx: &mut Context<Self>) {
        self.permissions_open = true;
        self.access_menu = false;
        self.load_access(cx);
        cx.notify();
    }

    pub fn revoke_permission(&mut self, id: String, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        let path = format!("/api/projects/{pid}/grants/{id}/revoke");
        self.api_call(cx, move |api| api.post::<api::Access>(&path, json!({})), move |this, result, _| {
            match result {
                Ok(access) if this.project_id.as_deref() == Some(pid.as_str()) => { this.access_seq += 1; this.access = Some(access); this.access_error = None; }
                Ok(_) => {},
                Err(e) => this.access_error = Some(e.message),
            }
        });
    }

    pub(super) fn render_permissions(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        if !self.permissions_open { return None }
        let mut card = div().id("permissions-dialog").w(px(520.)).max_h(px(560.)).overflow_y_scroll().p(px(24.)).rounded(px(R_12)).bg(bg_raised()).border_1().border_color(border()).flex().flex_col().gap(px(12.))
            .child(div().text_size(fs_16()).text_color(text_strong()).child(self.permissions_label()))
            .child(div().child("Revoking a grant affects future actions; it does not stop a command already running. Standalone chats keep their own grants; project chats share project grants."));
        if let Some(folder) = self.folder() { card = card.child(div().text_size(fs_12()).child(folder.root.clone())).child(div().child(format!("This chat: {}", permission_text(&self.effective_profile().unwrap_or_default())))); }
        let grants = self.access.as_ref().map(|a| a.grants.as_slice()).unwrap_or(&[]);
        if grants.is_empty() { card = card.child(div().child("No extra permissions granted. Actions follow the selected access level.")); }
        for (i, grant) in grants.iter().enumerate() {
            let id = grant["id"].as_str().unwrap_or("").to_string();
            let name = grant["capability"].as_str().unwrap_or("Permission").to_string();
            card = card.child(div().flex().items_center().gap(px(12.)).child(div().flex_1().child(name.clone()))
                .child(div().id(("revoke-grant", i)).px(px(10.)).py(px(6.)).rounded(px(R_8)).cursor_pointer().hover(|s| s.bg(bg_hover())).child("Revoke").accessible_click_in("permissions", format!("Revoke {name}"), true, cx.listener(move |this, _: &ClickEvent, _, cx| this.revoke_permission(id.clone(), cx)))));
        }
        card = card.child(div().text_size(fs_12()).child("Full access still allows commands after grants are revoked. Change the access level to require approval. Commands are not sandboxed."));
        if let Some(error) = &self.access_error { card = card.child(div().text_color(danger()).child(error.clone())); }
        card = card.child(ui::pill("permissions-close").child("Done").accessible_click_in("permissions", "Close permissions", true, cx.listener(|this, _: &ClickEvent, _, cx| { this.permissions_open = false; cx.notify(); })));
        Some(div().absolute().top_0().left_0().size_full().flex().items_center().justify_center().bg(gpui::rgba(0x00000099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation()).child(card).into_any_element())
    }
}
