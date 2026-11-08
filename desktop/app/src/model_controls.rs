//! Provider selection, capability-driven inference controls, and local preferences.
//! Credentials remain owned by Codex; this module stores only UI choices.
use super::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::{Mutex, atomic::AtomicU64};

#[derive(Default, Clone, Serialize, Deserialize)]
pub(super) struct Preferences {
    #[serde(default)]
    selected: Option<String>,
    #[serde(default)]
    controls: BTreeMap<String, api::ModelControls>,
}
static SAVE_LOCK: Mutex<()> = Mutex::new(());
static SAVE_VERSION: AtomicU64 = AtomicU64::new(0);
impl Preferences {
    fn load() -> Self {
        let Some(home) = logfile::umbrous_home() else { return Self::default() };
        let path = home.join("desktop-models.json");
        if fs::metadata(&path).map(|m| m.len() > 65_536).unwrap_or(true) { return Self::default() }
        fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }
    fn save(&self, version: u64) {
        let Ok(_lock) = SAVE_LOCK.lock() else { return };
        if version != SAVE_VERSION.load(Ordering::SeqCst) { return }
        let Some(home) = logfile::umbrous_home() else { return };
        let Ok(data) = serde_json::to_vec(self) else { return };
        let tmp = home.join(format!(".desktop-models-{}-{version}", std::process::id()));
        let result = (|| -> std::io::Result<()> {
            let mut f = OpenOptions::new().create_new(true).write(true).mode(0o600).open(&tmp)?;
            f.write_all(&data)?;
            f.sync_all()?;
            fs::rename(&tmp, home.join("desktop-models.json"))
        })();
        if result.is_err() { let _ = fs::remove_file(tmp); }
    }
    fn for_model(&self, model: &api::ModelInfo) -> api::ModelControls {
        self.controls.get(&model.name).cloned().unwrap_or_else(|| api::ModelControls {
            reasoning_effort: model.default_reasoning_effort.clone().filter(|x| model.reasoning_efforts.contains(x)),
            speed: "standard".into(),
        })
    }
}

/// Only the official HTTPS sign-in hosts may be opened by the native client.
fn official_login_url(raw: &str) -> bool {
    if raw.chars().any(|c| c.is_control() || c.is_whitespace() || c == '\\') { return false }
    let Some(rest) = raw.strip_prefix("https://") else { return false };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    matches!(authority, "auth.openai.com" | "auth.openai.com:443" | "chatgpt.com" | "chatgpt.com:443")
}

impl Workspace {
    pub(super) fn load_models(&mut self, open_menu: bool, cx: &mut Context<Self>) {
        self.models_seq += 1;
        let seq = self.models_seq;
        let load_preferences = !self.model_preferences_loaded;
        self.model_preferences_loaded = true;
        self.api_call(cx, move |api| {
            let prefs = load_preferences.then(Preferences::load);
            let models = api.get::<api::Models>("/api/models")?;
            let status = api.get::<api::CodexStatus>("/api/providers/codex").unwrap_or_default();
            Ok((models, status, prefs))
        }, move |this, r, cx| {
            if seq != this.models_seq { return }
            let (m, status, prefs) = r.unwrap_or_else(|e| (
                api::Models { reachable: false, version: None, error: Some(e.message), models: vec![] },
                api::CodexStatus::default(), None));
            if let Some(prefs) = prefs {
                if this.model.is_none() { this.model = prefs.selected.clone(); }
                this.model_preferences = prefs;
            }
            this.codex_status = status;
            if !m.reachable {
                this.notice = Some((m.error.clone().unwrap_or_else(|| "No model provider is connected. Start Ollama or connect ChatGPT.".into()), MetaKind::Warn, true));
            } else if matches!(this.notice, Some((_, _, true))) { this.notice = None; }
            if m.reachable {
                if let Some(sel) = &this.model {
                    if !m.models.iter().any(|x| &x.name == sel) {
                        this.notice = Some((format!("The model {sel} is unavailable. Pick a model to continue."), MetaKind::Warn, false));
                        this.model = None;
                    }
                }
                // Never choose cloud access implicitly: the user must choose ChatGPT.
                if this.model.is_none() && this.notice.is_none() {
                    this.model = m.models.iter().find(|x| !x.is_cloud() && x.tool_capable)
                        .or_else(|| m.models.iter().find(|x| !x.is_cloud())).map(|x| x.name.clone());
                }
                if let Some(md) = m.models.iter().find(|x| Some(&x.name) == this.model.as_ref()) {
                    let current = this.model_preferences.for_model(md);
                    if md.is_cloud() && !controls_supported(md, &current) {
                        this.notice = Some(("The saved reasoning or speed choice is no longer supported. Choose supported settings in the model menu before sending.".into(), MetaKind::Warn, false));
                    }
                }
            }
            this.models = Some(m);
            if open_menu { this.model_menu = true; }
            cx.notify();
        });
    }
    pub(super) fn provider_up(&self) -> bool {
        self.models.as_ref().map(|m| m.reachable && self.model.as_ref().map(|name|
            m.models.iter().any(|x| &x.name == name && (!x.is_cloud() || controls_supported(x, &self.model_preferences.for_model(x)))))
            .unwrap_or(true)).unwrap_or(false)
    }
    fn persist_model_preferences(&mut self, cx: &mut Context<Self>) {
        self.model_preferences.selected = self.model.clone();
        let prefs = self.model_preferences.clone();
        let version = SAVE_VERSION.fetch_add(1, Ordering::SeqCst) + 1;
        cx.background_spawn(async move { prefs.save(version) }).detach();
    }
    pub(super) fn current_model_controls(&self) -> api::ModelControls {
        self.models.as_ref().and_then(|ms| ms.models.iter().find(|m| Some(&m.name) == self.model.as_ref()))
            .map(|m| self.model_preferences.for_model(m)).unwrap_or_default()
    }
    pub(super) fn selected_model_label(&self) -> String {
        self.models.as_ref().and_then(|ms| ms.models.iter().find(|m| Some(&m.name) == self.model.as_ref()))
            .map(|m| m.label().to_string()).unwrap_or_else(|| "Pick a model".into())
    }
    fn set_model_control(&mut self, effort: Option<String>, speed: Option<String>, cx: &mut Context<Self>) {
        let Some(name) = self.model.clone() else { return };
        let mut current = self.current_model_controls();
        if let Some(effort) = effort { current.reasoning_effort = Some(effort); }
        if let Some(speed) = speed { current.speed = speed; }
        self.model_preferences.controls.insert(name, current);
        self.persist_model_preferences(cx);
        cx.notify();
    }
    fn connect_chatgpt(&mut self, cx: &mut Context<Self>) {
        if self.codex_login_busy { return }
        self.codex_login_busy = true;
        self.api_call(cx, |api| api.post::<api::CodexLogin>("/api/providers/codex/login", json!({})), |this, r, cx| {
            this.codex_login_busy = false;
            match r {
                Ok(login) if official_login_url(&login.auth_url) => {
                    cx.open_url(&login.auth_url);
                    this.notice = Some(("Finish signing in with ChatGPT in your browser, then press Refresh in the model menu. Credentials stay with Codex.".into(), MetaKind::Normal, false));
                }
                Ok(_) => this.notice = Some(("The provider returned an unexpected sign-in address. Nothing was opened.".into(), MetaKind::Bad, false)),
                Err(e) => this.notice = Some((e.message, MetaKind::Bad, false)),
            }
        });
    }
    pub(super) fn render_model_controls(&self, _cx: &mut Context<Self>) -> Vec<AnyElement> {
        let cloud = self.models.as_ref().and_then(|ms| ms.models.iter().find(|m| Some(&m.name) == self.model.as_ref())).map(|m| m.is_cloud()).unwrap_or(false);
        if !cloud { return vec![] }
        let controls = self.current_model_controls();
        let effort = controls.reasoning_effort.as_deref().unwrap_or("default");
        let mut chars = effort.chars();
        let label = chars.next().map(|c| c.to_uppercase().to_string() + chars.as_str()).unwrap_or_default();
        vec![div().text_color(theme::text_subtle()).child(label).into_any_element()]

    }
    pub(super) fn render_model_menu(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        if !self.model_menu { return None }
        use theme::*;
        let mut menu = div().id("model-menu").absolute().bottom(px(36.)).left(px(32.)).w(px(420.))
            .max_h(px(480.)).overflow_y_scroll().p(px(6.)).rounded(px(R_12)).bg(bg_raised())
            .border_1().border_color(border_strong()).shadow_lg().flex().flex_col();
        let note = |t: String| div().p(px(8.)).text_size(fs_12()).text_color(text_subtle()).child(t);
        menu = menu.child(div().flex().items_center().gap(px(8.)).p(px(6.))
            .child(ui::pill("connect-chatgpt").child(if self.codex_login_busy { "Opening sign-in…" } else if self.codex_status.authenticated { "ChatGPT connected" } else { "Connect ChatGPT" })
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.connect_chatgpt(cx))))
            .child(ui::pill("refresh-models").child("Refresh").on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.load_models(true, cx)))));
        if let Some(error) = &self.codex_status.error { menu = menu.child(note(error.clone())); }
        if !self.codex_status.available { menu = menu.child(note("ChatGPT requires the Codex CLI. Install or enable it, then Refresh.".into())); }
        if self.codex_status.authenticated && self.codex_status.auth_type.as_deref().map(|x| x != "chatgpt").unwrap_or(false) {
            menu = menu.child(note("Connect with ChatGPT to use subscription allowance; API-key billing is not selected here.".into()));
        }
        let models = self.models.as_ref().map(|m| m.models.as_slice()).unwrap_or(&[]);
        if models.is_empty() { menu = menu.child(note("No models available. Start Ollama for local models, or connect ChatGPT.".into())); }
        for (i, md) in models.iter().enumerate() {
            let name = md.name.clone();
            let provider = if md.is_cloud() { "ChatGPT · cloud" } else { "Ollama · local" };
            let label = if md.tool_capable { "tool calls verified" } else { "compatibility not verified" };
            let sub = [provider.to_string(), md.parameter_size.clone(), md.context_length.map(|c| format!("ctx {}", fmt_k(c))).unwrap_or_default(), label.to_string()].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · ");
            let on = self.model.as_ref() == Some(&name);
            let cloud = md.is_cloud();
            menu = menu.child(div().id(("mm-row", i)).flex().flex_col().px(px(8.)).py(px(6.)).rounded(px(R_8)).cursor_pointer()
                .when(on, |d| d.bg(bg_selected())).hover(|s| s.bg(bg_selected()))
                .child(div().text_size(fs_13()).text_color(text_strong()).child(md.label().to_string()))
                .child(div().text_size(fs_12()).text_color(text_subtle()).child(sub))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                    this.model = Some(name.clone());
                    this.model_menu = false;
                    if matches!(this.notice, Some((_, MetaKind::Warn, false))) { this.notice = None; }
                    if cloud { this.notice = Some(("ChatGPT uses your subscription. The conversation, selected memories, and approved tool results are sent to OpenAI.".into(), MetaKind::Normal, false)); }
                    this.persist_model_preferences(cx);
                    cx.notify();
                })));
            if on && md.is_cloud() {
                let current = self.model_preferences.for_model(md);
                menu = menu.child(note("Reasoning effort".into()));
                let mut row = div().flex().flex_wrap().gap(px(4.)).px(px(8.));
                for (n, effort) in md.reasoning_efforts.iter().enumerate() {
                    let value = effort.clone();
                    row = row.child(div().id(("reasoning-option", n)).px(px(7.)).py(px(4.)).rounded(px(R_8)).cursor_pointer().text_size(fs_12()).hover(|s| s.bg(bg_selected())).child(value.clone())
                        .when(current.reasoning_effort.as_ref() == Some(effort), |d| d.bg(bg_selected()))
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_model_control(Some(value.clone()), None, cx))));
                }
                menu = menu.child(row).child(note("Speed · Fast uses more subscription allowance".into()));
                let mut row = div().flex().gap(px(4.)).px(px(8.));
                for (n, speed) in ["standard", "fast"].iter().enumerate() {
                    let enabled = md.speeds.iter().any(|x| x == speed);
                    let value = speed.to_string();
                    row = row.child(div().id(("speed-option", n)).px(px(7.)).py(px(4.)).rounded(px(R_8)).cursor_pointer().text_size(fs_12()).hover(|s| s.bg(bg_selected())).child(if *speed == "fast" { "Fast" } else { "Standard" })
                        .when(current.speed == *speed, |d| d.bg(bg_selected()))
                        .when(!enabled, |d| d.opacity(0.4))
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                            if enabled { this.set_model_control(None, Some(value.clone()), cx); }
                        })));
                }
                menu = menu.child(row).child(note("Your conversation and tool results are sent to OpenAI using your ChatGPT subscription.".into()));
            }
        }
        menu = menu.child(note("Choose the provider and model explicitly. Umbrous never switches models for you.".into()));
        Some(menu.into_any_element())
    }
}
fn controls_supported(model: &api::ModelInfo, settings: &api::ModelControls) -> bool {
    settings.reasoning_effort.as_ref().map(|x| model.reasoning_efforts.contains(x)).unwrap_or(true)
        && model.speeds.contains(&settings.speed)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sign_in_links_are_official_https_only() {
        for url in ["https://auth.openai.com/oauth/authorize?state=x", "https://chatgpt.com/auth/login", "https://auth.openai.com:443/x"] { assert!(official_login_url(url), "{url}"); }
        for url in ["http://auth.openai.com/x", "https://evil.test/auth.openai.com", "https://auth.openai.com.evil.test", "https://auth.openai.com@evil.test", "https://evil@auth.openai.com/", "file:///tmp/x", "https://auth.openai.com:444/x"] { assert!(!official_login_url(url), "{url}"); }
    }
    #[test]
    fn inference_preferences_are_model_scoped_and_capability_checked() {
        let a: api::ModelInfo = serde_json::from_value(json!({"name":"codex/a", "reasoning_efforts":["low","high"], "default_reasoning_effort":"low", "speeds":["standard","fast"]})).unwrap();
        let b: api::ModelInfo = serde_json::from_value(json!({"name":"codex/b", "reasoning_efforts":["medium"], "default_reasoning_effort":"medium", "speeds":["standard"]})).unwrap();
        let mut prefs = Preferences::default();
        prefs.controls.insert(a.name.clone(), api::ModelControls { reasoning_effort: Some("high".into()), speed: "fast".into() });
        assert!(controls_supported(&a, &prefs.for_model(&a)));
        assert_eq!(prefs.for_model(&b).reasoning_effort.as_deref(), Some("medium"));
        assert_eq!(prefs.for_model(&b).speed, "standard");
        assert!(!controls_supported(&b, &prefs.for_model(&a)));
    }
}
