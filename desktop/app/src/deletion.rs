//! Focused, single-confirmation deletion. The comprehensive Start fresh picker
//! remains separate. A successful delete closes this dialog immediately.
use super::*;

#[derive(Clone, Debug)]
pub struct DeleteDialog {
    pub project: bool,
    pub id: String,
    pub title: String,
    pub preview: Option<api::ResetPreview>,
    pub error: Option<String>,
    pub deleting: bool,
}

impl DeleteDialog {
    fn selection(&self) -> Value {
        if self.project { json!({"delete_projects": [self.id]}) }
        else { json!({"chats": [self.id]}) }
    }
}

impl Workspace {
    pub fn open_deletion(&mut self, project: bool, id: String, title: String, cx: &mut Context<Self>) {
        self.chat_menu = None;
        self.project_menu = None;
        self.options_menu = false;
        self.deletion_seq += 1;
        let seq = self.deletion_seq;
        self.deletion = Some(DeleteDialog { project, id, title, preview: None, error: None, deleting: false });
        let selection = self.deletion.as_ref().unwrap().selection();
        self.api_call(cx, move |api| api.post::<api::ResetPreview>("/api/reset/preview", json!({"selection": selection})),
            move |this, result, _| {
                if this.deletion_seq != seq { return; }
                let Some(dialog) = this.deletion.as_mut() else { return };
                match result {
                    Ok(preview) => dialog.preview = Some(preview),
                    Err(error) => dialog.error = Some(error.message),
                }
            });
        cx.notify();
    }

    pub fn cancel_deletion(&mut self, cx: &mut Context<Self>) {
        if self.deletion.as_ref().map(|d| d.deleting).unwrap_or(false) { return; }
        self.deletion_seq += 1;
        self.deletion = None;
        cx.notify();
    }

    pub fn confirm_deletion(&mut self, cx: &mut Context<Self>) {
        let Some(dialog) = self.deletion.as_mut() else { return };
        let Some(preview) = dialog.preview.as_ref() else { return };
        if dialog.deleting || preview.empty { return; }
        let body = json!({"kind": if dialog.project { "project" } else { "chat" }, "id": dialog.id});
        dialog.deleting = true;
        dialog.error = None;
        let seq = self.deletion_seq;
        self.api_call(cx, move |api| api.post_long::<api::ResetDone>("/api/delete", body), move |this, result, cx| {
            if this.deletion_seq != seq { return; }
            match result {
                Ok(done) => {
                    // Remove the exact returned ids immediately, including archived
                    // rows; a slow refresh must not leave another Delete to click.
                    let gone = |id: &str| done.items.iter().any(|item| item.id == id);
                    this.background_runs.retain(|b| !gone(&b.run.chat_id));
                    this.unread_results.retain(|id, _| !gone(id));
                    if this.run.as_ref().map(|r| gone(&r.chat_id)).unwrap_or(false) { this.run = None; }
                    if this.chat.as_ref().map(|c| gone(&c.id)).unwrap_or(false)
                        || this.project_id.as_ref().map(|id| gone(id)).unwrap_or(false) {
                        this.new_chat(cx);
                    }
                    this.projects.retain(|p| !gone(&p.id));
                    this.chats.retain(|c| !gone(&c.id));
                    this.archived.retain(|c| !gone(&c.id));
                    if let Some(chats) = &mut this.settings_archived { chats.retain(|c| !gone(&c.id)); }
                    this.deletion = None;
                    this.deletion_seq += 1;
                    this.load_projects(None, cx);
                    if this.settings_open { this.load_settings_archived(cx); }
                }
                Err(error) => {
                    if let Some(dialog) = this.deletion.as_mut() {
                        dialog.deleting = false;
                        dialog.preview = error.preview.and_then(|p| serde_json::from_value(p).ok());
                        dialog.error = Some(if error.code == "preview_changed" {
                            "The contents changed while this dialog was open. Review the updated details before deleting.".into()
                        } else { error.message });
                    }
                }
            }
        });
        cx.notify();
    }

    pub(super) fn render_deletion(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        use theme::*;
        let dialog = self.deletion.as_ref()?;
        let title = if dialog.project { "Delete project?" } else { "Delete chat?" };
        let ready = dialog.preview.as_ref().map(|p| !p.empty).unwrap_or(false);
        let description = if dialog.project {
            let count = dialog.preview.as_ref().map(|p| p.items.iter().filter(|i| i.item_type == "chat").count());
            match count {
                Some(n) => format!("Delete “{}” and its {} chat{} (including archived chats), plus its project memory? This cannot be undone.",
                                   dialog.title, n, if n == 1 { "" } else { "s" }),
                None => format!("Delete “{}” and its chats and project memory? This cannot be undone.", dialog.title),
            }
        } else { format!("Delete “{}”? This chat will be permanently removed.", dialog.title) };
        let mut content = div().flex().flex_col().gap(px(12.))
            .child(div().text_size(fs_16()).font_weight(FontWeight::MEDIUM).text_color(text_strong()).child(title))
            .child(div().relative().child(crate::accessibility::text("deletion", "delete-description", description.clone())).child(description))
            .child(div().text_size(fs_12()).text_color(text_subtle()).child(if dialog.project {
                "Your source folders and files, personal memory, and activity records are kept."
            } else { "Your files, saved memory, and activity records are kept." }));
        if let Some(error) = &dialog.error {
            content = content.child(div().text_color(danger()).child(error.clone()));
        } else if dialog.preview.is_none() {
            content = content.child(div().text_color(text_subtle()).child("Loading…"));
        }
        if dialog.preview.as_ref().map(|p| !p.blocked.is_empty()).unwrap_or(false) {
            content = content.child(div().text_color(warning()).child("Running work in this item will be stopped and then deleted."));
        }
        content = content.child(div().flex().justify_end().gap(px(8.))
            .child(ui::pill("delete-cancel").child("Cancel")
                .accessible_click_in("deletion", "Cancel deletion", !dialog.deleting, cx.listener(|this, _: &ClickEvent, _, cx| this.cancel_deletion(cx))))
            .child(ui::pill("delete-confirm").child(if dialog.deleting { "Deleting…" } else { "Delete" })
                .when(ready && !dialog.deleting, |d| d.bg(danger()).text_color(bg_app()).border_color(danger()))
                .when(!ready || dialog.deleting, |d| d.text_color(text_subtle()).cursor_default())
                .accessible_click_in("deletion", "Delete permanently", ready && !dialog.deleting, cx.listener(|this, _: &ClickEvent, _, cx| this.confirm_deletion(cx)))));
        Some(div().id("delete-overlay").absolute().inset_0().flex().items_center().justify_center().bg(gpui::rgba(0x0000_0099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
            .child(div().id("delete-dialog").w(px(440.)).max_w_full().max_h_full().overflow_y_scroll().p(px(24.))
                .rounded(px(R_12)).bg(bg_raised()).border_1().border_color(border_strong()).shadow_lg().child(content))
            .into_any_element())
    }
}
