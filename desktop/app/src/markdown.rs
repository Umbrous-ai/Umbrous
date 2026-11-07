//! Markdown → a flat list of styled text blocks.
//!
//! Runs on a background thread (never on the UI thread). Model output is
//! untrusted: raw HTML is shown as literal text, links are styled but not
//! clickable, images show their alt text. Nothing here can make the app load
//! anything.

use std::ops::Range;

use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Inline {
    pub bold: bool,
    pub italic: bool,
    pub code: bool,
    pub link: bool,
    pub strike: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum BlockKind {
    Paragraph,
    Heading(u8),
    /// `marker` is "•" or "3." ; `depth` starts at 0
    ListItem { marker: String, depth: usize },
    Code { lang: String },
    Quote,
    Rule,
}

#[derive(Clone, Debug)]
pub struct Block {
    pub kind: BlockKind,
    pub text: String,
    /// Consecutive, non-overlapping styled ranges (plain text has none).
    pub spans: Vec<(Range<usize>, Inline)>,
}

impl Block {
    fn new(kind: BlockKind) -> Self {
        Block { kind, text: String::new(), spans: vec![] }
    }
    fn push(&mut self, s: &str, style: Inline) {
        if s.is_empty() {
            return;
        }
        let start = self.text.len();
        self.text.push_str(s);
        if style != Inline::default() {
            if let Some((r, st)) = self.spans.last_mut() {
                if *st == style && r.end == start {
                    r.end = self.text.len();
                    return;
                }
            }
            self.spans.push((start..self.text.len(), style));
        }
    }
}

/// Plain text (user messages): one block per paragraph, no markup.
pub fn plain(text: &str) -> Vec<Block> {
    vec![Block { kind: BlockKind::Paragraph, text: text.trim_end().to_string(), spans: vec![] }]
}

pub fn parse(src: &str) -> Vec<Block> {
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    opts.insert(Options::ENABLE_TABLES);
    let mut blocks: Vec<Block> = vec![];
    let mut cur: Option<Block> = None;
    let mut style = Inline::default();
    let mut lists: Vec<Option<u64>> = vec![]; // next number for ordered lists
    let mut quote = 0usize;
    let mut in_code = false;

    fn flush(blocks: &mut Vec<Block>, cur: &mut Option<Block>) {
        if let Some(mut b) = cur.take() {
            if matches!(b.kind, BlockKind::Code { .. }) {
                while b.text.ends_with('\n') {
                    b.text.pop();
                }
            } else {
                let trimmed = b.text.trim_end().len();
                b.text.truncate(trimmed);
                for (r, _) in b.spans.iter_mut() {
                    r.end = r.end.min(trimmed);
                    r.start = r.start.min(r.end);
                }
                b.spans.retain(|(r, _)| !r.is_empty());
            }
            if !b.text.is_empty() || matches!(b.kind, BlockKind::Rule) {
                blocks.push(b);
            }
        }
    }
    let para_kind = |quote: usize| if quote > 0 { BlockKind::Quote } else { BlockKind::Paragraph };

    for ev in Parser::new_ext(src, opts) {
        match ev {
            Event::Start(tag) => match tag {
                Tag::Paragraph => {
                    // inside a list item that already has text, a new paragraph is its own block
                    if cur.as_ref().map(|b| !b.text.is_empty()).unwrap_or(true) {
                        flush(&mut blocks, &mut cur);
                        cur = Some(Block::new(para_kind(quote)));
                    }
                }
                Tag::Heading { level, .. } => {
                    flush(&mut blocks, &mut cur);
                    let n = match level {
                        HeadingLevel::H1 => 1,
                        HeadingLevel::H2 => 2,
                        HeadingLevel::H3 => 3,
                        _ => 4,
                    };
                    cur = Some(Block::new(BlockKind::Heading(n)));
                }
                Tag::List(start) => {
                    flush(&mut blocks, &mut cur);
                    lists.push(start);
                }
                Tag::Item => {
                    flush(&mut blocks, &mut cur);
                    let depth = lists.len().saturating_sub(1);
                    let marker = match lists.last_mut() {
                        Some(Some(n)) => {
                            let m = format!("{n}.");
                            *n += 1;
                            m
                        }
                        _ => "•".to_string(),
                    };
                    cur = Some(Block::new(BlockKind::ListItem { marker, depth }));
                }
                Tag::CodeBlock(kind) => {
                    flush(&mut blocks, &mut cur);
                    let lang = match kind {
                        CodeBlockKind::Fenced(l) => l.split_whitespace().next().unwrap_or("").to_string(),
                        CodeBlockKind::Indented => String::new(),
                    };
                    in_code = true;
                    cur = Some(Block::new(BlockKind::Code { lang }));
                }
                Tag::BlockQuote(_) => {
                    flush(&mut blocks, &mut cur);
                    quote += 1;
                }
                Tag::Emphasis => style.italic = true,
                Tag::Strong => style.bold = true,
                Tag::Strikethrough => style.strike = true,
                Tag::Link { .. } => style.link = true,
                Tag::TableCell => {
                    if let Some(b) = cur.as_mut() {
                        if !b.text.is_empty() && !b.text.ends_with('\n') {
                            b.push(" │ ", Inline::default());
                        }
                    }
                }
                Tag::Table(_) => {
                    flush(&mut blocks, &mut cur);
                    in_code = true; // tables are shown monospaced, cell by cell
                    cur = Some(Block::new(BlockKind::Code { lang: "table".into() }));
                }
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Paragraph | TagEnd::Heading(_) => {
                    if !matches!(cur.as_ref().map(|b| &b.kind), Some(BlockKind::ListItem { .. })) {
                        flush(&mut blocks, &mut cur);
                    }
                }
                TagEnd::Item => flush(&mut blocks, &mut cur),
                TagEnd::List(_) => {
                    flush(&mut blocks, &mut cur);
                    lists.pop();
                }
                TagEnd::CodeBlock | TagEnd::Table => {
                    in_code = false;
                    flush(&mut blocks, &mut cur);
                }
                TagEnd::TableHead | TagEnd::TableRow => {
                    if let Some(b) = cur.as_mut() {
                        b.push("\n", Inline::default());
                    }
                }
                TagEnd::BlockQuote(_) => {
                    flush(&mut blocks, &mut cur);
                    quote = quote.saturating_sub(1);
                }
                TagEnd::Emphasis => style.italic = false,
                TagEnd::Strong => style.bold = false,
                TagEnd::Strikethrough => style.strike = false,
                TagEnd::Link => style.link = false,
                _ => {}
            },
            Event::Text(t) => {
                let b = cur.get_or_insert_with(|| Block::new(para_kind(quote)));
                b.push(&t, if in_code { Inline::default() } else { style });
            }
            Event::Code(t) => {
                let b = cur.get_or_insert_with(|| Block::new(para_kind(quote)));
                b.push(&t, Inline { code: true, ..style });
            }
            Event::Html(t) | Event::InlineHtml(t) => {
                let b = cur.get_or_insert_with(|| Block::new(para_kind(quote)));
                b.push(&t, style); // shown literally, never interpreted
            }
            Event::SoftBreak => {
                if let Some(b) = cur.as_mut() {
                    b.push(" ", style);
                }
            }
            Event::HardBreak => {
                if let Some(b) = cur.as_mut() {
                    b.push("\n", style);
                }
            }
            Event::Rule => {
                flush(&mut blocks, &mut cur);
                blocks.push(Block::new(BlockKind::Rule));
            }
            Event::TaskListMarker(done) => {
                let b = cur.get_or_insert_with(|| Block::new(para_kind(quote)));
                b.push(if done { "☑ " } else { "☐ " }, Inline::default());
            }
            _ => {}
        }
    }
    flush(&mut blocks, &mut cur);
    if blocks.is_empty() && !src.trim().is_empty() {
        return plain(src);
    }
    blocks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_blocks() {
        let b = parse("# Title\n\nSome **bold** and `code`.\n\n- one\n- two\n\n```rust\nfn x() {}\n```\n");
        assert_eq!(b[0].kind, BlockKind::Heading(1));
        assert_eq!(b[1].text, "Some bold and code.");
        assert_eq!(b[1].spans.len(), 2);
        assert!(matches!(b[2].kind, BlockKind::ListItem { .. }));
        assert_eq!(b[4].kind, BlockKind::Code { lang: "rust".into() });
        assert_eq!(b[4].text, "fn x() {}");
    }

    #[test]
    fn html_is_literal() {
        let b = parse("<script>alert(1)</script> hi <b>x</b>");
        let all: String = b.iter().map(|b| b.text.clone()).collect();
        assert!(all.contains("<script>"));
    }

    #[test]
    fn unfinished_stream_is_fine() {
        let b = parse("Here:\n\n```py\nprint(1)\n");
        assert_eq!(b.last().unwrap().text, "print(1)");
    }
}
