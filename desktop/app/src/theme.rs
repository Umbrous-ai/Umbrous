//! Colours and sizes, taken from the prototype (prototypes/umbrous-harness.html).

use gpui::{rgb, rgba, Hsla, Rgba};

pub const FONT: &str = ".SystemUIFont";
pub const MONO: &str = "Menlo";

pub fn bg() -> Rgba { rgb(0x111113) }
pub fn panel() -> Rgba { rgb(0x18181b) }
pub fn panel_2() -> Rgba { rgb(0x1f1f23) }
pub fn border() -> Rgba { rgb(0x2a2a30) }
pub fn hover() -> Rgba { rgb(0x26262b) }
pub fn active() -> Rgba { rgb(0x2c2c33) }
pub fn text() -> Rgba { rgb(0xececf1) }
pub fn muted() -> Rgba { rgb(0x9b9ba6) }
pub fn faint() -> Rgba { rgb(0x6b6b75) }
pub fn accent() -> Rgba { rgb(0x8b7cf6) }
pub fn good() -> Rgba { rgb(0x4ade80) }
pub fn warn() -> Rgba { rgb(0xfbbf24) }
pub fn bad() -> Rgba { rgb(0xf87171) }
pub fn code_bg() -> Rgba { rgb(0x0c0c0e) }
pub fn user_bubble() -> Rgba { rgb(0x232329) }
pub fn selection() -> Hsla { rgba(0x8b7cf655).into() }
pub fn inline_code_bg() -> Hsla { rgba(0xffffff14).into() }
