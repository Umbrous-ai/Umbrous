//! The transcript: messages, their parsed blocks, text selection, and rendering.
//!
//! * Virtualised: the workspace shows it with GPUI's `list`, which lays out
//!   only the rows on screen (plus a margin), so a 10,000-message chat costs
//!   about the same per frame as a short one.
//! * Markdown is parsed on a background thread. While a reply streams, deltas
//!   are batched (at most one update per ~33 ms) and only the streaming row is
//!   re-measured (`ListState::splice` of one row); the rest of the transcript
//!   is never rebuilt.
//! * Selection spans blocks and messages: press and drag anywhere in the
//!   transcript; Cmd-C copies plain text. Double-click selects a word, triple-
//!   click a block.

use std::ops::Range;
use std::sync::Arc;

use gpui::{
    div, prelude::*, px, AnyElement, App, Entity, FontStyle, FontWeight, HighlightStyle, MouseButton,
    SharedString, StrikethroughStyle, StyledText, UnderlineStyle,
};
use unicode_segmentation::UnicodeSegmentation;

use crate::markdown::{Block, BlockKind, Inline};
use crate::theme;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Pos {
    pub msg: usize,
    pub block: usize,
    pub offset: usize,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Selection {
    pub anchor: Option<Pos>,
    pub head: Option<Pos>,
    pub dragging: bool,
}

impl Selection {
    pub fn range(&self) -> Option<(Pos, Pos)> {
        let (a, h) = (self.anchor?, self.head?);
        if a == h {
            return None;
        }
        Some(if a < h { (a, h) } else { (h, a) })
    }
    /// The selected byte range inside one block, if any.
    pub fn in_block(&self, msg: usize, block: usize, len: usize) -> Option<Range<usize>> {
        let (s, e) = self.range()?;
        let here = (msg, block);
        if here < (s.msg, s.block) || here > (e.msg, e.block) {
            return None;
        }
        let start = if here == (s.msg, s.block) { s.offset } else { 0 };
        let end = if here == (e.msg, e.block) { e.offset } else { len };
        (start < end).then(|| start.min(len)..end.min(len))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Role {
    User,
    Assistant,
}

pub struct Msg {
    pub role: Role,
    pub text: String,
    pub blocks: Arc<Vec<Block>>,
    /// the text `blocks` was parsed from (lags `text` while streaming)
    pub parsed_len: usize,
    pub meta: String,
    pub meta_kind: MetaKind,
    pub streaming: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum MetaKind {
    Normal,
    Warn,
    Bad,
}

pub fn selected_text(msgs: &[Msg], sel: &Selection) -> Option<String> {
    let (s, e) = sel.range()?;
    let mut out = String::new();
    let last = e.msg.min(msgs.len().saturating_sub(1));
    for (mi, m) in msgs.iter().enumerate().take(last + 1).skip(s.msg) {
        let blocks = &m.blocks;
        for (bi, b) in blocks.iter().enumerate() {
            if let Some(r) = sel.in_block(mi, bi, b.text.len()) {
                if !out.is_empty() {
                    out.push_str(if bi == 0 { "\n\n" } else { "\n" });
                }
                if let BlockKind::ListItem { marker, .. } = &b.kind {
                    if r.start == 0 {
                        out.push_str(marker);
                        out.push(' ');
                    }
                }
                out.push_str(&b.text[r]);
            }
        }
    }
    (!out.is_empty()).then_some(out)
}

pub fn word_at(text: &str, ix: usize) -> Range<usize> {
    for (i, w) in text.split_word_bound_indices() {
        if ix >= i && ix < i + w.len() {
            return i..i + w.len();
        }
    }
    ix..ix
}

/// Merge markdown styling with the selection highlight into sorted, non-overlapping ranges.
fn highlights(block: &Block, sel: Option<Range<usize>>, mono: bool) -> Vec<(Range<usize>, HighlightStyle)> {
    let style_of = |i: Inline| {
        let mut h = HighlightStyle::default();
        if i.bold {
            h.font_weight = Some(FontWeight::SEMIBOLD);
        }
        if i.italic {
            h.font_style = Some(FontStyle::Italic);
        }
        if i.code && !mono {
            h.background_color = Some(theme::inline_code_bg());
            h.color = Some(gpui::rgb(0xe6d9ff).into());
        }
        if i.link {
            h.color = Some(theme::accent().into());
            h.underline = Some(UnderlineStyle { thickness: px(1.), color: None, wavy: false });
        }
        if i.strike {
            h.strikethrough = Some(StrikethroughStyle { thickness: px(1.), color: None });
        }
        h
    };
    // cut points
    let mut cuts = vec![0, block.text.len()];
    for (r, _) in &block.spans {
        cuts.push(r.start);
        cuts.push(r.end);
    }
    if let Some(s) = &sel {
        cuts.push(s.start);
        cuts.push(s.end);
    }
    cuts.sort_unstable();
    cuts.dedup();
    let mut out = vec![];
    for w in cuts.windows(2) {
        let (a, b) = (w[0], w[1]);
        if a >= b || !block.text.is_char_boundary(a) || !block.text.is_char_boundary(b) {
            continue;
        }
        let mut h = block
            .spans
            .iter()
            .find(|(r, _)| r.start <= a && b <= r.end)
            .map(|(_, i)| style_of(*i))
            .unwrap_or_default();
        if sel.as_ref().map(|s| s.start <= a && b <= s.end).unwrap_or(false) {
            h.background_color = Some(theme::selection());
        }
        if h != HighlightStyle::default() {
            out.push((a..b, h));
        }
    }
    out
}

/// Callbacks into the owner of the selection (the workspace).
pub trait SelectionHost: gpui::Focusable + 'static {
    fn selection_down(&mut self, pos: Pos, click_count: usize, shift: bool, cx: &mut gpui::Context<Self>)
    where
        Self: Sized;
    fn selection_drag(&mut self, pos: Pos, cx: &mut gpui::Context<Self>)
    where
        Self: Sized;
}

/// One text block, selectable. Mouse handlers capture this frame's layout,
/// which has been painted whenever its hitbox can receive an event.
fn text_block<H: SelectionHost>(
    host: &Entity<H>,
    msg: usize,
    bi: usize,
    block: &Block,
    sel: Option<Range<usize>>,
    mono: bool,
) -> AnyElement {
    let text: SharedString = block.text.clone().into();
    let styled = StyledText::new(text.clone()).with_highlights(highlights(block, sel, mono));
    let layout = styled.layout().clone();
    let layout2 = layout.clone();
    let (h1, h2) = (host.clone(), host.clone());
    let len = block.text.len();
    div()
        .id(("blk", msg * 100_000 + bi))
        .cursor_text()
        .on_mouse_down(MouseButton::Left, move |e, w, cx: &mut App| {
            let f = h1.read(cx).focus_handle(cx);
            w.focus(&f);
            let ix = match layout.index_for_position(e.position) {
                Ok(i) | Err(i) => i.min(len),
            };
            h1.update(cx, |h, cx| h.selection_down(Pos { msg, block: bi, offset: ix }, e.click_count, e.modifiers.shift, cx));
        })
        .on_mouse_move(move |e, _w, cx: &mut App| {
            if e.pressed_button != Some(MouseButton::Left) {
                return;
            }
            let ix = match layout2.index_for_position(e.position) {
                Ok(i) | Err(i) => i.min(len),
            };
            h2.update(cx, |h, cx| h.selection_drag(Pos { msg, block: bi, offset: ix }, cx));
        })
        .child(styled)
        .into_any_element()
}

/// One row of the virtualised list. Long replies are split into one row per
/// block, so a 200 KB message costs only its visible blocks per frame.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RowKind {
    User,
    Block(usize),
    Pending,
    Meta,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Row {
    pub msg: usize,
    pub kind: RowKind,
}

pub fn rows_for(ix: usize, m: &Msg) -> Vec<Row> {
    match m.role {
        Role::User => vec![Row { msg: ix, kind: RowKind::User }],
        Role::Assistant => {
            let mut v: Vec<Row> = (0..m.blocks.len()).map(|b| Row { msg: ix, kind: RowKind::Block(b) }).collect();
            if v.is_empty() && m.streaming {
                v.push(Row { msg: ix, kind: RowKind::Pending });
            }
            v.push(Row { msg: ix, kind: RowKind::Meta });
            v
        }
    }
}

fn block_element<H: SelectionHost>(host: &Entity<H>, ix: usize, bi: usize, b: &Block, s: Option<Range<usize>>) -> AnyElement {
    match &b.kind {
        BlockKind::Paragraph => div().child(text_block(host, ix, bi, b, s, false)).into_any_element(),
        BlockKind::Heading(level) => div()
            .pt(px(4.))
            .text_size(px(match level { 1 => 20., 2 => 17., _ => 15. }))
            .font_weight(FontWeight::SEMIBOLD)
            .child(text_block(host, ix, bi, b, s, false))
            .into_any_element(),
        BlockKind::ListItem { marker, depth } => div()
            .flex()
            .pl(px(4. + 18. * *depth as f32))
            .gap(px(8.))
            .child(div().flex_shrink_0().min_w(px(14.)).text_color(theme::muted()).child(marker.clone()))
            .child(div().flex_1().min_w_0().child(text_block(host, ix, bi, b, s, false)))
            .into_any_element(),
        BlockKind::Code { lang } => div()
            .flex()
            .flex_col()
            .rounded(px(8.))
            .border_1()
            .border_color(theme::border())
            .bg(theme::code_bg())
            .child(
                div()
                    .px(px(12.))
                    .pt(px(6.))
                    .text_size(px(11.))
                    .text_color(theme::faint())
                    .child(if lang.is_empty() { "code".to_string() } else { lang.clone() }),
            )
            .child(
                div()
                    .px(px(12.))
                    .py(px(8.))
                    .font_family(theme::MONO)
                    .text_size(px(12.5))
                    .line_height(px(19.))
                    .child(text_block(host, ix, bi, b, s, true)),
            )
            .into_any_element(),
        BlockKind::Quote => div()
            .border_l_2()
            .border_color(theme::border())
            .pl(px(12.))
            .text_color(theme::muted())
            .child(text_block(host, ix, bi, b, s, false))
            .into_any_element(),
        BlockKind::Rule => div().h(px(1.)).bg(theme::border()).into_any_element(),
    }
}

pub fn render_row<H: SelectionHost>(host: &Entity<H>, row: Row, m: &Msg, sel: &Selection) -> AnyElement {
    let outer = div().w_full().flex().justify_center().px(px(32.));
    let inner = div().w_full().max_w(px(760.)).min_w_0();
    let ix = row.msg;
    match row.kind {
        RowKind::User => {
            let b = m.blocks.first().cloned().unwrap_or_else(|| Block { kind: BlockKind::Paragraph, text: String::new(), spans: vec![] });
            let s = sel.in_block(ix, 0, b.text.len());
            outer
                .pt(px(18.))
                .pb(px(8.))
                .child(
                    inner.flex().justify_end().child(
                        div()
                            .max_w(px(560.))
                            .px(px(14.))
                            .py(px(9.))
                            .rounded(px(12.))
                            .bg(theme::user_bubble())
                            .border_1()
                            .border_color(theme::border())
                            .child(text_block(host, ix, 0, &b, s, false)),
                    ),
                )
                .into_any_element()
        }
        RowKind::Block(bi) => {
            let Some(b) = m.blocks.get(bi) else { return div().into_any_element() };
            let s = sel.in_block(ix, bi, b.text.len());
            outer.pt(px(if bi == 0 { 8. } else { 10. })).child(inner.child(block_element(host, ix, bi, b, s))).into_any_element()
        }
        RowKind::Pending => outer.pt(px(8.)).child(inner.text_color(theme::faint()).child("…")).into_any_element(),
        RowKind::Meta => {
            let color = match m.meta_kind {
                MetaKind::Normal => theme::faint(),
                MetaKind::Warn => theme::warn(),
                MetaKind::Bad => theme::bad(),
            };
            outer
                .pt(px(8.))
                .pb(px(10.))
                .child(inner.text_size(px(12.)).line_height(px(18.)).text_color(color).child(m.meta.clone()))
                .into_any_element()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::markdown;

    fn msg(role: Role, text: &str) -> Msg {
        let blocks = if role == Role::User { markdown::plain(text) } else { markdown::parse(text) };
        Msg { role, text: text.into(), parsed_len: text.len(), blocks: Arc::new(blocks), meta: String::new(), meta_kind: MetaKind::Normal, streaming: false }
    }

    #[test]
    fn selection_spans_messages_and_blocks() {
        let msgs = vec![msg(Role::User, "hello there"), msg(Role::Assistant, "first para\n\n- item one\n- item two")];
        let sel = Selection { anchor: Some(Pos { msg: 1, block: 2, offset: 4 }), head: Some(Pos { msg: 0, block: 0, offset: 6 }), dragging: false };
        assert_eq!(sel.in_block(0, 0, 11), Some(6..11));
        assert_eq!(sel.in_block(1, 1, 8), Some(0..8));
        assert_eq!(sel.in_block(1, 2, 8), Some(0..4));
        assert_eq!(selected_text(&msgs, &sel).unwrap(), "there\n\nfirst para\n• item one\n• item");
    }

    #[test]
    fn highlights_never_overlap() {
        let b = &markdown::parse("a **bold** and `code` end")[0];
        let h = highlights(b, Some(3..12), false);
        for w in h.windows(2) {
            assert!(w[0].0.end <= w[1].0.start);
        }
        assert!(h.iter().any(|(r, s)| r.start == 3 && s.background_color.is_some() && s.font_weight.is_some()));
    }

    #[test]
    fn rows_split_long_replies() {
        let m = msg(Role::Assistant, "p1\n\np2\n\n```\ncode\n```");
        let rows = rows_for(4, &m);
        assert_eq!(rows.len(), 4); // three blocks + meta
        assert_eq!(rows[3].kind, RowKind::Meta);
    }
}
