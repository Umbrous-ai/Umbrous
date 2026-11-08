//! Test-only driver (compiled only with `--features e2e`, never in a release
//! bundle). Reads commands from UMBROUS_E2E_SCRIPT, one per line, drives the
//! real window against the real bundled runtime, and appends one JSON object
//! per `record` to UMBROUS_E2E_OUT. See desktop/e2e/.

use std::io::Write;
use std::time::{Duration, Instant};

use gpui::{px, App, AsyncApp, ListOffset, WindowHandle};
use serde_json::{json, Value};

use crate::workspace::{Phase, Workspace};
use crate::transcript::Role;

pub fn start(window: WindowHandle<Workspace>, cx: &mut App) {
    let (Ok(script), Ok(out)) = (std::env::var("UMBROUS_E2E_SCRIPT"), std::env::var("UMBROUS_E2E_OUT")) else { return };
    let Ok(text) = std::fs::read_to_string(script) else { return };
    let _ = std::fs::remove_file(&out);
    cx.spawn(async move |cx| {
        let mut failed = false;
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
                    failed = true;
                    break;
                }
            }
            if cmd == "quit" {
                break;
            }
        }
        if failed {
            // a failed step skips the script's own `quit`: quit anyway, so a
            // harness waiting on the app (open -W) gets the failure instead of hanging
            cx.update(|cx| cx.quit()).ok();
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
        "access": ws.access.as_ref().map(|a| json!({"project": a.project_id, "profile": a.workspace.as_ref().map(|w| w.profile.clone())})),
        "ask_notice": ws.asks_before_read(),
        "interrupted": ws.interrupted.as_ref().map(|i| json!({"task": i.task_id, "text": i.text, "report": i.report})),
        "memory": ws.memory.as_ref().map(|v| json!({
            "project_id": v.project_id,
            "personal": v.personal.iter().map(|m| json!([m.id, m.kind, m.text, m.applies])).collect::<Vec<_>>(),
            "project": v.project.iter().map(|m| json!([m.id, m.kind, m.text])).collect::<Vec<_>>(),
            "candidates": v.candidates.iter().map(|c| json!([c.id, c.store, c.kind, c.basis, c.text])).collect::<Vec<_>>()})),
        "use_memory": ws.use_memory,
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
        "wait_access" => {
            // until the selected project's access has loaded (M3)
            wait_until(cx, window, 10, "access", |ws| ws.access.as_ref().map(|a| ws.project_id.as_deref() == Some(a.project_id.as_str())).unwrap_or(false)).await?;
        }
        "switch_workspace_read" => {
            // what the notice's "Switch to Workspace read" button does
            window.update(cx, |ws, _, cx| ws.switch_to_workspace_read(cx)).ok();
            wait_until(cx, window, 10, "profile switched", |ws| !ws.asks_before_read()).await?;
        }
        "wait_memory" => {
            // until the selected project's memory view has loaded (M6)
            wait_until(cx, window, 10, "memory", |ws| ws.memory.as_ref()
                .map(|v| v.project_id.as_deref() == ws.project_id.as_deref()).unwrap_or(false)).await?;
        }
        "memory_add" => {
            // what the Memory section's + and Enter do: `memory_add personal TEXT` or `memory_add project TEXT`
            let (store, text) = arg.split_once(' ').ok_or("memory_add personal|project TEXT")?;
            let store: &'static str = if store == "personal" { "personal" } else { "project" };
            let t = text.trim().to_string();
            window.update(cx, |ws, _, cx| ws.add_memory(store, t.clone(), cx)).ok();
            let t = text.trim().to_string();
            wait_until(cx, window, 10, "memory added", move |ws| memory_texts(ws).iter().any(|x| *x == t)).await?;
        }
        "memory_edit" => {
            // what clicking an item, editing it and Enter do: `memory_edit OLD|NEW`
            let (old, new) = arg.split_once('|').ok_or("memory_edit OLD|NEW")?;
            let id = memory_id(window, cx, old)?;
            let n = new.to_string();
            window.update(cx, |ws, _, cx| ws.edit_memory(id, n.clone(), cx)).ok();
            let n = new.to_string();
            wait_until(cx, window, 10, "memory edited", move |ws| memory_texts(ws).iter().any(|x| *x == n)).await?;
        }
        "memory_delete" => {
            // what pressing × twice does: `memory_delete TEXT` (an item containing TEXT)
            let id = memory_id(window, cx, arg)?;
            let gone = id.clone();
            window.update(cx, |ws, _, cx| ws.delete_memory(id, cx)).ok();
            wait_until(cx, window, 10, "memory deleted", move |ws| ws.memory_item(&gone).is_none()).await?;
        }
        "wait_candidates" => {
            // until N suggestions wait for review in the selected project (M7)
            let n: usize = arg.parse().map_err(|_| "wait_candidates N")?;
            wait_until(cx, window, 10, "candidates", move |ws| ws.memory.as_ref().map(|v| v.candidates.len() == n).unwrap_or(false)).await?;
        }
        "candidate_accept" => {
            // what a suggestion's Accept button does: `candidate_accept TEXT` (a suggestion containing TEXT)
            let (id, text) = candidate(window, cx, arg)?;
            let gone = id.clone();
            window.update(cx, |ws, _, cx| ws.accept_candidate(id, None, cx)).ok();
            wait_until(cx, window, 10, "accepted", move |ws| ws.candidate(&gone).is_none()
                && memory_texts(ws).iter().any(|x| *x == text)).await?;
        }
        "candidate_edit_accept" => {
            // what clicking a suggestion, editing it and Enter do: `candidate_edit_accept OLD|NEW`
            let (old, new) = arg.split_once('|').ok_or("candidate_edit_accept OLD|NEW")?;
            let (id, _) = candidate(window, cx, old)?;
            let (gone, n) = (id.clone(), new.to_string());
            window.update(cx, |ws, _, cx| ws.accept_candidate(id, Some(n), cx)).ok();
            let n = new.to_string();
            wait_until(cx, window, 10, "accepted", move |ws| ws.candidate(&gone).is_none()
                && memory_texts(ws).iter().any(|x| *x == n)).await?;
        }
        "candidate_reject" => {
            // what a suggestion's Reject button does: `candidate_reject TEXT`
            let (id, _) = candidate(window, cx, arg)?;
            let gone = id.clone();
            window.update(cx, |ws, _, cx| ws.reject_candidate(id, cx)).ok();
            wait_until(cx, window, 10, "rejected", move |ws| ws.memory.as_ref().is_some() && ws.candidate(&gone).is_none()).await?;
        }
        "reset_open" => {
            // Start fresh… (no argument), or Options › Delete chat… on the open chat (`reset_open chat`) (M8)
            let on_chat = arg == "chat";
            window.update(cx, |ws, _, cx| { let id = if on_chat { ws.chat.as_ref().map(|c| c.id.clone()) } else { None }; ws.open_reset(id, cx) }).ok();
            wait_until(cx, window, 10, "start fresh", |ws| ws.reset.as_ref().map(|r| r.chats.is_some()).unwrap_or(false)).await?;
            if on_chat {
                wait_until(cx, window, 10, "preview", |ws| ws.reset.as_ref().map(|r| r.preview.is_some() || r.error.is_some()).unwrap_or(false)).await?;
            }
        }
        "reset_race" => {
            // `reset_race TITLE A | TITLE B`: Delete chat… for A, cancelled while
            // A's preview is still loading (held 2.5 s), then Delete chat… for B.
            // A's late reply must not replace B's preview (M8 review).
            let (a, b) = arg.split_once('|').map(|(a, b)| (a.trim().to_string(), b.trim().to_string())).ok_or("reset_race A | B")?;
            let ids = window.update(cx, |ws, _, _| {
                let find = |t: &str| ws.chats.iter().find(|c| c.title.contains(t)).map(|c| c.id.clone());
                (find(&a), find(&b))
            }).map_err(|_| "window gone".to_string())?;
            let (Some(a_id), Some(b_id)) = ids else { return Err(format!("reset_race: no chat {a} or {b}")) };
            window.update(cx, |ws, _, cx| { ws.e2e_preview_delay_ms = 2500; ws.open_reset(Some(a_id), cx) }).ok();
            wait_until(cx, window, 10, "A's preview in flight", |ws| ws.reset.as_ref().map(|r| r.working).unwrap_or(false)).await?;
            window.update(cx, |ws, _, cx| { ws.close_reset(cx); ws.e2e_preview_delay_ms = 0; ws.open_reset(Some(b_id), cx) }).ok();
            wait_until(cx, window, 10, "B's preview", |ws| ws.reset.as_ref().map(|r| r.preview.is_some() || r.error.is_some()).unwrap_or(false)).await?;
            sleep(cx, 3500).await; // A's reply has come back by now
        }
        "reset_tick" => {
            // a tick box: all_chats | chat TITLE | all_personal | personal TEXT | all_project | project TEXT | start_fresh
            let (what, val) = arg.split_once(' ').map(|(a, b)| (a, b.trim().to_string())).unwrap_or((arg, String::new()));
            let tick = window.update(cx, |ws, _, _| -> Result<crate::workspace::ResetTick, String> {
                use crate::workspace::ResetTick as T;
                let r = ws.reset.as_ref().ok_or("start fresh is not open")?;
                let mem = ws.memory.as_ref().ok_or("no memory view")?;
                Ok(match what {
                    "all_chats" => T::AllChats,
                    "all_personal" => T::AllPersonal,
                    "all_project" => T::AllProject,
                    "start_fresh" => T::StartFresh,
                    "chat" => T::Chat(r.chats.as_ref().and_then(|c| c.iter().find(|c| c.title.contains(&val))).map(|c| c.id.clone()).ok_or("no such chat")?),
                    "personal" => T::Personal(mem.personal.iter().find(|m| m.text.contains(&val)).map(|m| m.id.clone()).ok_or("no such item")?),
                    "project" => T::Project(mem.project.iter().find(|m| m.text.contains(&val)).map(|m| m.id.clone()).ok_or("no such item")?),
                    _ => return Err(format!("reset_tick {what}?")),
                })
            }).map_err(|_| "window gone".to_string())??;
            window.update(cx, |ws, _, cx| ws.toggle_reset(tick, cx)).ok();
        }
        "reset_preview" => {
            window.update(cx, |ws, _, cx| ws.preview_reset(cx)).ok();
            wait_until(cx, window, 10, "preview", |ws| ws.reset.as_ref().map(|r| !r.working && (r.preview.is_some() || r.error.is_some())).unwrap_or(false)).await?;
        }
        "reset_confirm" => {
            // what Delete N items does: done, or refused with a new preview to check
            window.update(cx, |ws, _, cx| ws.confirm_reset(cx)).ok();
            wait_until(cx, window, 10, "reset answered", |ws| ws.reset.as_ref().map(|r| !r.working).unwrap_or(true)).await?;
            // once done, until the lists the app shows no longer hold what was deleted
            let gone: Vec<String> = window.update(cx, |ws, _, _| ws.reset.as_ref().and_then(|r| r.done.as_ref())
                .map(|d| d.items.iter().map(|i| i.id.clone()).collect()).unwrap_or_default()).unwrap_or_default();
            if !gone.is_empty() {
                wait_until(cx, window, 10, "lists reloaded", move |ws| !ws.chats.iter().any(|c| gone.contains(&c.id))
                    && ws.memory.as_ref().map(|v| !v.personal.iter().chain(v.project.iter()).any(|m| gone.contains(&m.id))).unwrap_or(false)).await?;
            }
        }
        "reset_cancel" => {
            window.update(cx, |ws, _, cx| ws.close_reset(cx)).ok();
        }
        "use_memory" => {
            let on = arg != "off";
            window.update(cx, |ws, _, cx| { ws.use_memory = on; cx.notify(); }).ok();
        }
        "resume" => {
            // what the interrupted notice's Resume button does (M5)
            window.update(cx, |ws, _, cx| ws.resume_task(cx)).ok();
            wait_until(cx, window, 10, "resume started", |ws| ws.is_running()).await?;
        }
        "stop" => {
            // what the Stop button does; records how long until the reply has ended (M4)
            let t0 = Instant::now();
            window.update(cx, |ws, _, cx| ws.stop_reply(cx)).ok();
            wait_until(cx, window, 10, "stopped", |ws| !ws.is_running()).await?;
            return Ok(Some(json!({"step": "stop", "stop_to_idle_ms": t0.elapsed().as_millis()})));
        }
        "wait_meta" => {
            // until the last reply's meta line contains the text (e.g. a running step)
            wait_until(cx, window, 60, "meta", |ws| ws.msgs.last().map(|m| m.meta.contains(arg)).unwrap_or(false)).await?;
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
                "last_text" => ws.msgs.last().map(|m| m.text.clone()).unwrap_or_default(),
                "no_text" => (!ws.msgs.iter().any(|m| m.text.contains(val))).to_string(),
                "user_meta_empty" => ws.msgs.iter().filter(|m| m.role == Role::User).all(|m| m.meta.is_empty()).to_string(),
                "access" => ws.access.as_ref().and_then(|a| a.workspace.as_ref()).map(|w| w.profile.clone()).unwrap_or_else(|| "none".into()),
                "ask_notice" => ws.asks_before_read().to_string(),
                "last_text_len_over" => (ws.msgs.last().map(|m| m.text.len()).unwrap_or(0) > val.parse::<usize>().unwrap_or(0)).to_string(),
                "phase" => match &ws.phase { Phase::Ready => "ready".into(), Phase::Starting => "starting".into(), Phase::Failed(f) => f.code.clone() },
                "sessions" | "sessions_lack" => ws.chats.iter().map(|c| c.title.clone()).collect::<Vec<_>>().join(" | "),
                "opening" => ws.opening_title().unwrap_or_else(|| "none".into()),
                "interrupted" => ws.interrupted.as_ref().map(|i| i.text.clone()).unwrap_or_else(|| "none".into()),
                "last_text_lacks" => (!ws.msgs.last().map(|m| m.text.contains(val)).unwrap_or(false)).to_string(),
                "reply_metas" => ws.msgs.iter().filter(|m| m.role == Role::Assistant).map(|m| m.meta.clone()).collect::<Vec<_>>().join(" | "),
                "memory" => memory_texts(ws).join(" | "),
                "memory_count" => memory_texts(ws).len().to_string(),
                "memory_project" => ws.memory.as_ref().map(|v| v.project.len()).unwrap_or(0).to_string(),
                "memory_personal" => ws.memory.as_ref().map(|v| v.personal.len()).unwrap_or(0).to_string(),
                "candidates" => ws.memory.as_ref().map(|v| v.candidates.iter()
                    .map(|c| format!("{} {} ({}): {}", c.store, c.kind, c.basis, c.text)).collect::<Vec<_>>().join(" | ")).unwrap_or_default(),
                "reset" => match &ws.reset {
                    None => "closed".into(),
                    Some(r) if r.done.is_some() => format!("done: {}", r.done.as_ref().unwrap().summary()),
                    Some(r) => match &r.preview {
                        Some(p) => format!("preview {} items{}: {}{}", p.items.len(),
                                           r.warning.as_ref().map(|w| format!(" (warning: {w})")).unwrap_or_default(),
                                           p.items.iter().map(|i| i.label()).collect::<Vec<_>>().join(" | "),
                                           format!(" || stays: {} {}", p.kept.iter().map(|k| k.text.clone()).collect::<Vec<_>>().join(" "), p.backups)),
                        None => format!("open{}", r.error.as_ref().map(|e| format!(" (error: {e})")).unwrap_or_default()),
                    },
                },
                _ => "?".into(),
            }).map_err(|_| "window gone".to_string())?;
            let ok = match what {
                "chat" if val != "none" => got.contains(val),
                "last_meta" | "sessions" | "last_text" | "reply_metas" | "memory" => got.contains(val),
                "reset" if val != "closed" => got.contains(val),
                "sessions_lack" => !got.contains(val),
                "candidates" if val != "none" => got.contains(val),
                "candidates" => got.is_empty(),
                "last_text_lacks" => got == "true",
                "interrupted" if val != "none" => got.contains(val),
                "no_text" | "user_meta_empty" => got == "true",
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
        "card" => {
            let open = arg != "close";
            window.update(cx, |ws, _, cx| ws.set_card_open(open, cx)).ok();
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
        "scroll_wheel_up" => {
            // a real scroll-wheel event over the transcript (up by PX), then check that
            // later frames do not move the view back toward the end
            let dy: f32 = arg.parse().unwrap_or(40.);
            let top = |ws: &Workspace| -f32::from(ws.list.scroll_px_offset_for_scrollbar().y);
            let (top0, state0) = window.update(cx, |ws, _, _| (top(ws), ws.scroll_state())).map_err(|_| "window gone".to_string())?;
            let at = window.update(cx, |ws, _, _| ws.list.viewport_bounds().center()).map_err(|_| "window gone".to_string())?;
            let sent = scroll_wheel(f64::from(f32::from(at.x)), f64::from(f32::from(at.y)), dy as i32);
            next_frame(window, cx).await;
            let (top1, state1) = window.update(cx, |ws, _, _| (top(ws), ws.scroll_state())).map_err(|_| "window gone".to_string())?;
            for _ in 0..20 {
                next_frame(window, cx).await;
            }
            sleep(cx, 300).await;
            next_frame(window, cx).await;
            let (top2, state2) = window.update(cx, |ws, _, _| (top(ws), ws.scroll_state())).map_err(|_| "window gone".to_string())?;
            let rec = json!({"step": "scroll_wheel_up", "top_before": top0, "top_after_event": top1, "top_after_frames": top2,
                             "follow_reveal": [state0, state1, state2],
                             "at": [f32::from(at.x), f32::from(at.y)], "sent": sent});
            if top1 > top0 - 1. {
                return Ok(Some(json!({"step": "scroll_wheel_up", "error": format!("the view is not above where it was (the wheel event did not scroll, or render undid it in the same frame): {rec}")})));
            }
            if top2 > top1 + 0.5 {
                return Ok(Some(json!({"step": "scroll_wheel_up", "error": format!("the view moved back toward the end: {rec}")})));
            }
            return Ok(Some(rec));
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

fn memory_texts(ws: &Workspace) -> Vec<String> {
    ws.memory.as_ref().map(|v| v.personal.iter().chain(v.project.iter()).map(|m| m.text.clone()).collect()).unwrap_or_default()
}

fn candidate(window: WindowHandle<Workspace>, cx: &mut AsyncApp, text: &str) -> Result<(String, String), String> {
    window
        .update(cx, |ws, _, _| ws.memory.as_ref().and_then(|v| v.candidates.iter()
            .find(|c| c.text.contains(text)).map(|c| (c.id.clone(), c.text.clone()))))
        .ok()
        .flatten()
        .ok_or_else(|| format!("no suggestion containing {text:?}"))
}

fn memory_id(window: WindowHandle<Workspace>, cx: &mut AsyncApp, text: &str) -> Result<String, String> {
    window
        .update(cx, |ws, _, _| ws.memory.as_ref().and_then(|v| v.personal.iter().chain(v.project.iter())
            .find(|m| m.text.contains(text)).map(|m| m.id.clone())))
        .ok()
        .flatten()
        .ok_or_else(|| format!("no memory item containing {text:?}"))
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

/// A scroll-wheel event (pixel units, `dy` > 0 scrolls up) at a window point (from
/// the top left), handed straight to our own window's content view, in-process.
fn scroll_wheel(x: f64, y_from_top: f64, dy: i32) -> Value {
    use objc::runtime::Object;
    use objc::{class, msg_send, sel, sel_impl};
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct CGPoint { x: f64, y: f64 }
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSRect { x: f64, y: f64, w: f64, h: f64 }
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventCreateScrollWheelEvent(source: *const std::ffi::c_void, units: u32, count: u32, wheel1: i32, ...) -> *mut std::ffi::c_void;
        fn CGEventSetLocation(ev: *mut std::ffi::c_void, p: CGPoint);
        fn CGEventSetIntegerValueField(ev: *mut std::ffi::c_void, field: u32, value: i64);
        fn CGMainDisplayID() -> u32;
        fn CGDisplayPixelsHigh(display: u32) -> usize;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: *const std::ffi::c_void);
    }
    unsafe {
        let app: *mut Object = msg_send![class!(NSApplication), sharedApplication];
        let windows: *mut Object = msg_send![app, windows];
        let win: *mut Object = msg_send![windows, firstObject];
        if win.is_null() { return json!("no window"); }
        let view: *mut Object = msg_send![win, contentView];
        let frame: NSRect = msg_send![view, frame];
        let ev = CGEventCreateScrollWheelEvent(std::ptr::null(), 0 /* pixel */, 1, dy);
        if ev.is_null() { return json!("no event"); }
        CGEventSetIntegerValueField(ev, 88 /* kCGScrollWheelEventIsContinuous */, 1);
        // an event with no window reports its screen point as locationInWindow; place it
        // so that point is the window point we want (GPUI flips y with the view height)
        let screen_h = CGDisplayPixelsHigh(CGMainDisplayID()) as f64;
        CGEventSetLocation(ev, CGPoint { x, y: screen_h - (frame.h - y_from_top) });
        let ns: *mut Object = msg_send![class!(NSEvent), eventWithCGEvent: ev];
        let mut got = json!("no NSEvent or no GPUIView");
        // GPUI's own view (GPUIView) is a subview of the window's content view
        let subviews: *mut Object = msg_send![view, subviews];
        let n: usize = msg_send![subviews, count];
        let gpui_view = (0..n).map(|i| -> *mut Object { msg_send![subviews, objectAtIndex: i] })
            .find(|&v| { let c: *const objc::runtime::Class = msg_send![v, class]; (*c).name() == "GPUIView" });
        if let (false, Some(target)) = (ns.is_null(), gpui_view) {
            let loc: CGPoint = msg_send![ns, locationInWindow];
            let d: f64 = msg_send![ns, scrollingDeltaY];
            let precise: i8 = msg_send![ns, hasPreciseScrollingDeltas];
            let _: () = msg_send![target, scrollWheel: ns];
            got = json!({"location_in_window": [loc.x, loc.y], "view_h": frame.h, "delta_y": d, "precise": precise != 0});
        }
        CFRelease(ev);
        got
    }
}
