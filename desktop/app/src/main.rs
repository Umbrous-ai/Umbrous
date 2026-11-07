//! Umbrous for macOS: a native GPUI interface over the bundled Python runtime.
//!
//! Rust does the desktop work: the window and everything drawn in it, menus,
//! keyboard shortcuts, clipboard, text input, and the runtime's lifecycle
//! (backend.rs). The agent, its API, chat history and projects stay in Python
//! (umbrous/), reached over the same protected loopback API the browser used.

mod actions;
mod api;
mod backend;
mod composer;
#[cfg(feature = "e2e")]
mod e2e;
mod logfile;
mod markdown;
mod theme;
mod transcript;
mod workspace;

use std::fs::OpenOptions;
use std::os::unix::io::AsRawFd;

use gpui::{
    point, px, size, App, AppContext, Application, Bounds, Focusable, Menu, MenuItem, OsAction, PromptLevel,
    SystemMenuType, TitlebarOptions, WindowBounds, WindowHandle, WindowOptions,
};

use actions::*;
use workspace::Workspace;

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// One app instance per data folder. A second copy (e.g. `open -n`) brings the
/// first to the front and exits, so there is never a second runtime. The lock
/// file holds the first copy's pid, so exactly that process is activated (never
/// another app that happens to share the bundle id).
fn single_instance() -> Option<std::fs::File> {
    use std::io::{Read, Seek, Write};
    let home = logfile::umbrous_home()?;
    let run = home.join("run");
    std::fs::create_dir_all(&run).ok()?;
    let mut f = OpenOptions::new().create(true).truncate(false).read(true).write(true).open(run.join("desktop.lock")).ok()?;
    let rc = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        let mut s = String::new();
        let _ = f.read_to_string(&mut s);
        logfile::event("app.second_launch", &[]);
        if let Ok(pid) = s.trim().parse::<i32>() {
            activate_pid(pid);
        }
        std::process::exit(0);
    }
    let _ = f.set_len(0);
    let _ = f.seek(std::io::SeekFrom::Start(0));
    let _ = write!(f, "{}", std::process::id());
    Some(f)
}

fn activate_pid(pid: i32) {
    use objc::runtime::Object;
    use objc::{class, msg_send, sel, sel_impl};
    unsafe {
        let app: *mut Object = msg_send![class!(NSRunningApplication), runningApplicationWithProcessIdentifier: pid];
        if !app.is_null() {
            // NSApplicationActivateAllWindows (1) | NSApplicationActivateIgnoringOtherApps (2)
            let _: bool = msg_send![app, activateWithOptions: 3usize];
            let _: bool = msg_send![app, unhide];
        }
    }
}

fn menus() -> Vec<Menu> {
    vec![
        Menu {
            name: "Umbrous".into(),
            items: vec![
                MenuItem::action("About Umbrous", About),
                MenuItem::separator(),
                MenuItem::os_submenu("Services", SystemMenuType::Services),
                MenuItem::separator(),
                MenuItem::action("Hide Umbrous", Hide),
                MenuItem::action("Hide Others", HideOthers),
                MenuItem::action("Show All", ShowAll),
                MenuItem::separator(),
                MenuItem::action("Quit Umbrous", Quit),
            ],
        },
        Menu {
            name: "File".into(),
            items: vec![MenuItem::action("New Chat", NewChat), MenuItem::separator(), MenuItem::action("Close Window", CloseWindow)],
        },
        Menu {
            name: "Edit".into(),
            items: vec![
                MenuItem::os_action("Undo", Undo, OsAction::Undo),
                MenuItem::os_action("Redo", Redo, OsAction::Redo),
                MenuItem::separator(),
                MenuItem::os_action("Cut", Cut, OsAction::Cut),
                MenuItem::os_action("Copy", Copy, OsAction::Copy),
                MenuItem::os_action("Paste", Paste, OsAction::Paste),
                MenuItem::os_action("Select All", SelectAll, OsAction::SelectAll),
                MenuItem::separator(),
                MenuItem::action("Emoji & Symbols", ShowCharacterPalette),
            ],
        },
        Menu {
            name: "View".into(),
            items: vec![
                MenuItem::action("Toggle Sidebar", ToggleSidebar),
                MenuItem::action("Toggle Details", ToggleDetails),
                MenuItem::separator(),
                MenuItem::action("Enter Full Screen", ToggleFullScreen),
            ],
        },
        Menu {
            name: "Chat".into(),
            items: vec![MenuItem::action("Stop Reply", StopReply), MenuItem::action("Check Ollama Again", ReloadModels)],
        },
        Menu {
            name: "Window".into(),
            items: vec![MenuItem::action("Minimize", Minimize), MenuItem::action("Zoom", Zoom)],
        },
        Menu { name: "Help".into(), items: vec![MenuItem::action("Show Umbrous Data Folder", ShowDataFolder)] },
    ]
}

fn open_main_window(started: Started, cx: &mut App) -> WindowHandle<Workspace> {
    let Started(backend, tx, rx) = started;
    let mut rx = Some(rx);
    let bounds = Bounds::centered(None, size(px(1440.), px(900.)), cx);
    let window = cx
        .open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                titlebar: Some(TitlebarOptions {
                    title: Some("Umbrous".into()),
                    appears_transparent: true,
                    traffic_light_position: Some(point(px(18.), px(18.))),
                }),
                window_min_size: Some(size(px(900.), px(600.))),
                app_id: Some("ai.umbrous.desktop".into()),
                ..Default::default()
            },
            |window, cx| {
                let ws = cx.new(|cx| Workspace::new(backend.clone(), tx.clone(), rx.take().unwrap(), window, cx));
                // Closing the window hides Umbrous (it keeps running, like other
                // single-window Mac apps); the Dock icon brings it back. Quit stops it.
                window.on_window_should_close(cx, |_, cx| {
                    cx.hide();
                    false
                });
                ws
            },
        )
        .expect("open window");
    window
        .update(cx, |ws, window, cx| {
            window.focus(&ws.composer.focus_handle(cx));
        })
        .ok();
    window
}

struct Started(backend::Backend, futures::channel::mpsc::UnboundedSender<backend::BackendEvent>,
               futures::channel::mpsc::UnboundedReceiver<backend::BackendEvent>);

fn main() {
    let _lock = single_instance();
    logfile::event("app.started", &[("version", VERSION.to_string())]);
    // Start Python first: it boots in parallel with GPUI's window and Metal setup.
    let backend = backend::Backend::new();
    let (tx, rx) = futures::channel::mpsc::unbounded();
    backend.start(tx.clone());
    let mut started = Some(Started(backend, tx, rx));
    let app = Application::new();
    // Dock click (or `open`) while hidden: show the window again.
    app.on_reopen(|cx| cx.activate(true));
    app.run(move |cx: &mut App| {
        actions::bind(cx);
        cx.set_menus(menus());
        let window = open_main_window(started.take().expect("run once"), cx);
        let backend = window.update(cx, |ws, _, _| ws.backend.clone()).expect("workspace");

        // Every way of quitting (Cmd-Q, the menu, the Dock, logout) ends here.
        cx.on_app_quit(move |_| {
            backend.shutdown(); // synchronous: GPUI only waits 100 ms for quit futures
            logfile::event("app.exited", &[]);
            async {}
        })
        .detach();

        cx.on_action(|_: &Quit, cx| cx.quit());
        cx.on_action(|_: &Hide, cx| cx.hide());
        cx.on_action(|_: &HideOthers, cx| cx.hide_other_apps());
        cx.on_action(|_: &ShowAll, cx| cx.unhide_other_apps());
        cx.on_action(|_: &CloseWindow, cx| cx.hide());
        cx.on_action(|_: &ShowDataFolder, cx| {
            if let Some(h) = logfile::umbrous_home() {
                cx.reveal_path(&h);
            }
        });
        cx.on_action(move |_: &Minimize, cx| {
            window.update(cx, |_, w, _| w.minimize_window()).ok();
        });
        cx.on_action(move |_: &Zoom, cx| {
            window.update(cx, |_, w, _| w.zoom_window()).ok();
        });
        cx.on_action(move |_: &ToggleFullScreen, cx| {
            window.update(cx, |_, w, _| w.toggle_fullscreen()).ok();
        });
        cx.on_action(move |_: &About, cx| {
            window
                .update(cx, |_, w, cx| {
                    let _answer = w.prompt(
                        PromptLevel::Info,
                        &format!("Umbrous {VERSION}"),
                        Some("A local-first agent harness. Native interface (GPUI) over the bundled Python runtime.\nMIT License."),
                        &["OK"],
                        cx,
                    );
                })
                .ok();
        });

        #[cfg(feature = "e2e")]
        e2e::start(window, cx);

        cx.activate(true);
    });
}
