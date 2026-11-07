//! Small building blocks matching the original interface's CSS classes
//! (prototypes/umbrous-harness.html). Each helper names the class it mirrors.

use gpui::{div, prelude::*, px, radians, svg, Div, Hsla, SharedString, Stateful, Svg, Transformation};

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

/// An icon that follows its row's colour: `color` normally, `hover` while the
/// pointer is over the element that declared `group` (CSS: the icon inherits
/// `currentColor`, which `:hover` changes; GPUI icons take an explicit colour).
pub fn hover_icon(name: &str, group: &SharedString, color: impl Into<Hsla>, hover: impl Into<Hsla>) -> Svg {
    let hover: Hsla = hover.into();
    icon(name).text_color(color.into()).group_hover(group.clone(), move |s| s.text_color(hover))
}

/// `.icon-btn`: 28×28, radius 6, subtle; `:hover` hover background and strong
/// colour; `[aria-pressed=true]` selected background and strong colour, also on hover.
pub fn icon_btn(id: &'static str, name: &str, pressed: bool) -> Stateful<Div> {
    let color: Hsla = if pressed { text_strong().into() } else { text_subtle().into() };
    let group = SharedString::from(id);
    div()
        .id(id)
        .group(group.clone())
        .size(px(28.))
        .flex_none()
        .flex()
        .items_center()
        .justify_center()
        .rounded(px(R_6))
        .cursor_pointer()
        .text_color(color)
        .when(pressed, |d| d.bg(bg_selected()))
        .hover(move |s| s.bg(if pressed { bg_selected() } else { bg_hover() }).text_color(text_strong()))
        .child(hover_icon(name, &group, color, text_strong()))
}

/// `.nav`: 36px row, padding 0 10, gap 10, radius 8, 14px. Its id is also its
/// hover group, so icons inside can use `hover_icon(…, &id, …)`.
pub fn nav(id: impl Into<SharedString>, current: bool) -> Stateful<Div> {
    let id: SharedString = id.into();
    div()
        .id(gpui::ElementId::Name(id.clone()))
        .group(id)
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

/// `.pill`: 28px, padding 0 10, full radius, 12px, default colour; `:hover`
/// hover background and strong colour. Icons inside: `hover_icon(…, &"<id>".into(), …)`.
pub fn pill(id: &'static str) -> Stateful<Div> {
    div()
        .id(id)
        .group(id)
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
