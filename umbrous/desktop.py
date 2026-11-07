"""The pipe between the Umbrous desktop app (Tauri, Rust) and this runtime.

`python -m umbrous serve --desktop` is how the desktop app starts the runtime.
It changes only how the runtime talks to the process that started it:

stdout carries one JSON object per line, and nothing else:

    {"event": "ready", "origin": "http://127.0.0.1:<port>", "launch_code": "...", ...}
    {"event": "launch_code", "launch_code": "..."}          answer to "reissue"
    {"event": "error", "code": "already_running" | "port_in_use" | "config_error"
                             | "database_error" | "startup_failed", "message": "..."}
    {"event": "stopped"}

Human-readable messages go to stderr instead.

stdin carries commands from the app, one per line:

    reissue      make a new one-time launch code (the window must load the page again)
    stop         shut down cleanly (same as SIGTERM)

When stdin closes (the app quit, or crashed and the OS closed its end of the
pipe), the runtime shuts down cleanly. So a desktop runtime never outlives its
app, and the app never has to look for, or signal, any process it did not start.

Why a pipe: the launch code then never appears on a command line (visible to
every local user in the process list) or in a file. The session credential
itself is never sent to the app at all; only the page holds it.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Callable, Optional

_out_lock = threading.Lock()


def emit(event: str, **fields) -> None:
    line = json.dumps({"event": event, **fields})
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def watch_stdin(on_stop: Callable[[], None], on_reissue: Callable[[], Optional[str]]) -> threading.Thread:
    """Read commands until stdin closes, then stop the runtime."""

    def run() -> None:
        try:
            for raw in sys.stdin:
                cmd = raw.strip()
                if cmd == "reissue":
                    code = on_reissue()
                    if code:
                        emit("launch_code", launch_code=code)
                elif cmd == "stop":
                    break
                # anything else is ignored
        except (OSError, ValueError):
            pass
        on_stop()

    t = threading.Thread(target=run, name="desktop-stdin", daemon=True)
    t.start()
    return t
