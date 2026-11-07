//! Design tokens, copied from the original interface's `:root`
//! (prototypes/umbrous-harness.html, see prototypes/NOTES.md). Keep the
//! names and values in step with that file: it is the visual source of truth.

use gpui::{px, rgb, rgba, Hsla, Pixels, Rgba};

// Type: SF Pro, weights 400 and 500 only
pub const FONT: &str = ".SystemUIFont"; // resolves to SF Pro on macOS
pub const MONO: &str = "SF Mono";
pub fn fs_12() -> Pixels { px(12.) }
pub fn fs_13() -> Pixels { px(13.) }
pub fn fs_14() -> Pixels { px(14.) }
pub fn fs_16() -> Pixels { px(16.) }
pub fn fs_18() -> Pixels { px(18.) }

// Dark theme
pub fn bg_canvas() -> Rgba { rgb(0x141414) } // behind floating panels
pub fn bg_app() -> Rgba { rgb(0x1B1B1B) } // panels
pub fn bg_raised() -> Rgba { rgb(0x222222) } // cards, code blocks, composer
pub fn bg_hover() -> Rgba { rgb(0x232323) }
pub fn bg_selected() -> Rgba { rgb(0x292929) }
pub fn border() -> Rgba { rgb(0x2A2A2A) }
pub fn border_strong() -> Rgba { rgb(0x353535) }
pub fn text_subtle() -> Rgba { rgb(0x7F7F7F) }
pub fn text_default() -> Rgba { rgb(0xB5B5B5) }
pub fn text_strong() -> Rgba { rgb(0xF2F2F2) }

pub fn accent() -> Rgba { rgb(0x8B7CF6) }
pub fn accent_soft() -> Hsla { rgba(0x8B7CF624).into() } // rgba(139,124,246,.14)
pub fn danger() -> Rgba { rgb(0xF26D6D) }
pub fn success() -> Rgba { rgb(0x5CCB8A) }
pub fn warning() -> Rgba { rgb(0xF2C14E) }

// Text selection: the accent at low opacity (the browser used its own highlight)
pub fn selection() -> Hsla { rgba(0x8B7CF655).into() }

// Radius and spacing scales
pub const R_4: f32 = 4.;
pub const R_6: f32 = 6.;
pub const R_8: f32 = 8.;
pub const R_12: f32 = 12.;
pub const SP_4: f32 = 4.;
pub const SP_8: f32 = 8.;
pub const SP_12: f32 = 12.;
pub const SP_16: f32 = 16.;
pub const SP_24: f32 = 24.;
pub const SP_32: f32 = 32.;
pub const SP_48: f32 = 48.;
