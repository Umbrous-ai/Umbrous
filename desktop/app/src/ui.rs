//! Small building blocks matching the original interface's CSS classes
//! (prototypes/umbrous-harness.html). Each helper names the class it mirrors.

use gpui::{div, prelude::*, px, radians, svg, Div, Hsla, Stateful, Svg, Transformation};

use crate::theme::*;

/// `.i`: a 16px outline icon in the current colour.
pub fn icon(name: &str) -> Svg {
    svg().path(format!("icons/{name}.svg")).size(px(16.)).flex_none()
}

pub fn icon_sized(name: &str, size: f32) -> Svg {
    svg().path(format!("icons/{name}.svg")).size(px(size)).flex_none()
}

/// `.chev` rotated -90° when a section is collapsed.
pub fn chevron(open: bool) -> Svg {
    let s = icon_sized("chevron-down", 14.);
    if open { s } else { s.with_transformation(Transformation::rotate(radians(-std::f32::consts::FRAC_PI_2))) }
}

/// `.icon-btn`: 28×28, radius 6, subtle; hover raises the colour.
pub fn icon_btn(id: impl Into<gpui::ElementId>, name: &str, pressed: bool) -> Stateful<Div> {
    let color: Hsla = if pressed { text_strong().into() } else { text_subtle().into() };
    div()
        .id(id)
        .size(px(28.))
        .flex_none()
        .flex()
        .items_center()
        .justify_center()
        .rounded(px(R_6))
        .cursor_pointer()
        .text_color(color)
        .when(pressed, |d| d.bg(bg_selected()))
        .hover(|s| s.bg(bg_hover()).text_color(text_strong()))
        .child(icon(name).text_color(color))
}

/// `.nav`: 36px row, padding 0 10, gap 10, radius 8, 14px.
pub fn nav(id: impl Into<gpui::ElementId>, current: bool) -> Stateful<Div> {
    div()
        .id(id)
        .flex()
        .items_center()
        .gap(px(10.))
        .w_full()
        .h(px(36.))
        .px(px(10.))
        .rounded(px(R_8))
        .text_size(fs_14())
        .cursor_pointer()
        .text_color(if current { text_strong() } else { text_default() })
        .when(current, |d| d.bg(bg_selected()).font_weight(gpui::FontWeight::MEDIUM))
        .hover(|s| s.bg(if current { bg_selected() } else { bg_hover() }).text_color(text_strong()))
}

/// `.nav .t`
pub fn nav_title(text: impl Into<gpui::SharedString>) -> Div {
    div().flex_1().min_w_0().overflow_hidden().whitespace_nowrap().text_ellipsis().child(text.into())
}

/// `.nav .n`
pub fn nav_count(text: impl Into<gpui::SharedString>) -> Div {
    div().flex_none().text_size(fs_12()).text_color(text_subtle()).font_weight(gpui::FontWeight::NORMAL).child(text.into())
}

/// `.pill`: 28px, padding 0 10, full radius, 12px, default colour.
pub fn pill(id: impl Into<gpui::ElementId>) -> Stateful<Div> {
    div()
        .id(id)
        .flex()
        .items_center()
        .gap(px(6.))
        .h(px(28.))
        .px(px(10.))
        .border_1()
        .border_color(border())
        .rounded_full()
        .text_size(fs_12())
        .text_color(text_default())
        .cursor_pointer()
        .hover(|s| s.bg(bg_hover()).text_color(text_strong()))
}

/// `.notice` (+ `.warn` / `.bad`): 13px, subtle, 1px border, radius 8, padding 8 12.
pub fn notice(kind: Option<NoticeKind>) -> Div {
    let color = match kind {
        Some(NoticeKind::Warn) => warning(),
        Some(NoticeKind::Bad) => danger(),
        None => text_subtle(),
    };
    div()
        .flex()
        .items_center()
        .gap(px(12.))
        .text_size(fs_13())
        .line_height(px(19.5))
        .text_color(color)
        .border_1()
        .border_color(border())
        .rounded(px(R_8))
        .px(px(12.))
        .py(px(8.))
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum NoticeKind {
    Warn,
    Bad,
}

/// `.panel`: the floating panels on the canvas.
pub fn panel() -> Div {
    div().bg(bg_app()).border_1().border_color(border()).rounded(px(R_12)).min_h_0().flex().flex_col().overflow_hidden()
}

/// `.dot`
pub fn dot(color: impl Into<Hsla>) -> Div {
    div().size(px(6.)).rounded_full().flex_none().bg(color.into())
}

/// `.kv`: 28px row, key 72px subtle, value strong.
pub fn kv(key: &str, value: impl IntoElement) -> Div {
    div()
        .flex()
        .items_center()
        .gap(px(8.))
        .h(px(28.))
        .text_size(fs_13())
        .child(div().w(px(72.)).flex_none().text_color(text_subtle()).child(key.to_string()))
        .child(div().flex().items_center().gap(px(6.)).min_w_0().text_color(text_strong()).child(value))
}

/// `.rsec` with its `h4`.
pub fn rsec(title: &str, last: bool) -> Div {
    div()
        .py(px(SP_12))
        .when(!last, |d| d.border_b_1().border_color(border()))
        .flex()
        .flex_col()
        .child(div().text_size(fs_12()).text_color(text_subtle()).mb(px(SP_8)).child(title.to_string()))
}

/// `.row` in the Details panel: 32px, icon, title, trailing code.
pub fn rrow(icon_name: &str, title: &str) -> Div {
    div()
        .flex()
        .items_center()
        .gap(px(10.))
        .h(px(32.))
        .px(px(8.))
        .mx(px(-8.))
        .rounded(px(R_6))
        .text_size(fs_13())
        .text_color(text_default())
        .hover(|s| s.bg(bg_hover()))
        .child(icon(icon_name).text_color(text_subtle()))
        .child(div().flex_1().min_w_0().overflow_hidden().whitespace_nowrap().text_ellipsis().child(title.to_string()))
}

/// `.tg`: the 28×16 switch.
pub fn toggle(on: bool) -> Div {
    div()
        .w(px(28.))
        .h(px(16.))
        .flex_none()
        .rounded_full()
        .bg(if on { accent() } else { bg_selected() })
        .relative()
        .child(
            div()
                .absolute()
                .top(px(2.))
                .left(px(if on { 14. } else { 2. }))
                .size(px(12.))
                .rounded_full()
                .bg(if on { gpui::rgb(0xffffff) } else { text_subtle() }),
        )
}

/// `code` inside `.row`: 12px mono, subtle.
pub fn code_small(text: impl Into<gpui::SharedString>) -> Div {
    div().font_family(MONO).text_size(fs_12()).text_color(text_subtle()).child(text.into())
}
