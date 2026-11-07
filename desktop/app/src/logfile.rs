//! `<Umbrous home>/logs/desktop.log`: one JSON line per shell event.
//!
//! Same rules as the runtime's own log (docs/m0-runtime.md): no chat content,
//! no launch codes or credentials, only ids, timings and outcomes. The
//! runtime's stderr is kept in memory for the error screen, never written here,
//! because a traceback could quote a message.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_BYTES: u64 = 1024 * 1024;
static LOCK: Mutex<()> = Mutex::new(());

/// Same rule as umbrous/config.py default_home().
pub fn umbrous_home() -> Option<PathBuf> {
    if let Ok(h) = std::env::var("UMBROUS_HOME") {
        if !h.is_empty() {
            return Some(PathBuf::from(h));
        }
    }
    let home = std::env::var("HOME").ok()?;
    Some(PathBuf::from(home).join("Library").join("Application Support").join("Umbrous"))
}

pub fn log_dir() -> Option<PathBuf> {
    umbrous_home().map(|h| h.join("logs"))
}

fn private_dir(p: &PathBuf) -> std::io::Result<()> {
    fs::create_dir_all(p)?;
    fs::set_permissions(p, fs::Permissions::from_mode(0o700))
}

pub fn event(name: &str, fields: &[(&str, String)]) {
    let Some(dir) = log_dir() else { return };
    let _guard = LOCK.lock();
    if let Some(home) = dir.parent() {
        if private_dir(&home.to_path_buf()).is_err() {
            return;
        }
    }
    if private_dir(&dir).is_err() {
        return;
    }
    let path = dir.join("desktop.log");
    if fs::metadata(&path).map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
        let _ = fs::rename(&path, dir.join("desktop.log.1"));
    }
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    let mut obj = serde_json::Map::new();
    obj.insert("ts_ms".into(), ts.to_string().parse::<u64>().map(Into::into).unwrap_or_default());
    obj.insert("event".into(), name.into());
    for (k, v) in fields {
        obj.insert((*k).into(), v.clone().into());
    }
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).mode(0o600).open(&path) {
        let _ = writeln!(f, "{}", serde_json::Value::Object(obj));
    }
}
