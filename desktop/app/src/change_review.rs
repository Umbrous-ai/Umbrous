//! Read-only review of the current folder, separate from this chat's activity.
use super::*;
use crate::accessibility::AccessibleClick;
use theme::*;

impl Workspace {
    pub fn review_snapshot(&self) -> Value { self.review_data.clone().unwrap_or(Value::Null) }

    pub fn open_change_review(&mut self, cx: &mut Context<Self>) {
        let Some(chat) = self.chat.as_ref() else { return };
        let id = chat.id.clone();
        self.review_open = true;
        self.review_loading = true;
        self.review_data = None;
        self.review_file = 0;
        self.review_seq += 1;
        let seq = self.review_seq;
        let view = self.view;
        self.api_call(cx, move |api| api.get::<Value>(&format!("/api/tasks/review?chat_id={id}")), move |this, result, _| {
            if this.review_seq != seq || this.view != view { return; }
            this.review_loading = false;
            this.review_data = Some(match result {
                Ok(data) => data,
                Err(error) => json!({"available":false,"message":error.message}),
            });
        });
        cx.notify();
    }

    pub(super) fn render_change_review(&self, cx: &mut Context<Self>) -> Option<AnyElement> {
        if !self.review_open { return None; }
        let mut card = div().w_full().h_full().max_w(px(1000.)).max_h(px(720.)).p(px(24.)).rounded(px(R_12))
            .bg(bg_raised()).border_1().border_color(border_strong()).flex().flex_col().gap(px(12.))
            .child(div().flex().items_center().justify_between()
                .child(div().text_size(fs_18()).text_color(text_strong()).child("Folder changes"))
                .child(ui::pill("review-close").child("Done").accessible_click_in("review", "Close folder changes", true, cx.listener(|this, _: &ClickEvent, _, cx| { this.review_open = false; cx.notify(); }))))
            .child(div().text_size(fs_12()).child("Current changes against Git HEAD, including work outside this chat. Reviewing does not modify files or stage changes."));
        if self.review_loading {
            card = card.child(div().child("Reading folder changes…"));
        } else if let Some(data) = &self.review_data {
            if let Some(message) = data["message"].as_str() { card = card.child(div().child(message.to_string())); }
            let files = data["files"].as_array();
            if data["available"] == true && files.map(|f| f.is_empty()).unwrap_or(false) {
                card = card.child(div().child(if data["truncated"] == true || data["omitted"].as_u64().unwrap_or(0) > 0 { "No changes in the files reviewed. Some files were omitted." } else { "No folder changes found." }));
            }
            if let Some(files) = files.filter(|f| !f.is_empty()) {
                let mut names = div().id("review-files").w(px(230.)).flex_none().h_full().overflow_y_scroll().flex().flex_col().gap(px(4.));
                for (i, file) in files.iter().enumerate() {
                    let path = file["path"].as_str().unwrap_or("File").to_string();
                    names = names.child(ui::nav(SharedString::from(format!("review-file-{i}")), self.review_file == i)
                        .child(ui::nav_title(path)).child(ui::nav_count(file["status"].as_str().unwrap_or("" ).to_string()))
                        .accessible_click_in("review", format!("Review {}", file["path"].as_str().unwrap_or("file")), true, cx.listener(move |this, _: &ClickEvent, _, cx| { this.review_file = i; cx.notify(); })));
                }
                let file = &files[self.review_file.min(files.len() - 1)];
                let mut patch = div().relative().id("review-patch").flex_1().min_w_0().h_full().overflow_y_scroll().p(px(12.)).bg(bg_app()).rounded(px(R_8)).flex().flex_col()
                    .child(div().text_color(text_strong()).mb(px(12.)).child(file["path"].as_str().unwrap_or("").to_string()));
                patch = patch.child(crate::accessibility::text("review", "review-patch-text", format!("{}\n{}\n{}", file["path"].as_str().unwrap_or(""), file["patch"].as_str().unwrap_or(""), file["error"].as_str().unwrap_or(""))));
                if file["binary"] == true { patch = patch.child(div().child("Binary file — no text preview.")); }
                if let Some(error) = file["error"].as_str() { patch = patch.child(div().text_color(warning()).child(error.to_string())); }
                if let Some(text) = file["patch"].as_str() {
                    for line in text.lines() {
                        let color = if line.starts_with('+') { success() } else if line.starts_with('-') { danger() } else { text_default() };
                        patch = patch.child(div().font_family("Menlo").text_size(fs_12()).text_color(color).child(if line.is_empty() { " " } else { line }.to_string()));
                    }
                }
                if file["truncated"] == true { patch = patch.child(div().text_color(warning()).child("Preview truncated. Open the folder to inspect the complete change.")); }
                card = card.child(div().flex_1().min_h_0().flex().gap(px(12.)).child(names).child(patch));
            }
            if data["truncated"] == true {
                card = card.child(div().text_color(warning()).child("Some changes are omitted from this bounded preview. Review the folder for the complete diff."));
            }
            card = card.child(ui::pill("review-refresh").child("Refresh changes").accessible_click_in("review", "Refresh changes", true, cx.listener(|this, _: &ClickEvent, _, cx| this.open_change_review(cx))));
        }
        Some(div().absolute().top_0().left_0().size_full().p(px(32.)).flex().items_center().justify_center().bg(gpui::rgba(0x00000099))
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation()).child(card).into_any_element())
    }
}
