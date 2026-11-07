//! Keyboard actions and their default bindings (standard macOS keys).

use gpui::{actions, App, KeyBinding};

actions!(
    umbrous,
    [
        // app
        Quit, Hide, HideOthers, ShowAll, About, NewChat, CloseWindow, Minimize, Zoom, ToggleFullScreen,
        ToggleSidebar, ToggleDetails, StopReply, ShowDataFolder, ReloadModels,
        // editing (composer and transcript)
        Copy, Cut, Paste, SelectAll, Undo, Redo,
        Backspace, Delete, DeleteWordLeft, DeleteToLineStart,
        Left, Right, Up, Down, SelectLeft, SelectRight, SelectUp, SelectDown,
        WordLeft, WordRight, SelectWordLeft, SelectWordRight,
        LineStart, LineEnd, SelectLineStart, SelectLineEnd, DocStart, DocEnd,
        Enter, Newline, Escape, ShowCharacterPalette,
    ]
);

pub fn bind(cx: &mut App) {
    const INPUT: Option<&str> = Some("Composer");
    cx.bind_keys([
        KeyBinding::new("cmd-q", Quit, None),
        KeyBinding::new("cmd-h", Hide, None),
        KeyBinding::new("alt-cmd-h", HideOthers, None),
        KeyBinding::new("cmd-n", NewChat, None),
        KeyBinding::new("cmd-w", CloseWindow, None),
        KeyBinding::new("cmd-m", Minimize, None),
        KeyBinding::new("ctrl-cmd-f", ToggleFullScreen, None),
        KeyBinding::new("cmd-\\", ToggleSidebar, None),
        KeyBinding::new("alt-cmd-\\", ToggleDetails, None),
        KeyBinding::new("cmd-.", StopReply, None),
        KeyBinding::new("cmd-c", Copy, None),
        KeyBinding::new("cmd-x", Cut, None),
        KeyBinding::new("cmd-v", Paste, None),
        KeyBinding::new("cmd-a", SelectAll, None),
        KeyBinding::new("cmd-z", Undo, None),
        KeyBinding::new("cmd-shift-z", Redo, None),
        KeyBinding::new("escape", Escape, None),
        KeyBinding::new("backspace", Backspace, INPUT),
        KeyBinding::new("delete", Delete, INPUT),
        KeyBinding::new("alt-backspace", DeleteWordLeft, INPUT),
        KeyBinding::new("cmd-backspace", DeleteToLineStart, INPUT),
        KeyBinding::new("left", Left, INPUT),
        KeyBinding::new("right", Right, INPUT),
        KeyBinding::new("up", Up, INPUT),
        KeyBinding::new("down", Down, INPUT),
        KeyBinding::new("shift-left", SelectLeft, INPUT),
        KeyBinding::new("shift-right", SelectRight, INPUT),
        KeyBinding::new("shift-up", SelectUp, INPUT),
        KeyBinding::new("shift-down", SelectDown, INPUT),
        KeyBinding::new("alt-left", WordLeft, INPUT),
        KeyBinding::new("alt-right", WordRight, INPUT),
        KeyBinding::new("alt-shift-left", SelectWordLeft, INPUT),
        KeyBinding::new("alt-shift-right", SelectWordRight, INPUT),
        KeyBinding::new("cmd-left", LineStart, INPUT),
        KeyBinding::new("cmd-right", LineEnd, INPUT),
        KeyBinding::new("home", LineStart, INPUT),
        KeyBinding::new("end", LineEnd, INPUT),
        KeyBinding::new("cmd-shift-left", SelectLineStart, INPUT),
        KeyBinding::new("cmd-shift-right", SelectLineEnd, INPUT),
        KeyBinding::new("cmd-up", DocStart, INPUT),
        KeyBinding::new("cmd-down", DocEnd, INPUT),
        KeyBinding::new("enter", Enter, INPUT),
        KeyBinding::new("shift-enter", Newline, INPUT),
        KeyBinding::new("alt-enter", Newline, INPUT),
        KeyBinding::new("ctrl-cmd-space", ShowCharacterPalette, INPUT),
    ]);
}
