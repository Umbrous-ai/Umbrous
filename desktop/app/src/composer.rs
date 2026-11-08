//! The message box: a multi-line native text field.
//!
//! GPUI has no text field of its own (Zed builds its editor on top of the same
//! primitives), so this one implements the platform text-input protocol
//! (`EntityInputHandler`): typing, IME composition (marked text, candidate
//! window position), the character palette, dictation, and the standard
//! macOS editing keys. Enter sends; Shift-Enter or Option-Enter adds a line.

use std::ops::Range;

use gpui::{
    canvas, div, fill, point, prelude::*, px, size, App, Bounds, ClipboardItem, Context, CursorStyle, ElementInputHandler,
    EntityInputHandler, EventEmitter, FocusHandle, Focusable, HighlightStyle, MouseButton, MouseDownEvent,
    MouseMoveEvent, MouseUpEvent, Pixels, Point, ScrollHandle, SharedString, StyledText, TextLayout, UTF16Selection,
    UnderlineStyle, Window,
};
use unicode_segmentation::UnicodeSegmentation;

use crate::actions::*;
use crate::theme;

pub enum ComposerEvent {
    Submit(String),
}

pub struct Composer {
    focus: FocusHandle,
    content: String,
    placeholder: SharedString,
    selected: Range<usize>,
    reversed: bool,
    marked: Option<Range<usize>>,
    layout: Option<TextLayout>,
    painted: bool,
    selecting: bool,
    disabled: bool,
    undo: Vec<(String, Range<usize>)>,
    redo: Vec<(String, Range<usize>)>,
    /// one-line field (the sidebar's project name box)
    compact: bool,
    /// remembered x position for up/down
    goal_x: Option<Pixels>,
    /// the message box grows up to MAX_HEIGHT and then scrolls
    scroll: ScrollHandle,
    /// bring the caret into view at the next paint (set by typing and caret moves,
    /// not by scrolling, so you can scroll away from the caret to read)
    reveal: bool,
}

/// Tallest the message box grows before it scrolls (about ten lines).
pub const MAX_HEIGHT: f32 = 220.;

impl EventEmitter<ComposerEvent> for Composer {}

impl Focusable for Composer {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus.clone()
    }
}

impl Composer {
    pub fn new(cx: &mut Context<Self>) -> Self {
        Composer {
            focus: cx.focus_handle(),
            content: String::new(),
            placeholder: "Ask Umbrous to do something…".into(),
            compact: false,
            selected: 0..0,
            reversed: false,
            marked: None,
            layout: None,
            painted: false,
            selecting: false,
            disabled: false,
            undo: vec![],
            redo: vec![],
            goal_x: None,
            scroll: ScrollHandle::new(),
            reveal: false,
        }
    }

    pub fn text(&self) -> &str {
        &self.content
    }

    pub fn set_placeholder(&mut self, p: impl Into<SharedString>, cx: &mut Context<Self>) {
        self.placeholder = p.into();
        cx.notify();
    }

    pub fn set_compact(&mut self, cx: &mut Context<Self>) {
        self.compact = true;
        cx.notify();
    }

    pub fn set_disabled(&mut self, disabled: bool, cx: &mut Context<Self>) {
        if self.disabled != disabled {
            self.disabled = disabled;
            cx.notify();
        }
    }

    pub fn set_text(&mut self, text: &str, cx: &mut Context<Self>) {
        self.content = text.to_string();
        self.selected = text.len()..text.len();
        self.marked = None;
        self.goal_x = None;
        self.reveal = true;
        cx.notify();
    }

    fn cursor(&self) -> usize {
        if self.reversed { self.selected.start } else { self.selected.end }
    }

    fn move_to(&mut self, ix: usize, cx: &mut Context<Self>) {
        self.selected = ix..ix;
        self.reversed = false;
        self.reveal = true;
        cx.notify();
    }

    fn select_to(&mut self, ix: usize, cx: &mut Context<Self>) {
        self.reveal = true;
        if self.reversed { self.selected.start = ix } else { self.selected.end = ix }
        if self.selected.end < self.selected.start {
            self.reversed = !self.reversed;
            self.selected = self.selected.end..self.selected.start;
        }
        cx.notify();
    }

    fn prev_boundary(&self, ix: usize) -> usize {
        self.content.grapheme_indices(true).rev().find_map(|(i, _)| (i < ix).then_some(i)).unwrap_or(0)
    }
    fn next_boundary(&self, ix: usize) -> usize {
        self.content.grapheme_indices(true).find_map(|(i, _)| (i > ix).then_some(i)).unwrap_or(self.content.len())
    }
    fn prev_word(&self, ix: usize) -> usize {
        self.content.unicode_word_indices().rev().find_map(|(i, _)| (i < ix).then_some(i)).unwrap_or(0)
    }
    fn next_word(&self, ix: usize) -> usize {
        self.content
            .unicode_word_indices()
            .find_map(|(i, w)| (i + w.len() > ix).then_some(i + w.len()))
            .unwrap_or(self.content.len())
    }
    fn line_start(&self, ix: usize) -> usize {
        self.content[..ix].rfind('\n').map(|i| i + 1).unwrap_or(0)
    }
    fn line_end(&self, ix: usize) -> usize {
        self.content[ix..].find('\n').map(|i| ix + i).unwrap_or(self.content.len())
    }

    /// Index one visual line up or down, keeping the horizontal position.
    fn vertical(&mut self, from: usize, down: bool) -> usize {
        let (Some(layout), true) = (self.layout.clone(), self.painted) else { return from };
        let Some(pos) = layout.position_for_index(from) else { return from };
        let x = *self.goal_x.get_or_insert(pos.x);
        let lh = layout.line_height();
        let target = point(x, if down { pos.y + lh * 1.5 } else { pos.y - lh * 0.5 });
        let b = layout.bounds();
        if target.y < b.top() {
            return 0;
        }
        if target.y > b.bottom() {
            return self.content.len();
        }
        match layout.index_for_position(target) {
            Ok(i) | Err(i) => i.min(self.content.len()),
        }
    }

    fn edit(&mut self, range: Range<usize>, text: &str, cx: &mut Context<Self>) {
        if self.disabled {
            return;
        }
        self.undo.push((self.content.clone(), self.selected.clone()));
        if self.undo.len() > 200 {
            self.undo.remove(0);
        }
        self.redo.clear();
        self.content.replace_range(range.clone(), text);
        let ix = range.start + text.len();
        self.selected = ix..ix;
        self.reversed = false;
        self.goal_x = None;
        self.reveal = true;
        cx.notify();
    }

    // ---- actions ----
    fn left(&mut self, _: &Left, _: &mut Window, cx: &mut Context<Self>) {
        let ix = if self.selected.is_empty() { self.prev_boundary(self.cursor()) } else { self.selected.start };
        self.goal_x = None;
        self.move_to(ix, cx)
    }
    fn right(&mut self, _: &Right, _: &mut Window, cx: &mut Context<Self>) {
        let ix = if self.selected.is_empty() { self.next_boundary(self.cursor()) } else { self.selected.end };
        self.goal_x = None;
        self.move_to(ix, cx)
    }
    fn up(&mut self, _: &Up, _: &mut Window, cx: &mut Context<Self>) {
        let ix = self.vertical(self.cursor(), false);
        let g = self.goal_x;
        self.move_to(ix, cx);
        self.goal_x = g;
    }
    fn down(&mut self, _: &Down, _: &mut Window, cx: &mut Context<Self>) {
        let ix = self.vertical(self.cursor(), true);
        let g = self.goal_x;
        self.move_to(ix, cx);
        self.goal_x = g;
    }
    fn select_left(&mut self, _: &SelectLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.prev_boundary(self.cursor()), cx)
    }
    fn select_right(&mut self, _: &SelectRight, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.next_boundary(self.cursor()), cx)
    }
    fn select_up(&mut self, _: &SelectUp, _: &mut Window, cx: &mut Context<Self>) {
        let ix = self.vertical(self.cursor(), false);
        self.select_to(ix, cx)
    }
    fn select_down(&mut self, _: &SelectDown, _: &mut Window, cx: &mut Context<Self>) {
        let ix = self.vertical(self.cursor(), true);
        self.select_to(ix, cx)
    }
    fn word_left(&mut self, _: &WordLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.prev_word(self.cursor()), cx)
    }
    fn word_right(&mut self, _: &WordRight, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.next_word(self.cursor()), cx)
    }
    fn select_word_left(&mut self, _: &SelectWordLeft, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.prev_word(self.cursor()), cx)
    }
    fn select_word_right(&mut self, _: &SelectWordRight, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.next_word(self.cursor()), cx)
    }
    fn line_start_a(&mut self, _: &LineStart, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.line_start(self.cursor()), cx)
    }
    fn line_end_a(&mut self, _: &LineEnd, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.line_end(self.cursor()), cx)
    }
    fn select_line_start(&mut self, _: &SelectLineStart, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.line_start(self.cursor()), cx)
    }
    fn select_line_end(&mut self, _: &SelectLineEnd, _: &mut Window, cx: &mut Context<Self>) {
        self.select_to(self.line_end(self.cursor()), cx)
    }
    fn doc_start(&mut self, _: &DocStart, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(0, cx)
    }
    fn doc_end(&mut self, _: &DocEnd, _: &mut Window, cx: &mut Context<Self>) {
        self.move_to(self.content.len(), cx)
    }
    fn select_all(&mut self, _: &SelectAll, _: &mut Window, cx: &mut Context<Self>) {
        self.selected = 0..self.content.len();
        self.reversed = false;
        cx.notify()
    }
    fn backspace(&mut self, _: &Backspace, _: &mut Window, cx: &mut Context<Self>) {
        let r = if self.selected.is_empty() { self.prev_boundary(self.cursor())..self.cursor() } else { self.selected.clone() };
        self.edit(r, "", cx)
    }
    fn delete(&mut self, _: &Delete, _: &mut Window, cx: &mut Context<Self>) {
        let r = if self.selected.is_empty() { self.cursor()..self.next_boundary(self.cursor()) } else { self.selected.clone() };
        self.edit(r, "", cx)
    }
    fn delete_word_left(&mut self, _: &DeleteWordLeft, _: &mut Window, cx: &mut Context<Self>) {
        let r = if self.selected.is_empty() { self.prev_word(self.cursor())..self.cursor() } else { self.selected.clone() };
        self.edit(r, "", cx)
    }
    fn delete_to_line_start(&mut self, _: &DeleteToLineStart, _: &mut Window, cx: &mut Context<Self>) {
        let c = self.cursor();
        self.edit(self.line_start(c)..c, "", cx)
    }
    fn newline(&mut self, _: &Newline, _: &mut Window, cx: &mut Context<Self>) {
        self.edit(self.selected.clone(), "\n", cx)
    }
    fn enter(&mut self, _: &Enter, _: &mut Window, cx: &mut Context<Self>) {
        if self.marked.is_some() {
            return; // Enter confirms IME composition; the input system handles it
        }
        if !self.content.trim().is_empty() && !self.disabled {
            cx.emit(ComposerEvent::Submit(self.content.clone()));
        }
    }
    fn copy(&mut self, _: &Copy, _: &mut Window, cx: &mut Context<Self>) {
        if self.selected.is_empty() {
            cx.propagate(); // let the transcript copy its selection
            return;
        }
        {
            cx.write_to_clipboard(ClipboardItem::new_string(self.content[self.selected.clone()].to_string()));
        }
    }
    fn cut(&mut self, _: &Cut, _: &mut Window, cx: &mut Context<Self>) {
        if !self.selected.is_empty() {
            cx.write_to_clipboard(ClipboardItem::new_string(self.content[self.selected.clone()].to_string()));
            self.edit(self.selected.clone(), "", cx)
        }
    }
    fn paste(&mut self, _: &Paste, _: &mut Window, cx: &mut Context<Self>) {
        if let Some(text) = cx.read_from_clipboard().and_then(|i| i.text()) {
            let text = text.replace("\r\n", "\n");
            self.edit(self.selected.clone(), &text, cx)
        }
    }
    fn undo_a(&mut self, _: &Undo, _: &mut Window, cx: &mut Context<Self>) {
        if let Some((text, sel)) = self.undo.pop() {
            self.redo.push((std::mem::replace(&mut self.content, text), self.selected.clone()));
            self.selected = sel;
            self.reveal = true;
            cx.notify();
        }
    }
    fn redo_a(&mut self, _: &Redo, _: &mut Window, cx: &mut Context<Self>) {
        if let Some((text, sel)) = self.redo.pop() {
            self.undo.push((std::mem::replace(&mut self.content, text), self.selected.clone()));
            self.selected = sel;
            self.reveal = true;
            cx.notify();
        }
    }
    fn character_palette(&mut self, _: &ShowCharacterPalette, window: &mut Window, _: &mut Context<Self>) {
        window.show_character_palette();
    }

    fn index_at(&self, p: Point<Pixels>) -> usize {
        match (&self.layout, self.painted, self.content.is_empty()) {
            (Some(l), true, false) => match l.index_for_position(p) {
                Ok(i) | Err(i) => i.min(self.content.len()),
            },
            _ => 0,
        }
    }
    fn on_mouse_down(&mut self, e: &MouseDownEvent, window: &mut Window, cx: &mut Context<Self>) {
        window.focus(&self.focus);
        let ix = self.index_at(e.position);
        self.goal_x = None;
        if e.click_count == 2 {
            self.selected = crate::transcript::word_at(&self.content, ix.min(self.content.len()));
            self.reversed = false;
            cx.notify();
        } else if e.click_count >= 3 {
            self.selected = self.line_start(ix)..self.line_end(ix);
            cx.notify();
        } else if e.modifiers.shift {
            self.select_to(ix, cx)
        } else {
            self.selecting = true;
            self.move_to(ix, cx)
        }
    }
    fn on_mouse_move(&mut self, e: &MouseMoveEvent, _: &mut Window, cx: &mut Context<Self>) {
        if self.selecting {
            let ix = self.index_at(e.position);
            self.select_to(ix, cx)
        }
    }
    fn on_mouse_up(&mut self, _: &MouseUpEvent, _: &mut Window, _: &mut Context<Self>) {
        self.selecting = false;
    }

    // UTF-16 <-> UTF-8 (the platform protocol counts UTF-16 units)
    fn utf8_to_utf16(&self, ix: usize) -> usize {
        self.content[..ix.min(self.content.len())].encode_utf16().count()
    }
    fn utf16_to_utf8(&self, ix: usize) -> usize {
        let mut n = 0;
        for (i, ch) in self.content.char_indices() {
            if n >= ix {
                return i;
            }
            n += ch.len_utf16();
        }
        self.content.len()
    }
    fn range_to_utf16(&self, r: &Range<usize>) -> Range<usize> {
        self.utf8_to_utf16(r.start)..self.utf8_to_utf16(r.end)
    }
    fn range_from_utf16(&self, r: &Range<usize>) -> Range<usize> {
        self.utf16_to_utf8(r.start)..self.utf16_to_utf8(r.end)
    }
}

impl EntityInputHandler for Composer {
    fn text_for_range(&mut self, r: Range<usize>, actual: &mut Option<Range<usize>>, _: &mut Window, _: &mut Context<Self>) -> Option<String> {
        let r = self.range_from_utf16(&r);
        actual.replace(self.range_to_utf16(&r));
        Some(self.content[r].to_string())
    }
    fn selected_text_range(&mut self, _: bool, _: &mut Window, _: &mut Context<Self>) -> Option<UTF16Selection> {
        Some(UTF16Selection { range: self.range_to_utf16(&self.selected), reversed: self.reversed })
    }
    fn marked_text_range(&self, _: &mut Window, _: &mut Context<Self>) -> Option<Range<usize>> {
        self.marked.as_ref().map(|r| self.range_to_utf16(r))
    }
    fn unmark_text(&mut self, _: &mut Window, _: &mut Context<Self>) {
        self.marked = None;
    }
    fn replace_text_in_range(&mut self, r: Option<Range<usize>>, text: &str, _: &mut Window, cx: &mut Context<Self>) {
        let r = r.map(|r| self.range_from_utf16(&r)).or(self.marked.clone()).unwrap_or(self.selected.clone());
        self.marked = None;
        self.edit(r, text, cx);
    }
    fn replace_and_mark_text_in_range(&mut self, r: Option<Range<usize>>, text: &str, new_sel: Option<Range<usize>>, _: &mut Window, cx: &mut Context<Self>) {
        if self.disabled {
            return;
        }
        let r = r.map(|r| self.range_from_utf16(&r)).or(self.marked.clone()).unwrap_or(self.selected.clone());
        self.content.replace_range(r.clone(), text);
        self.marked = (!text.is_empty()).then(|| r.start..r.start + text.len());
        self.selected = new_sel
            .map(|s| {
                // new_sel is relative to the inserted text, in UTF-16 units
                let inserted = &self.content[r.start..r.start + text.len()];
                let conv = |u: usize| {
                    let mut n = 0;
                    for (i, ch) in inserted.char_indices() {
                        if n >= u {
                            return i;
                        }
                        n += ch.len_utf16();
                    }
                    inserted.len()
                };
                r.start + conv(s.start)..r.start + conv(s.end)
            })
            .unwrap_or(r.start + text.len()..r.start + text.len());
        self.reveal = true;
        cx.notify();
    }
    fn bounds_for_range(&mut self, r: Range<usize>, element_bounds: Bounds<Pixels>, _: &mut Window, _: &mut Context<Self>) -> Option<Bounds<Pixels>> {
        let layout = self.layout.as_ref().filter(|_| self.painted)?;
        let r = self.range_from_utf16(&r);
        let start = layout.position_for_index(r.start).unwrap_or(element_bounds.origin);
        Some(Bounds::new(start, size(px(2.), layout.line_height())))
    }
    fn character_index_for_point(&mut self, p: Point<Pixels>, _: &mut Window, _: &mut Context<Self>) -> Option<usize> {
        let ix = self.index_at(p);
        Some(self.utf8_to_utf16(ix))
    }
}

impl Render for Composer {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let empty = self.content.is_empty();
        let placeholder: SharedString = if self.disabled && !self.compact {
            "Archived chats are read-only. Restore it to continue.".into() // chat.js setComposer()
        } else {
            self.placeholder.clone()
        };
        let text: SharedString = if empty { placeholder } else { self.content.clone().into() };
        let mut highlights: Vec<(Range<usize>, HighlightStyle)> = vec![];
        if empty {
            highlights.push((0..text.len(), HighlightStyle { color: Some(theme::text_subtle().into()), ..Default::default() }));
        } else {
            // selection and marked (IME) text; they never overlap in practice, so order them
            if !self.selected.is_empty() {
                highlights.push((self.selected.clone(), HighlightStyle { background_color: Some(theme::selection()), ..Default::default() }));
            }
            if let Some(m) = self.marked.clone() {
                highlights.retain(|(r, _)| r.end <= m.start || r.start >= m.end);
                highlights.push((m, HighlightStyle {
                    underline: Some(UnderlineStyle { thickness: px(1.), color: Some(theme::text_strong().into()), wavy: false }),
                    ..Default::default()
                }));
                highlights.sort_by_key(|(r, _)| r.start);
            }
        }
        let styled = StyledText::new(text).with_highlights(highlights);
        self.layout = Some(styled.layout().clone());
        self.painted = false;
        let entity = cx.entity();
        let focus = self.focus.clone();
        let focused = focus.is_focused(window);
        let cursor = if empty { 0 } else { self.cursor() };
        let show_caret = focused && self.selected.is_empty() && !self.disabled;
        let layout = styled.layout().clone();

        let reveal = std::mem::take(&mut self.reveal) && !empty;
        let scroll = self.scroll.clone();
        let compact = self.compact;

        // Block layout all the way down, with a definite width: inside a flex column the
        // text was measured as one unwrapped line, so a long paragraph got a one-line
        // box and its wrapped lines were painted over the toolbar and out of the window.
        div()
            .id("composer-scroll")
            .key_context("Composer")
            .track_focus(&self.focus)
            .cursor(CursorStyle::IBeam)
            .on_action(cx.listener(Self::left))
            .on_action(cx.listener(Self::right))
            .on_action(cx.listener(Self::up))
            .on_action(cx.listener(Self::down))
            .on_action(cx.listener(Self::select_left))
            .on_action(cx.listener(Self::select_right))
            .on_action(cx.listener(Self::select_up))
            .on_action(cx.listener(Self::select_down))
            .on_action(cx.listener(Self::word_left))
            .on_action(cx.listener(Self::word_right))
            .on_action(cx.listener(Self::select_word_left))
            .on_action(cx.listener(Self::select_word_right))
            .on_action(cx.listener(Self::line_start_a))
            .on_action(cx.listener(Self::line_end_a))
            .on_action(cx.listener(Self::select_line_start))
            .on_action(cx.listener(Self::select_line_end))
            .on_action(cx.listener(Self::doc_start))
            .on_action(cx.listener(Self::doc_end))
            .on_action(cx.listener(Self::select_all))
            .on_action(cx.listener(Self::backspace))
            .on_action(cx.listener(Self::delete))
            .on_action(cx.listener(Self::delete_word_left))
            .on_action(cx.listener(Self::delete_to_line_start))
            .on_action(cx.listener(Self::newline))
            .on_action(cx.listener(Self::enter))
            .on_action(cx.listener(Self::copy))
            .on_action(cx.listener(Self::cut))
            .on_action(cx.listener(Self::paste))
            .on_action(cx.listener(Self::undo_a))
            .on_action(cx.listener(Self::redo_a))
            .on_action(cx.listener(Self::character_palette))
            .on_mouse_down(MouseButton::Left, cx.listener(Self::on_mouse_down))
            .on_mouse_move(cx.listener(Self::on_mouse_move))
            .on_mouse_up(MouseButton::Left, cx.listener(Self::on_mouse_up))
            .on_mouse_up_out(MouseButton::Left, cx.listener(Self::on_mouse_up))
            .w_full()
            .min_h(px(if compact { 19.5 } else { 21. }))
            .max_h(px(if compact { 240. } else { MAX_HEIGHT }))
            .overflow_y_scroll()
            .track_scroll(&self.scroll)
            .text_size(px(if compact { 13. } else { 14. }))
            .line_height(px(if compact { 19.5 } else { 21. }))
            .text_color(if self.disabled { theme::text_subtle() } else { theme::text_strong() })
            .child(
                // the text and, over it, the caret and input handler; both scroll together
                div()
                    .w_full()
                    .relative()
                    .child(styled)
                    .child(
                        canvas(
                            move |_, _, _| {},
                            move |bounds, _, window, cx| {
                                window.handle_input(&focus, ElementInputHandler::new(bounds, entity.clone()), cx);
                                entity.update(cx, |c, _| c.painted = true);
                                let caret = layout.position_for_index(cursor);
                                if show_caret {
                                    if let Some(p) = caret {
                                        window.paint_quad(fill(Bounds::new(p, size(px(1.5), layout.line_height())), theme::accent()));
                                    }
                                }
                                if let (true, Some(p)) = (reveal, caret) {
                                    // keep the caret's line inside the visible part of the box
                                    let view = scroll.bounds();
                                    let lh = layout.line_height();
                                    let mut off = scroll.offset();
                                    if p.y < view.top() {
                                        off.y += view.top() - p.y;
                                    } else if p.y + lh > view.bottom() {
                                        off.y -= p.y + lh - view.bottom();
                                    }
                                    if off != scroll.offset() {
                                        scroll.set_offset(off);
                                        window.refresh();
                                    }
                                }
                            },
                        )
                        .absolute()
                        .top_0()
                        .left_0()
                        .size_full(),
                    ),
            )
    }
}
