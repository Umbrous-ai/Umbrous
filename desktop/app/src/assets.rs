//! Icons, compiled into the binary. Extracted unchanged from the SVG sprite at
//! the top of prototypes/umbrous-harness.html (1px outline icons on a 16px grid),
//! plus list-check, diff and connectors for the context card, drawn in the same style.

use std::borrow::Cow;

use gpui::{AssetSource, Result, SharedString};

const ICONS: &[(&str, &[u8])] = &[
    ("icons/arrow-up.svg", include_bytes!("../assets/icons/arrow-up.svg")),
    ("icons/asterisk.svg", include_bytes!("../assets/icons/asterisk.svg")),
    ("icons/bolt.svg", include_bytes!("../assets/icons/bolt.svg")),
    ("icons/shield.svg", include_bytes!("../assets/icons/shield.svg")),
    ("icons/shield-check.svg", include_bytes!("../assets/icons/shield-check.svg")),
    ("icons/shield-pen.svg", include_bytes!("../assets/icons/shield-pen.svg")),
    ("icons/book.svg", include_bytes!("../assets/icons/book.svg")),
    ("icons/bot.svg", include_bytes!("../assets/icons/bot.svg")),
    ("icons/branch.svg", include_bytes!("../assets/icons/branch.svg")),
    ("icons/chart.svg", include_bytes!("../assets/icons/chart.svg")),
    ("icons/check.svg", include_bytes!("../assets/icons/check.svg")),
    ("icons/chevron-down.svg", include_bytes!("../assets/icons/chevron-down.svg")),
    ("icons/chevrons.svg", include_bytes!("../assets/icons/chevrons.svg")),
    ("icons/clip.svg", include_bytes!("../assets/icons/clip.svg")),
    ("icons/clock.svg", include_bytes!("../assets/icons/clock.svg")),
    ("icons/code.svg", include_bytes!("../assets/icons/code.svg")),
    ("icons/commit.svg", include_bytes!("../assets/icons/commit.svg")),
    ("icons/connectors.svg", include_bytes!("../assets/icons/connectors.svg")),
    ("icons/copy.svg", include_bytes!("../assets/icons/copy.svg")),
    ("icons/cpu.svg", include_bytes!("../assets/icons/cpu.svg")),
    ("icons/diff.svg", include_bytes!("../assets/icons/diff.svg")),
    ("icons/doc.svg", include_bytes!("../assets/icons/doc.svg")),
    ("icons/folder.svg", include_bytes!("../assets/icons/folder.svg")),
    ("icons/globe.svg", include_bytes!("../assets/icons/globe.svg")),
    ("icons/grid.svg", include_bytes!("../assets/icons/grid.svg")),
    ("icons/home.svg", include_bytes!("../assets/icons/home.svg")),
    ("icons/inbox.svg", include_bytes!("../assets/icons/inbox.svg")),
    ("icons/layers.svg", include_bytes!("../assets/icons/layers.svg")),
    ("icons/list-check.svg", include_bytes!("../assets/icons/list-check.svg")),
    ("icons/logo.svg", include_bytes!("../assets/icons/logo.svg")),
    ("icons/moon.svg", include_bytes!("../assets/icons/moon.svg")),
    ("icons/more.svg", include_bytes!("../assets/icons/more.svg")),
    ("icons/pen.svg", include_bytes!("../assets/icons/pen.svg")),
    ("icons/plug.svg", include_bytes!("../assets/icons/plug.svg")),
    ("icons/plus.svg", include_bytes!("../assets/icons/plus.svg")),
    ("icons/plus-circle.svg", include_bytes!("../assets/icons/plus-circle.svg")),
    ("icons/rocket.svg", include_bytes!("../assets/icons/rocket.svg")),
    ("icons/search.svg", include_bytes!("../assets/icons/search.svg")),
    ("icons/sidebar.svg", include_bytes!("../assets/icons/sidebar.svg")),
    ("icons/sidebar-right.svg", include_bytes!("../assets/icons/sidebar-right.svg")),
    ("icons/sliders.svg", include_bytes!("../assets/icons/sliders.svg")),
    ("icons/sparkle.svg", include_bytes!("../assets/icons/sparkle.svg")),
    ("icons/stop.svg", include_bytes!("../assets/icons/stop.svg")),
    ("icons/table.svg", include_bytes!("../assets/icons/table.svg")),
    ("icons/terminal.svg", include_bytes!("../assets/icons/terminal.svg")),
    ("icons/wand.svg", include_bytes!("../assets/icons/wand.svg")),
    ("icons/x.svg", include_bytes!("../assets/icons/x.svg")),
];

pub struct Assets;

impl AssetSource for Assets {
    fn load(&self, path: &str) -> Result<Option<Cow<'static, [u8]>>> {
        Ok(ICONS.iter().find(|(p, _)| *p == path).map(|(_, b)| Cow::Borrowed(*b)))
    }

    fn list(&self, path: &str) -> Result<Vec<SharedString>> {
        Ok(ICONS.iter().filter(|(p, _)| p.starts_with(path)).map(|(p, _)| SharedString::from(*p)).collect())
    }
}
