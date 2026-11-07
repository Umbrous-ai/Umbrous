//! Test-only driver (compiled only with `--features e2e`, never in a release
//! bundle). Reads commands from UMBROUS_E2E_SCRIPT, one per line, drives the
//! real window against the real bundled runtime, and appends one JSON object
//! per `record` to UMBROUS_E2E_OUT. See desktop/e2e/.

use std::io::Write;
use std::time::{Duration, Instant};

use gpui::{px, App, AsyncApp, ListOffset, WindowHandle};
use serde_json::{json, Value};

use crate::workspace::{Phase, Workspace};

pub fn start(window: WindowHandle<Workspace>, cx: &mut App) {
    let (Ok(script), Ok(out)) = (std::env::var("UMBROUS_E2E_SCRIPT"), std::env::var("UMBROUS_E2E_OUT")) else { return };
    let Ok(text) = std::fs::read_to_string(script) else { return };
    let _ = std::fs::remove_file(&out);
    cx.spawn(async move |cx| {
        for line in text.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
            let (cmd, arg) = line.split_once(' ').map(|(a, b)| (a, b.trim())).unwrap_or((line, ""));
            let res = run(cmd, arg, window, cx).await;
            let rec = match res {
                Ok(Some(v)) => Some(v),
                Ok(None) => None,
                Err(e) => Some(json!({"step": line, "error": e})),
            };
            if let Some(v) = rec {
                if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&out) {
                    let _ = writeln!(f, "{v}");
                }
                if v.get("error").is_some() {
                    break;
                }
            }
            if cmd == "quit" {
                break;
            }
        }
    })
    .detach();
}

async fn sleep(cx: &mut AsyncApp, ms: u64) {
    cx.background_executor().timer(Duration::from_millis(ms)).await;
}

async fn wait_until(cx: &mut AsyncApp, window: WindowHandle<Workspace>, secs: u64, what: &str,
                    f: impl Fn(&Workspace) -> bool) -> Result<(), String> {
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        if window.update(cx, |ws, _, _| f(ws)).unwrap_or(false) {
            return Ok(());
        }
        sleep(cx, 50).await;
    }
    Err(format!("timeout waiting for {what}"))
}

async fn next_frame(window: WindowHandle<Workspace>, cx: &mut AsyncApp) {
    let (tx, rx) = futures::channel::oneshot::channel();
    window
        .update(cx, |_, w, _| {
            w.on_next_frame(move |_, _| {
                let _ = tx.send(());
            });
            w.refresh();
        })
        .ok();
    let _ = rx.await;
}

fn snapshot(ws: &Workspace, label: &str) -> Value {
    let phase = match &ws.phase {
        Phase::Ready => "ready".to_string(),
        Phase::Starting => "starting".to_string(),
        Phase::Failed(f) => format!("failed:{}", f.code),
    };
    json!({
        "record": label,
        "phase": phase,
        "chat": ws.chat.as_ref().map(|c| json!({"title": c.title, "archived": c.archived})),
        "projects": ws.projects.iter().map(|p| json!([p.name, p.chats, p.archived_chats])).collect::<Vec<_>>(),
        "sessions": ws.chats.iter().map(|c| c.title.clone()).collect::<Vec<_>>(),
        "messages": ws.msgs.len(),
        "texts": ws.msgs.iter().rev().take(4).rev().map(|m| m.text.chars().take(160).collect::<String>()).collect::<Vec<_>>(),
        "metas": ws.msgs.iter().rev().take(4).rev().map(|m| m.meta.clone()).collect::<Vec<_>>(),
        "blocks_last": ws.msgs.last().map(|m| m.blocks.len()),
        "model": ws.model,
        "ollama": ws.models.as_ref().map(|m| json!({"reachable": m.reachable, "models": m.models.len()})),
        "perf": format!("{:?}", ws.perf),
    })
}

async fn run(cmd: &str, arg: &str, window: WindowHandle<Workspace>, cx: &mut AsyncApp) -> Result<Option<Value>, String> {
    match cmd {
        "sleep" => sleep(cx, arg.parse().unwrap_or(500)).await,
        "wait_ready" => wait_until(cx, window, arg.parse().unwrap_or(30), "ready", |ws| matches!(ws.phase, Phase::Ready) && !ws.projects.is_empty()).await?,
        "wait_failed" => wait_until(cx, window, arg.parse().unwrap_or(30), "failure", |ws| matches!(ws.phase, Phase::Failed(_))).await?,
        "wait_models" => wait_until(cx, window, 30, "models", |ws| ws.models.is_some()).await?,
        "record" => return Ok(window.update(cx, |ws, _, _| snapshot(ws, arg)).ok()),
        "new_chat" => {
            window.update(cx, |ws, _, cx| ws.new_chat(cx)).ok();
        }
        "send" => {
            let t = arg.replace("\\n", "\n");
            window.update(cx, |ws, w, cx| ws.send(t, w, cx)).ok();
            wait_until(cx, window, 5, "send started", |ws| !ws.msgs.is_empty()).await?;
        }
        "wait_reply" => {
            let t0 = Instant::now();
            let mut first: Option<u128> = None;
            let end = Instant::now() + Duration::from_secs(arg.parse().unwrap_or(300));
            loop {
                let (streaming, has_text) = window
                    .update(cx, |ws, _, _| {
                        let m = ws.msgs.last();
                        (m.map(|m| m.streaming).unwrap_or(false), m.map(|m| !m.text.is_empty()).unwrap_or(false))
                    })
                    .unwrap_or((false, false));
                if has_text && first.is_none() {
                    first = Some(t0.elapsed().as_millis());
                }
                if !streaming {
                    break;
                }
                if Instant::now() > end {
                    return Err("reply timeout".into());
                }
                sleep(cx, 20).await;
            }
            return Ok(Some(json!({"step": "wait_reply", "first_text_ms": first, "total_ms": t0.elapsed().as_millis()})));
        }
        "open_chat" => {
            let id = window
                .update(cx, |ws, _, _| ws.chats.iter().find(|c| c.title.contains(arg)).map(|c| c.id.clone()))
                .ok()
                .flatten();
            wait_until(cx, window, 10, "chat listed", |ws| ws.chats.iter().any(|c| c.title.contains(arg))).await?;
            let id = id.or_else(|| window.update(cx, |ws, _, _| ws.chats.iter().find(|c| c.title.contains(arg)).map(|c| c.id.clone())).ok().flatten()).ok_or("no such chat")?;
            let t0 = Instant::now();
            window.update(cx, |ws, _, cx| ws.open_chat(id.clone(), cx)).ok();
            wait_until(cx, window, 60, "chat open", |ws| ws.chat.as_ref().map(|c| c.id == id).unwrap_or(false) && !ws.msgs.is_empty()).await?;
            next_frame(window, cx).await;
            return Ok(Some(json!({"step": "open_chat", "open_to_painted_ms": t0.elapsed().as_millis()})));
        }
        "select_project" => {
            let id = window.update(cx, |ws, _, _| ws.projects.iter().find(|p| p.name == arg).map(|p| p.id.clone())).ok().flatten().ok_or("no such project")?;
            window.update(cx, |ws, _, cx| ws.select_project(id, cx)).ok();
            sleep(cx, 400).await;
        }
        "archive" | "unarchive" => {
            let want = cmd == "archive";
            window.update(cx, |ws, _, cx| ws.set_archived(want, cx)).ok();
            wait_until(cx, window, 10, "archive state", move |ws| ws.chat.as_ref().map(|c| c.archived == want).unwrap_or(false)).await?;
        }
        "add_project" => {
            let name = arg.to_string();
            window.update(cx, |ws, w, cx| ws.add_project_named(name.clone(), w, cx)).ok();
            let n = arg.to_string();
            wait_until(cx, window, 10, "project added", move |ws| ws.projects.iter().any(|p| p.name == n)).await?;
            sleep(cx, 300).await;
        }
        "select_model" => {
            let name = arg.to_string();
            wait_until(cx, window, 15, "models", |ws| ws.models.is_some()).await?;
            window.update(cx, |ws, _, cx| { ws.model = Some(name.clone()); cx.notify(); }).ok();
        }
        "open_chat_nowait" => {
            // click a chat and do not wait for it (to race it against the next step)
            wait_until(cx, window, 10, "chat listed", |ws| ws.chats.iter().any(|c| c.title.contains(arg))).await?;
            let id = window.update(cx, |ws, _, _| ws.chats.iter().find(|c| c.title.contains(arg)).map(|c| c.id.clone())).ok().flatten().ok_or("no such chat")?;
            window.update(cx, |ws, _, cx| ws.open_chat(id, cx)).ok();
        }
        "wait_streaming" => {
            // until the reply has visible text and is still streaming
            wait_until(cx, window, 120, "streaming text", |ws| {
                ws.is_running() && ws.msgs.last().map(|m| m.streaming && m.text.len() > 20).unwrap_or(false)
            }).await?;
        }
        "wait_idle" => {
            wait_until(cx, window, arg.parse().unwrap_or(60), "no reply running", |ws| !ws.is_running()).await?;
        }
        "expect" => {
            let (what, val) = arg.split_once(' ').map(|(a, b)| (a, b.trim())).unwrap_or((arg, ""));
            let got: String = window.update(cx, |ws, _, _| match what {
                "messages" => ws.msgs.len().to_string(),
                "chat" => ws.chat.as_ref().map(|c| c.title.clone()).unwrap_or_else(|| "none".into()),
                "running" => ws.is_running().to_string(),
                "last_meta" => ws.msgs.last().map(|m| m.meta.clone()).unwrap_or_default(),
                "last_text_len_over" => (ws.msgs.last().map(|m| m.text.len()).unwrap_or(0) > val.parse::<usize>().unwrap_or(0)).to_string(),
                "phase" => match &ws.phase { Phase::Ready => "ready".into(), Phase::Starting => "starting".into(), Phase::Failed(f) => f.code.clone() },
                "sessions" => ws.chats.iter().map(|c| c.title.clone()).collect::<Vec<_>>().join(" | "),
                "opening" => ws.opening_title().unwrap_or_else(|| "none".into()),
                _ => "?".into(),
            }).map_err(|_| "window gone".to_string())?;
            let ok = match what {
                "chat" if val != "none" => got.contains(val),
                "last_meta" | "sessions" => got.contains(val),
                "last_text_len_over" => got == "true",
                _ => got == val,
            };
            return Ok(Some(if ok { json!({"expect": arg, "ok": true}) } else { json!({"step": format!("expect {arg}"), "error": format!("got {got:?}")}) }));
        }
        "hover" => {
            // move the pointer (window points from the top left) so hover styles can be
            // screenshotted: an NSMouseMoved event sent to our own window, in-process.
            let mut it = arg.split_whitespace().map(|v| v.parse::<f64>().unwrap_or(0.));
            let (x, y) = (it.next().unwrap_or(0.), it.next().unwrap_or(0.));
            mouse_moved(x, y);
            sleep(cx, 50).await;
            next_frame(window, cx).await;
        }
        "kill_backend" => {
            let pid = window.update(cx, |ws, _, _| ws.backend.pid()).ok().flatten().ok_or("no backend")?;
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
        }
        "retry" => {
            window.update(cx, |ws, _, cx| ws.retry(cx)).ok();
        }
        "select_all_copy" => {
            window
                .update(cx, |ws, w, cx| {
                    w.focus(&gpui::Focusable::focus_handle(ws, cx));
                    w.dispatch_action(Box::new(crate::actions::SelectAll), cx);
                    w.dispatch_action(Box::new(crate::actions::Copy), cx);
                })
                .ok();
            sleep(cx, 100).await;
            let clip = cx.update(|cx| cx.read_from_clipboard().and_then(|c| c.text())).ok().flatten().unwrap_or_default();
            return Ok(Some(json!({"step": "select_all_copy", "chars": clip.chars().count(), "head": clip.chars().take(80).collect::<String>()})));
        }
        "scroll_test" => {
            // scroll from the end to the top in steps, timing each frame
            let steps: usize = arg.parse().unwrap_or(60);
            let mut frames = vec![];
            for _ in 0..steps {
                let t = Instant::now();
                window.update(cx, |ws, _, _| ws.list.scroll_by(px(-600.))).ok();
                next_frame(window, cx).await;
                frames.push(t.elapsed().as_secs_f64() * 1000.0);
            }
            window.update(cx, |ws, _, _| ws.list.scroll_to(ListOffset { item_ix: 0, offset_in_item: px(0.) })).ok();
            next_frame(window, cx).await;
            frames.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let pct = |p: f64| frames[((frames.len() as f64 - 1.0) * p) as usize];
            return Ok(Some(json!({"step": "scroll_test", "frames": frames.len(), "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": pct(1.0)})));
        }
        "screenshot" => {
            next_frame(window, cx).await;
            sleep(cx, 150).await;
            screenshot(arg)?;
        }
        "quit" => {
            cx.update(|cx| cx.quit()).ok();
        }
        other => return Err(format!("unknown command {other}")),
    }
    Ok(None)
}

/// Capture this app's own window to a PNG (no Screen Recording permission is
/// needed for an app's own windows). Test builds only.
pub fn screenshot(path: &str) -> Result<(), String> {
    use std::ffi::c_void;
    type CFTypeRef = *const c_void;
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct CGRect { x: f64, y: f64, w: f64, h: f64 }
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGWindowListCopyWindowInfo(option: u32, relative: u32) -> CFTypeRef;
        fn CGWindowListCreateImage(rect: CGRect, option: u32, window: u32, image_option: u32) -> CFTypeRef;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFArrayGetCount(a: CFTypeRef) -> isize;
        fn CFArrayGetValueAtIndex(a: CFTypeRef, i: isize) -> CFTypeRef;
        fn CFDictionaryGetValue(d: CFTypeRef, k: CFTypeRef) -> CFTypeRef;
        fn CFNumberGetValue(n: CFTypeRef, ty: i32, out: *mut c_void) -> bool;
        fn CFStringCreateWithCString(alloc: CFTypeRef, s: *const i8, enc: u32) -> CFTypeRef;
        fn CFURLCreateFromFileSystemRepresentation(alloc: CFTypeRef, buf: *const u8, len: isize, dir: bool) -> CFTypeRef;
        fn CFRelease(x: CFTypeRef);
    }
    #[link(name = "ImageIO", kind = "framework")]
    extern "C" {
        fn CGImageDestinationCreateWithURL(url: CFTypeRef, ty: CFTypeRef, count: usize, opts: CFTypeRef) -> CFTypeRef;
        fn CGImageDestinationAddImage(d: CFTypeRef, img: CFTypeRef, props: CFTypeRef);
        fn CGImageDestinationFinalize(d: CFTypeRef) -> bool;
    }
    unsafe {
        let s = |t: &str| CFStringCreateWithCString(std::ptr::null(), std::ffi::CString::new(t).unwrap().as_ptr(), 0x0800_0100);
        let (k_pid, k_num, k_layer) = (s("kCGWindowOwnerPID"), s("kCGWindowNumber"), s("kCGWindowLayer"));
        let list = CGWindowListCopyWindowInfo(1 /* on screen only */, 0);
        if list.is_null() {
            return Err("no window list".into());
        }
        let me = std::process::id() as i32;
        let mut wid: u32 = 0;
        for i in 0..CFArrayGetCount(list) {
            let d = CFArrayGetValueAtIndex(list, i);
            let (mut pid, mut num, mut layer) = (0i32, 0i32, 0i32);
            CFNumberGetValue(CFDictionaryGetValue(d, k_pid), 3, &mut pid as *mut _ as *mut c_void);
            CFNumberGetValue(CFDictionaryGetValue(d, k_layer), 3, &mut layer as *mut _ as *mut c_void);
            if pid == me && layer == 0 {
                CFNumberGetValue(CFDictionaryGetValue(d, k_num), 3, &mut num as *mut _ as *mut c_void);
                wid = num as u32;
                break;
            }
        }
        CFRelease(list);
        if wid == 0 {
            return Err("own window not found".into());
        }
        let null = CGRect { x: f64::INFINITY, y: f64::INFINITY, w: 0.0, h: 0.0 };
        let img = CGWindowListCreateImage(null, 8 /* including window */, wid, 1 /* ignore framing */);
        if img.is_null() {
            return Err("capture refused".into());
        }
        let url = CFURLCreateFromFileSystemRepresentation(std::ptr::null(), path.as_ptr(), path.len() as isize, false);
        let dest = CGImageDestinationCreateWithURL(url, s("public.png"), 1, std::ptr::null());
        CGImageDestinationAddImage(dest, img, std::ptr::null());
        let ok = CGImageDestinationFinalize(dest);
        CFRelease(dest);
        CFRelease(url);
        CFRelease(img);
        if ok { Ok(()) } else { Err("png write failed".into()) }
    }
}

fn mouse_moved(x: f64, y_from_top: f64) {
    use objc::runtime::Object;
    use objc::{class, msg_send, sel, sel_impl};
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSPoint { x: f64, y: f64 }
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSRect { x: f64, y: f64, w: f64, h: f64 }
    unsafe {
        let app: *mut Object = msg_send![class!(NSApplication), sharedApplication];
        let windows: *mut Object = msg_send![app, windows];
        let win: *mut Object = msg_send![windows, firstObject];
        if win.is_null() { return; }
        let view: *mut Object = msg_send![win, contentView];
        let frame: NSRect = msg_send![view, frame];
        let num: isize = msg_send![win, windowNumber];
        let p = NSPoint { x, y: frame.h - y_from_top };
        let ev: *mut Object = msg_send![class!(NSEvent), mouseEventWithType: 5u64 /* NSEventTypeMouseMoved */
            location: p modifierFlags: 0u64 timestamp: 0f64 windowNumber: num context: std::ptr::null::<Object>()
            eventNumber: 0isize clickCount: 0isize pressure: 0f32];
        if !ev.is_null() {
            let _: () = msg_send![win, sendEvent: ev];
        }
    }
}
