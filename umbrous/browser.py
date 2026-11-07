"""Open the page in a dedicated, extension-free browser profile.

Why: any browser extension allowed on the Umbrous page can read it and use its
session credential to call the API as you. Your everyday browser profile
probably has extensions, so Umbrous launches a Chromium-family browser with
its own profile directory (<home>/browser-profile) and extensions disabled.

If no Chromium browser is installed, it falls back to the default browser and
says so, because that fallback is weaker.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from umbrous.logging_setup import log_event

log = logging.getLogger("umbrous.browser")

# Arc and Safari are left out: they do not support a separate --user-data-dir.
MAC_APPS = ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium")
LINUX_BINARIES = ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge")


def chromium_args(profile_dir: Path, url: str) -> List[str]:
    return [
        f"--user-data-dir={profile_dir}",
        "--disable-extensions",
        "--no-first-run",
        "--no-default-browser-check",
        f"--app={url}",
        "--window-size=1440,900",
    ]


def _find_mac_app() -> Optional[str]:
    for app in MAC_APPS:
        if subprocess.run(["open", "-Ra", app], capture_output=True).returncode == 0:
            return app
    return None


def open_page(url: str, profile_dir: Path) -> str:
    """Returns a short description of how the page was opened."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    if sys.platform == "darwin":
        app = _find_mac_app()
        if app:
            subprocess.run(["open", "-na", app, "--args", *chromium_args(profile_dir, url)], check=False)
            log_event(log, "browser.opened", app=app, profile="dedicated", extensions="disabled")
            return f"{app} (dedicated profile, extensions off)"
    else:
        for name in LINUX_BINARIES:
            binary = shutil.which(name)
            if binary:
                subprocess.Popen([binary, *chromium_args(profile_dir, url)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log_event(log, "browser.opened", app=name, profile="dedicated", extensions="disabled")
                return f"{name} (dedicated profile, extensions off)"
    webbrowser.open(url)
    log_event(log, "browser.opened", level=logging.WARNING, app="default", profile="default",
              warning="extensions in your normal profile can read the page and use its credential")
    return "your default browser (WARNING: its extensions can see the page)"
