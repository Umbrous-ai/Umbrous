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
    Pixels, SharedString, StrikethroughStyle, StyledText, TextRun, UnderlineStyle, Window,
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
            // `.answer strong`: weight 500, strong colour
            h.font_weight = Some(FontWeight::MEDIUM);
            h.color = Some(theme::text_strong().into());
        }
        if i.italic {
            h.font_style = Some(FontStyle::Italic);
        }
        if i.code && !mono {
            // `.chip`: soft accent background, strong text
            h.background_color = Some(theme::accent_soft());
            h.color = Some(theme::text_strong().into());
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
    use theme::*;
    match &b.kind {
        // `.answer`: 16px / 1.6, default colour
        BlockKind::Paragraph => div()
            .text_size(fs_16())
            .line_height(px(25.6))
            .text_color(text_default())
            .child(text_block(host, ix, bi, b, s, false))
            .into_any_element(),
        // `h3`: 16px, weight 500, strong
        BlockKind::Heading(level) => div()
            .text_size(if *level == 1 { fs_18() } else { fs_16() })
            .line_height(px(25.6))
            .font_weight(FontWeight::MEDIUM)
            .text_color(text_strong())
            .child(text_block(host, ix, bi, b, s, false))
            .into_any_element(),
        // `.hl`: 14px / 1.6, padding-left 20, subtle markers
        BlockKind::ListItem { marker, depth } => div()
            .flex()
            .pl(px(20. * *depth as f32))
            .text_size(fs_14())
            .line_height(px(22.4))
            .text_color(text_default())
            .child(div().flex_none().w(px(24.)).pl(px(if marker == "•" { 6. } else { 0. })).text_color(text_subtle()).child(marker.clone()))
            .child(div().flex_1().min_w_0().child(text_block(host, ix, bi, b, s, false)))
            .into_any_element(),
        // code: a raised card, mono
        BlockKind::Code { lang } => div()
            .flex()
            .flex_col()
            .rounded(px(R_8))
            .border_1()
            .border_color(border())
            .bg(bg_raised())
            .when(!lang.is_empty() && lang != "table", |d| {
                d.child(div().px(px(SP_12)).pt(px(SP_8)).text_size(fs_12()).text_color(text_subtle()).child(lang.clone()))
            })
            .child(
                div()
                    .px(px(SP_12))
                    .py(px(SP_8))
                    .font_family(MONO)
                    .text_size(fs_13())
                    .line_height(px(19.5))
                    .text_color(text_strong())
                    .child(text_block(host, ix, bi, b, s, true)),
            )
            .into_any_element(),
        BlockKind::Quote => div()
            .border_l_1()
            .border_color(border_strong())
            .pl(px(SP_12))
            .text_size(fs_16())
            .line_height(px(25.6))
            .text_color(text_subtle())
            .child(text_block(host, ix, bi, b, s, false))
            .into_any_element(),
        BlockKind::Rule => div().h(px(1.)).bg(border()).into_any_element(),
    }
}

/// Space above a block, from the page: paragraphs follow each other at 12,
/// headings get the `h3` margin, list items the `li` margin of 8.
fn block_gap(kind: &BlockKind, prev: Option<&BlockKind>) -> f32 {
    match (kind, prev) {
        (_, None) => 0.,
        (BlockKind::ListItem { .. }, Some(BlockKind::ListItem { .. })) => 8.,
        (BlockKind::Heading(_), _) => 24.,
        (_, Some(BlockKind::Heading(_))) => 12.,
        _ => 16.,
    }
}

/// The width of a message bubble's text if it never wrapped: its longest line.
/// The bubble is given this width (capped at 80% of the column) in block layout:
/// as a shrink-to-fit item of a flex row its text was measured too wide, so a long
/// message got a too-short bubble and its wrapped lines were painted over the reply.
fn unwrapped_width(text: &str, window: &Window) -> Pixels {
    let font = window.text_style().font();
    text.split('\n')
        .filter(|l| !l.is_empty())
        .map(|l| {
            let run = TextRun { len: l.len(), font: font.clone(), color: theme::text_strong().into(), background_color: None,
                                underline: None, strikethrough: None };
            window.text_system().shape_line(SharedString::from(l.to_string()), theme::fs_14(), &[run], None).width
        })
        .fold(px(0.), |a, b| if b > a { b } else { a })
}

pub fn render_row<H: SelectionHost>(host: &Entity<H>, row: Row, m: &Msg, sel: &Selection, first: bool, window: &Window,
                                    list_w: Pixels) -> AnyElement {
    use theme::*;
    // `.thread`: centred, 32 on the sides, 48 above the first message
    let outer = div().w_full().flex().justify_center().px(px(SP_32)).when(first, |d| d.pt(px(SP_48)));
    // `.thread { max-width: 760px; padding: 0 32px }`: a 696px text column
    let inner = div().w_full().max_w(px(696.)).min_w_0();
    let ix = row.msg;
    match row.kind {
        // `.msg-u`: right-aligned, raised, 1px border, radius 12, padding 10 16, 14px strong, 48 below
        RowKind::User => {
            let b = m.blocks.first().cloned().unwrap_or_else(|| Block { kind: BlockKind::Paragraph, text: String::new(), spans: vec![] });
            let s = sel.in_block(ix, 0, b.text.len());
            // Block layout all the way, centred and right-aligned by auto margins: as
            // flex items, the bubble's text was measured unwrapped and the row came out
            // too short, so the rows after it were drawn over the wrapped lines.
            div().w_full().px(px(SP_32)).pt(px(SP_48))
                .child(
                    inner.mx_auto().child(
                        div()
                            .ml_auto()
                            // padding 16+16, border 1+1, and a pixel of slack for rounding; at most
                            // 80% of the 696px column (narrower windows: of the list less its sides)
                            .w({
                                let col = if list_w > px(0.) { (list_w - px(2. * SP_32)).min(px(696.)) } else { px(696.) };
                                (unwrapped_width(&b.text, window) + px(35.)).min(col * 0.8)
                            })
                            .bg(bg_raised())
                            .border_1()
                            .border_color(border())
                            .rounded(px(R_12))
                            .px(px(SP_16))
                            .py(px(10.))
                            .text_size(fs_14())
                            .line_height(px(21.))
                            .text_color(text_strong())
                            .child(text_block(host, ix, 0, &b, s, false)),
                    ),
                )
                .into_any_element()
        }
        RowKind::Block(bi) => {
            let Some(b) = m.blocks.get(bi) else { return div().into_any_element() };
            let s = sel.in_block(ix, bi, b.text.len());
            let gap = if bi == 0 { SP_48 } else { block_gap(&b.kind, m.blocks.get(bi - 1).map(|p| &p.kind)) };
            outer.pt(px(gap)).child(inner.child(block_element(host, ix, bi, b, s))).into_any_element()
        }
        RowKind::Pending => outer.pt(px(SP_48)).child(inner).into_any_element(),
        // `.reply-meta`: 12px subtle, 8 above; `.reply` 16 below
        RowKind::Meta => {
            let color = match m.meta_kind {
                MetaKind::Normal => text_subtle(),
                MetaKind::Warn => warning(),
                MetaKind::Bad => danger(),
            };
            outer
                .pt(px(SP_8))
                .pb(px(SP_16))
                .child(inner.text_size(fs_12()).line_height(px(18.)).text_color(color).child(m.meta.clone()))
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
