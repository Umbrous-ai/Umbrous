"""M9: is a workspace Umbrous's own source? (docs/m9-coding.md, Working on itself)

Umbrous can work on its own repository like any other folder. What it must
never do is let an edit look like an update of the running app: the running
runtime has already loaded its code (from this folder when started with
bin/umbrous, or from inside Umbrous.app), and the native app is a compiled
binary. Changes take effect only through an explicit rebuild and restart.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import umbrous

RUNNING_FROM = Path(umbrous.__file__).resolve().parent.parent


def is_umbrous_source(root: str) -> bool:
    return (os.path.isfile(os.path.join(root, "umbrous", "__init__.py"))
            and os.path.isfile(os.path.join(root, "bin", "umbrous"))
            and os.path.isdir(os.path.join(root, "desktop")))


def note(root: str) -> Optional[str]:
    """What the model and the report are told, or None for any other folder."""
    if not is_umbrous_source(root):
        return None
    here = os.path.realpath(str(RUNNING_FROM)) == os.path.realpath(root)
    where = ("The running runtime was started from this folder, but it has already loaded its code"
             if here else f"The running Umbrous runs from {RUNNING_FROM}, not from this folder")
    return (f"This folder is Umbrous's own source. {where}, so editing these files does not change the running "
            "app. Changes take effect only after a rebuild (desktop/scripts/build-native.sh for Umbrous.app) and a "
            "restart of Umbrous. Do not say the running app was updated.")
