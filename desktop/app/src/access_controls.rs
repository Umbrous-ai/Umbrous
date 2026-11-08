//! The composer's access pill and menu (what the agent may do in the project's
//! folder, per chat, like Codex's per-session permission), approval cards for the
//! actions that access asks about, and the tool check that decides whether a
//! model gets tools at all.
//!
//! Each chat has its own access (one of the workspace profiles in
//! umbrous/gate/access.py), stored with the chat; a chat without one uses the
//! project's profile. The folder, grants and hard blocks stay the project's. The
//! gate reads the chat's access at every call; this menu only stores the choice.
use super::*;
use gpui::PathPromptOptions;

/// The three levels the menu offers (Freddy's design, after Codex): profile, name, icon.
/// Read only: list, read, search. Workspace write: also edit files; asks before each
/// command. Full access: also run commands without asking (as you, not sandboxed).
pub const LEVELS: [(&str, &str, &str); 3] = [
    ("workspace_read", "Read only", "shield-check"),
    ("code_ask_commands", "Workspace write", "shield-pen"),
    ("workspace_code", "Full access", "shield"),
];

/// Any profile's name and icon, including Ask before reads (set in the browser page
/// or the CLI), which the menu does not offer but a project may still use.
pub(super) fn level_label(profile: &str) -> &'static str {
    if profile == "ask_before_read" { return "Ask before reads" }
    LEVELS.iter().find(|l| l.0 == profile).map(|l| l.1).unwrap_or("Custom access")
}
fn level_icon(profile: &str) -> &'static str {
    LEVELS.iter().find(|l| l.0 == profile).map(|l| l.2).unwrap_or("shield-check")
}

/// The "Enable Full access?" dialog: open while Some; `checked` is its checkbox.
#[derive(Clone, Copy, Debug, Default)]
pub struct FullAccessConfirm {
    pub checked: bool,
}

/// One request from the gate, waiting for your answer (the event `approval_required`).
#[derive(Clone, Debug)]
pub struct PendingApproval {
    pub id: String,
    pub action: String,
    pub tool: String,
    pub args: Value,
    pub resource: String,
    /// an answer is on its way to the runtime
    pub answering: bool,
    pub error: Option<String>,
}

impl PendingApproval {
    pub(super) fn from_event(ev: &Value) -> Option<Self> {
        let s = |k: &str| ev.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let id = s("approval_id");
        if !id.starts_with("apr_") {
            return None;
        }
        Some(Self { id, action: s("action"), tool: s("tool"), args: ev.get("args").cloned().unwrap_or(Value::Null),
                    resource: s("resource"), answering: false, error: None })
    }
    fn kind(&self) -> &'static str {
        if self.action.starts_with("cmd.") { "command" } else if matches!(self.action.as_str(), "fs.write" | "fs.edit") { "change" } else { "read" }
    }
    fn grant_word(&self) -> &'static str {
        match self.action.as_str() {
            "fs.read" => "reads", "fs.list" => "listing", "fs.search" => "searches", "fs.find" => "finding files",
            "fs.write" => "writing files", "fs.edit" => "edits", "cmd.run" => "commands", "cmd.check" => "command checks",
            _ => "this",
        }
    }
}

impl Workspace {
    pub(super) fn permissions_label(&self) -> &'static str {
        if self.project_id.as_ref().map(|p| !p.starts_with("prj_session_")).unwrap_or(false) { "Manage project permissions" } else { "Manage chat permissions" }
    }
    pub(super) fn selected_model_info(&self) -> Option<&api::ModelInfo> {
        self.models.as_ref().and_then(|ms| ms.models.iter().find(|m| Some(&m.name) == self.model.as_ref()))
    }

    /// The selected project's folder, if its access is loaded.
    pub(super) fn folder(&self) -> Option<&api::AccessWorkspace> {
        self.access.as_ref().filter(|a| self.project_id.as_deref() == Some(a.project_id.as_str())).and_then(|a| a.workspace.as_ref())
    }

    /// A folder is attached but the chosen model would get no tools for it.
    pub fn tools_blocked_by_check(&self) -> bool {
        self.folder().is_some() && self.selected_model_info().map(|m| !m.tool_capable).unwrap_or(false)
    }

    /// The chat's own access: the open chat's, or the one chosen for the next new chat.
    pub fn chat_access(&self) -> Option<String> {
        match &self.chat {
            Some(c) => c.access.clone(),
            None => self.pending_access.clone(),
        }
    }

    /// What this chat's actions are decided by: its own access, else the project's profile.
    pub fn effective_profile(&self) -> Option<String> {
        self.folder().map(|w| self.chat_access().unwrap_or_else(|| w.profile.clone()))
    }

    /// Choose this chat's access (None: use the project's). For a chat that does not
    /// exist yet, it is sent when the chat is created with the first message.
    pub fn set_chat_access(&mut self, access: Option<&'static str>, cx: &mut Context<Self>) {
        self.access_menu = false;
        self.access_error = None;
        logfile::event("ui.chat_access", &[("to", access.unwrap_or("project").into())]);
        let Some(chat) = self.chat.clone() else {
            self.pending_access = access.map(str::to_string);
            if self.setup_step == Some(2) { self.setup_step = Some(3); self.check_tools(cx); }
            cx.notify();
            return;
        };
        let view = self.view;
        let advance_setup = self.setup_step == Some(2);
        let path = format!("/api/chats/{}/access", chat.id);
        self.api_call(cx, move |api| api.post::<api::OneChat>(&path, json!({ "access": access })), move |this, r, cx| match r {
            Ok(one) => {
                if let Some(c) = this.chat.as_mut().filter(|c| c.id == one.chat.id) { c.access = one.chat.access.clone(); }
                if let Some(c) = this.chats.iter_mut().find(|c| c.id == one.chat.id) { c.access = one.chat.access; }
                if this.view == view && advance_setup && this.setup_step == Some(2) { this.setup_step = Some(3); this.check_tools(cx); }
            }
            Err(e) => this.access_error = Some(e.message),
        });
        cx.notify();
    }

    /// The project's own profile (the default for its chats).
    pub fn set_profile(&mut self, profile: &'static str, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        logfile::event("ui.profile_switch", &[("to", profile.into())]);
        self.access_menu = false;
        self.access_error = None;
        let path = format!("/api/projects/{pid}/profile");
        self.api_call(cx, move |api| api.post::<api::Access>(&path, json!({ "profile": profile })), move |this, r, _| match r {
            Ok(a) if this.project_id.as_deref() == Some(a.project_id.as_str()) => {
                this.access_seq += 1; // a list answer already on its way is older than this
                this.access = Some(a);
            }
            Ok(_) => {}
            Err(e) => this.access_error = Some(e.message),
        });
        cx.notify();
    }

    /// Pick a folder for the project with the system's folder picker and attach it. The
    /// project's default for other chats is Read only; the level you chose is stored on
    /// this chat only. Picking the folder is the choice; nothing is attached otherwise.
    pub(super) fn choose_folder(&mut self, profile: &'static str, cx: &mut Context<Self>) {
        if self.project_id.is_none() {
            if self.loading_chat { return; }
            self.loading_chat = true;
            let view = self.view;
            self.api_call(cx, |api| api.post::<api::OneChat>("/api/chats", json!({})), move |this, result, cx| {
                if this.view != view { return; }
                this.loading_chat = false;
                match result {
                    Ok(one) => { this.project_id = Some(one.chat.project_id.clone()); this.chat = Some(one.chat); this.load_chats(cx); this.choose_folder(profile, cx); }
                    Err(e) => this.access_error = Some(e.message),
                }
            });
            return;
        }
        self.access_menu = false;
        let view = self.view;
        let project = self.project_id.clone();
        let rx = cx.prompt_for_paths(PathPromptOptions { files: false, directories: true, multiple: false,
                                                          prompt: Some("Use this folder".into()) });
        cx.spawn(async move |this, cx| {
            let Ok(Ok(Some(paths))) = rx.await else { return };
            let Some(root) = paths.into_iter().next() else { return };
            this.update(cx, |this, cx| {
                if this.view == view && this.project_id == project { this.attach_folder(root.to_string_lossy().to_string(), profile, cx); }
            }).ok();
        }).detach();
        cx.notify();
    }

    /// Attach `root` to the project (its default for other chats: Read only) and give
    /// the chat the choice was made in its level `profile`.
    pub fn attach_folder(&mut self, root: String, profile: &'static str, cx: &mut Context<Self>) {
        let Some(pid) = self.project_id.clone() else { return };
        logfile::event("ui.workspace_attach", &[("profile", profile.into())]);
        self.access_error = None;
        let path = format!("/api/projects/{pid}/workspace");
        // The transcript this choice was made in. `view` changes on New chat, opening a
        // chat and switching project, but not when an unsaved chat is saved by its first
        // message: an id alone cannot tell two unsaved chats apart (both None).
        let view_at_choice = self.view;
        let delay = self.e2e_attach_delay_ms;
        self.api_call(cx, move |api| {
            if delay > 0 { std::thread::sleep(std::time::Duration::from_millis(delay)); }
            api.post::<api::Access>(&path, json!({ "root": root, "profile": "workspace_read" }))
        }, move |this, r, cx| match r {
            Ok(a) if this.project_id.as_deref() == Some(a.project_id.as_str()) => {
                this.access_seq += 1;
                this.access = Some(a);
                // only if you are still in the chat you chose it for
                if this.view == view_at_choice {
                    this.set_chat_access(Some(profile), cx);
                    if this.setup_step == Some(1) { this.setup_step = Some(2); }
                }
            }
            Ok(_) => {}
            Err(e) => this.access_error = Some(e.message),
        });
        cx.notify();
    }

    /// Answer the waiting approval: `once`, `grant` (this kind of action, for this project) or `deny`.
    pub fn answer_approval(&mut self, choice: &'static str, cx: &mut Context<Self>) {
        let Some(ap) = self.approval.as_mut().filter(|a| !a.answering) else { return };
        ap.answering = true;
        ap.error = None;
        let id = ap.id.clone();
        logfile::event("ui.approval", &[("choice", choice.into())]);
        let path = format!("/api/approvals/{id}");
        self.api_call(cx, move |api| api.post::<Value>(&path, json!({ "choice": choice })), move |this, r, cx| {
            let Some(ap) = this.approval.as_mut().filter(|a| a.id == id) else { return };
            match r {
                // the card goes once the answer is in; the step's result follows on the stream
                Ok(_) => {
                    this.approval = None;
                    if choice == "grant" { this.load_access(cx); }
                }
                Err(e) => { ap.answering = false; ap.error = Some(format!("This request is closed: {}", e.message)); }
            }
        });
        cx.notify();
    }

    pub fn probe_running(&self) -> bool { self.probe_busy.is_some() }

    pub fn toggle_access_menu(&mut self, cx: &mut Context<Self>) {
        self.access_menu = !self.access_menu;
        self.model_menu = false;
        if self.access_menu { self.load_access(cx); }
        cx.notify();
    }

    pub fn access_pill_label(&self) -> String {
        match self.effective_profile() {
            None if self.access.is_none() && self.project_id.is_some() => "Loading access…".to_string(),
            None => "Chat only".to_string(),
            Some(p) => level_label(&p).to_string(),
        }
    }

    /// The composer's access pill: what the agent may do in this project's folder.
    /// A row of the menu was chosen. Full access asks first, in a dialog.
    pub fn choose_level(&mut self, profile: &'static str, cx: &mut Context<Self>) {
        self.access_menu = false;
        if profile == "workspace_code" && self.effective_profile().as_deref() != Some("workspace_code") {
            self.full_access_confirm = Some(FullAccessConfirm::default());
        } else if self.folder().is_some() {
            self.set_chat_access(Some(profile), cx);
        } else {
            self.choose_folder(profile, cx);
        }
        cx.notify();
    }

    pub fn confirm_full_access(&mut self, confirm: bool, cx: &mut Context<Self>) {
        let checked = self.full_access_confirm.take().map(|c| c.checked).unwrap_or(false);
        if confirm && checked {
            if self.folder().is_some() { self.set_chat_access(Some("workspace_code"), cx) } else { self.choose_folder("workspace_code", cx) }
        }
        cx.notify();
    }

    pub fn toggle_full_access_check(&mut self, cx: &mut Context<Self>) {
        if let Some(c) = self.full_access_confirm.as_mut() { c.checked = !c.checked; }
        cx.notify();
    }

    /// The dialog, over the whole window.
    pub(super) fn render_full_access_confirm(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        let c = self.full_access_confirm?;
        let folder = self.folder().map(|w| format!("the folder {}", w.name));
        let enable = ui::pill("full-access-enable").h(px(36.)).px(px(16.)).text_size(fs_13()).child("Enable Full access")
            .when(c.checked, |d| d.bg(text_strong()).text_color(bg_app()).border_color(text_strong()).hover(|s| s.bg(text_default()).text_color(bg_app())))
            .when(!c.checked, |d| d.bg(bg_selected()).text_color(text_subtle()).cursor_default().hover(|s| s.bg(bg_selected()).text_color(text_subtle())))
            .accessible_click_in("full-access", "Enable full access", c.checked, cx.listener(|this, _: &ClickEvent, _, cx| this.confirm_full_access(true, cx)));
        Some(div().id("full-access-overlay").absolute().top_0().left_0().size_full().flex().items_center().justify_center()
            .bg(gpui::rgba(0x0000_0099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
            .child(div().id("full-access-dialog").w(px(440.)).p(px(24.)).rounded(px(R_12)).bg(bg_raised())
                .border_1().border_color(border_strong()).shadow_lg().flex().flex_col().gap(px(16.))
                .child(div().flex().items_center()
                    .child(div().flex_1().text_size(fs_16()).font_weight(FontWeight::MEDIUM).text_color(text_strong()).child("Enable Full access?"))
                    .child(ui::icon_btn("full-access-close", "x", false)
                        .accessible_click_in("full-access", "Cancel full access", true, cx.listener(|this, _: &ClickEvent, _, cx| this.confirm_full_access(false, cx)))))
                .child(div().flex().items_start().gap(px(10.))
                    .child(ui::icon("shield").text_color(danger()).mt(px(2.)))
                    // a definite width (440 less padding, icon and gap): as a flex item the
                    // text was measured as one line and ran out of the dialog
                    .child(div().w(px(366.)).text_size(fs_13()).line_height(px(20.)).text_color(text_default()).child(format!(
                        "Full access lets the agent edit files and run commands in {} without asking you first. Commands run as you and are not sandboxed: they can do anything you can. This applies to this chat only. Use it only when you trust the current task.",
                        folder.as_deref().unwrap_or("the folder you pick next")))))
                .child(div().id("full-access-check").flex().items_center().gap(px(10.)).cursor_pointer()
                    .child(div().size(px(18.)).flex_none().rounded(px(R_6)).border_1().border_color(border_strong())
                        .flex().items_center().justify_center()
                        .when(c.checked, |d| d.bg(text_strong()).child(ui::icon_sized("check", 14.).text_color(bg_app()))))
                    .child(div().text_size(fs_13()).text_color(text_strong()).child("I understand the risks and want to continue"))
                    .accessible_click_in("full-access", if c.checked { "Risk acknowledgment selected" } else { "Acknowledge unsandboxed command risk" }, true, cx.listener(|this, _: &ClickEvent, _, cx| this.toggle_full_access_check(cx))))
                .child(div().flex().justify_end().gap(px(8.))
                    .child(ui::pill("full-access-cancel").h(px(36.)).px(px(16.)).text_size(fs_13()).child("Cancel")
                        .accessible_click_in("full-access", "Cancel full access", true, cx.listener(|this, _: &ClickEvent, _, cx| this.confirm_full_access(false, cx))))
                    .child(enable)))
            .into_any_element())
    }

    /// The pill with its menu, which opens above it.
    pub(super) fn render_access_control(&self, cx: &mut Context<Self>) -> AnyElement {
        div().relative().child(self.render_access_pill(cx)).children(self.render_access_menu(cx)).into_any_element()
    }

    fn render_access_pill(&self, cx: &mut Context<Self>) -> AnyElement {
        use theme::*;
        // Full access in red (Freddy); a model that gets no tools in amber
        let full = self.effective_profile().as_deref() == Some("workspace_code");
        let tint: Option<gpui::Rgba> = if self.tools_blocked_by_check() { Some(warning()) } else if full { Some(danger()) } else { None };
        let label = self.access_pill_label();
        let (c, h) = match tint { Some(t) => (t, t), None => (text_default(), text_strong()) };
        ui::pill("access-pill")
            .when_some(tint, |d, t| d.text_color(t).hover(move |s| s.bg(bg_hover()).text_color(t)))
            .child(ui::hover_icon(self.effective_profile().map(|p| level_icon(&p)).unwrap_or("shield-check"), &"access-pill".into(), c, h))
            .child(label)
            .child(ui::hover_icon("chevron-down", &"access-pill".into(), c, h))
            .accessible_click("Choose access level", true, cx.listener(|this, _: &ClickEvent, _, cx| this.toggle_access_menu(cx)))
            .into_any_element()
    }

    fn render_access_menu(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        if !self.access_menu || self.settings_open || self.review_open || self.permissions_open
            || self.full_access_confirm.is_some() || self.deletion.is_some() || self.reset.is_some() { return None }
        use theme::*;
        let note = |t: String| div().px(px(8.)).py(px(6.)).text_size(fs_12()).text_color(text_subtle()).child(t);
        let folder = self.folder().cloned();
        let mut menu = div().id("access-menu").absolute().bottom(px(36.)).left_0().w(px(360.))
            .max_h(px(480.)).overflow_y_scroll().p(px(6.)).rounded(px(R_12)).bg(bg_raised())
            .border_1().border_color(border_strong()).shadow_lg().flex().flex_col();
        let current = self.effective_profile();
        if folder.is_none() {
            menu = menu.child(note("No folder connected. Choose a folder before setting file access.".into()));
        }
        if folder.is_none() {
            menu = menu.child(ui::pill("access-setup-folder").child("Choose folder…").on_click(cx.listener(|this, _: &ClickEvent, _, cx| { this.access_menu = false; this.start_project_setup(cx); })));
        }
        for (i, (profile, _label, icon)) in LEVELS.iter().enumerate().filter(|_| folder.is_some()) {
            let on = current.as_deref() == Some(*profile);
            let profile: &'static str = profile;
            menu = menu.child(div().id(("access-row", i)).flex().items_center().gap(px(10.)).h(px(36.)).px(px(10.)).rounded(px(R_8))
                .cursor_pointer().text_size(fs_14()).text_color(text_strong()).hover(|s| s.bg(bg_hover()))
                .child(ui::icon(icon).text_color(if profile == "workspace_code" { danger() } else { text_default() }))
                .child(div().flex_1().child(super::project_setup::permission_text(profile)))
                .when(on, |d| d.child(ui::icon("check").text_color(text_strong())))
                .accessible_click(super::project_setup::permission_text(profile), true, cx.listener(move |this, _: &ClickEvent, _, cx| this.choose_level(profile, cx))));
        }
        if self.tools_blocked_by_check() {
            let model = self.selected_model_label();
            let busy = self.probe_busy.is_some();
            let cloud = self.selected_model_info().map(|m| m.is_cloud()).unwrap_or(false);
            menu = menu.child(div().flex().items_center().gap(px(8.)).px(px(8.)).py(px(6.))
                .child(div().flex_1().min_w_0().text_size(fs_12()).text_color(warning()).child(format!(
                    "{model} has not passed the tool check, so it gets no tools in this folder.{}",
                    if cloud { " The check is one short request on your subscription." } else { "" })))
                .child(ui::pill("check-tools").child(if busy { "Checking…" } else { "Check tools" })
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.check_tools(cx)))));
        }
        if let Some(e) = &self.access_error {
            menu = menu.child(div().px(px(8.)).py(px(6.)).text_size(fs_12()).text_color(danger()).child(e.clone()));
        }
        menu = menu.child(ui::pill("manage-access-permissions").child(self.permissions_label()).accessible_click(self.permissions_label(), true, cx.listener(|this, _: &ClickEvent, _, cx| this.open_permissions(cx))));
        Some(gpui::deferred(menu.occlude()).with_priority(1).into_any_element())
    }

    /// The waiting approval, as a card above the composer.
    pub(super) fn render_approval(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        let ap = self.approval.as_ref()?;
        let title = match ap.kind() { "command" => "Run this command?", "change" => "Allow this change?", _ => "Allow this read?" };
        let what = api::step_label(&ap.tool, &ap.args);
        let busy = ap.answering;
        let btn = |id: &'static str, text: String, choice: &'static str, primary: bool| {
            ui::pill(id).child(text)
                .when(primary, |d| d.bg(text_strong()).text_color(bg_app()).border_color(text_strong()).hover(|s| s.bg(text_default()).text_color(bg_app())))
                .when(busy, |d| d.opacity(0.5))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.answer_approval(choice, cx)))
        };
        // The exact command the approval authorizes, verbatim (never the shortened step
        // label): a long or multi-line command scrolls instead of being cut.
        let command = (ap.kind() == "command").then(|| ap.args.get("command").and_then(Value::as_str).unwrap_or("").to_string());
        let background = ap.args.get("background").and_then(Value::as_bool).unwrap_or(false);
        // block layout (not a flex column): text in flex items was measured unwrapped
        Some(div().id("approval-card").w_full().min_w_0().overflow_hidden().px(px(12.)).py(px(10.))
            .rounded(px(R_8)).border_1().border_color(border_strong()).bg(bg_raised())
            .text_size(fs_13()).line_height(px(19.5)).text_color(text_default())
            .child(div().w_full().text_color(text_strong()).font_weight(FontWeight::MEDIUM).child(title))
            .when_some(command.clone(), |d, cmd| d.child(
                div().id("approval-command").mt(px(6.)).w_full().max_h(px(180.)).overflow_y_scroll()
                    .px(px(10.)).py(px(8.)).rounded(px(R_6)).bg(bg_app()).border_1().border_color(border())
                    .font_family(MONO).text_size(fs_12()).line_height(px(18.)).text_color(text_strong())
                    .child(div().w_full().child(if cmd.is_empty() { "(empty command)".to_string() } else { breakable(&cmd) }))))
            .when(command.is_none(), |d| d.child(div().mt(px(6.)).w_full().child(format!("{}{}",
                what.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default(), what.chars().skip(1).collect::<String>()))))
            .child(div().mt(px(6.)).w_full().text_size(fs_12()).text_color(text_subtle()).font_family(MONO)
                .child(match ap.resource.as_str() { "" | "." => format!("{} in the selected folder", ap.action), r => format!("{} on {r}", ap.action) }))
            .when(command.is_some(), |d| d.child(div().mt(px(6.)).w_full().text_size(fs_12()).text_color(warning()).child(format!(
                "It runs as you, with /bin/sh in the selected folder, and is not sandboxed: it can do anything you can.{}",
                if background { " It may keep running in the background." } else { "" }))))
            .child(div().mt(px(8.)).flex().flex_wrap().gap(px(6.))
                .child(btn("approve-once", "Approve once".into(), "once", true))
                .child(btn("approve-grant", format!("Allow {} in this {}", ap.grant_word(), if self.project_id.as_ref().map(|p| p.starts_with("prj_session_")).unwrap_or(true) { "chat" } else { "project" }), "grant", false))
                .child(btn("approve-deny", "Deny".into(), "deny", false))
                .child(ui::pill("manage-approval-permissions").child(self.permissions_label()).accessible_click(self.permissions_label(), true, cx.listener(|this, _: &ClickEvent, _, cx| this.open_permissions(cx)))))
            .when_some(ap.error.clone(), |d, e| d.child(div().mt(px(6.)).w_full().text_size(fs_12()).text_color(danger()).child(e)))
            .into_any_element())
    }
}

/// The command as shown on the card: the same characters, with a zero-width break
/// opportunity every 32 characters of a long unbroken run, so a long path or token
/// wraps inside the card instead of widening it (the text is never shortened).
fn breakable(cmd: &str) -> String {
    let mut out = String::with_capacity(cmd.len() + cmd.len() / 16);
    let mut run = 0;
    for ch in cmd.chars() {
        if ch.is_whitespace() { run = 0 } else {
            if run == 32 { out.push('\u{200B}'); run = 0; }
            run += 1;
        }
        out.push(ch);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_long_command_keeps_every_character() {
        let cmd = format!("true {} && make test\necho tail", "x".repeat(100));
        let shown = breakable(&cmd);
        assert_eq!(shown.replace('\u{200B}', ""), cmd);
        assert!(shown.split(|c: char| c.is_whitespace() || c == '\u{200B}').all(|w| w.chars().count() <= 32));
    }
}
