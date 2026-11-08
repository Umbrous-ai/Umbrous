"""Workspaces, access profiles and grants (M3): what each project may touch.

Each project has at most one workspace folder and exactly one access profile.
A profile says, for each capability, whether the gate allows it, asks first,
or blocks it. A grant ("allow for this project") turns "ask" into "allow" for
one capability until it is revoked.

Some things are never decided by the profile at all: paths outside the
workspace and excluded files (.env, keys, .git, ...) are blocked by the gate
under every profile, and no approval or grant can change that.

`policy_version` goes up whenever the workspace or profile changes. A one-time
approval remembers the version it was given under, so an approval given
before a change is stale afterwards and is refused.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

CAPABILITIES = ("fs.list", "fs.read", "fs.search", "fs.find", "fs.write", "fs.edit", "cmd.run", "cmd.check")
ALLOW, ASK, BLOCK = "allow", "ask", "block"
READS = ("fs.list", "fs.read", "fs.search", "fs.find")
WRITES = ("fs.write", "fs.edit")
COMMANDS = ("cmd.run", "cmd.check")


def _rules(reads: str, writes: str, commands: str) -> Dict[str, str]:
    # checking or stopping a command this task already started needs no second approval
    return dict({a: reads for a in READS}, **{a: writes for a in WRITES}, **{"cmd.run": commands},
                **{"cmd.check": BLOCK if commands == BLOCK else ALLOW})


PROFILES: Dict[str, dict] = {
    "workspace_read": {
        "label": "Workspace read",
        "description": "List, read and search files inside the workspace without asking. No changes, no commands.",
        "rules": _rules(ALLOW, BLOCK, BLOCK),
    },
    "ask_before_read": {
        "label": "Ask before read",
        "description": "Ask before every list, read or search (approve once, or allow for this project). "
                       "No changes, no commands.",
        "rules": _rules(ASK, BLOCK, BLOCK),
    },
    # M9: coding. Chosen by you: in Access, with `bin/umbrous workspace --profile`, or by naming
    # the folder in a message ("work on /path/to/project"). Commands run as you: not a sandbox.
    "workspace_code": {
        "label": "Code in workspace",
        "description": "Read, create and edit files inside the workspace, and run commands in it, without asking. "
                       "Commands run as you, with your permissions: this is not a sandbox.",
        "rules": _rules(ALLOW, ALLOW, ALLOW),
    },
    "code_ask_commands": {
        "label": "Code, ask before commands",
        "description": "Read, create and edit files inside the workspace without asking; ask before each command "
                       "(approve once, or allow for this project).",
        "rules": _rules(ALLOW, ALLOW, ASK),
    },
}
CODING_PROFILES = ("workspace_code", "code_ask_commands")
DEFAULT_PROFILE = "workspace_read"


class AccessError(ValueError):
    def __init__(self, message: str, code: str = "access_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Workspace:
    project_id: str
    root: str
    profile: str
    attached_at: str
    policy_version: int

    @property
    def name(self) -> str:
        return os.path.basename(self.root.rstrip("/")) or self.root

    def as_dict(self) -> dict:
        p = PROFILES[self.profile]
        return {"project_id": self.project_id, "root": self.root, "name": self.name, "profile": self.profile,
                "profile_label": p["label"], "attached_at": self.attached_at, "policy_version": self.policy_version}


@dataclass(frozen=True)
class Grant:
    id: str
    project_id: str
    capability: str
    granted_at: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def check_root(root: str, umbrous_home: Path) -> str:
    """Validate a folder offered as a workspace and return its real path."""
    if not isinstance(root, str) or not root.strip():
        raise AccessError("Give the full path of a folder.", "bad_workspace")
    root = os.path.expanduser(root.strip())
    if not os.path.isabs(root):
        raise AccessError("Give the full path of a folder (starting with / or ~).", "bad_workspace")
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise AccessError("That folder does not exist.", "bad_workspace")
    home = os.path.realpath(os.path.expanduser("~"))
    if real in ("/", home) or real == os.path.dirname(home):
        raise AccessError("Pick a specific project folder, not your whole home folder or disk.", "workspace_too_broad")
    data = os.path.realpath(str(umbrous_home))
    if data == real or data.startswith(real.rstrip("/") + "/") or real.startswith(data.rstrip("/") + "/"):
        raise AccessError("A workspace cannot contain, or be inside, Umbrous's own data folder.", "workspace_too_broad")
    return real


class AccessStore:
    def __init__(self, db_path: Path, umbrous_home: Path) -> None:
        self.db_path = Path(db_path)
        self.home = Path(umbrous_home)
        self._lock = threading.Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _project_exists(self, c: sqlite3.Connection, project_id: str) -> None:
        if c.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise AccessError("That project does not exist.", "no_such_project")

    def _bump(self, c: sqlite3.Connection, project_id: str) -> None:
        c.execute("INSERT INTO access_policy (project_id, version) VALUES (?, 1) "
                  "ON CONFLICT(project_id) DO UPDATE SET version = version + 1", (project_id,))

    def policy_version(self, project_id: str) -> int:
        with self._db() as c:
            row = c.execute("SELECT version FROM access_policy WHERE project_id = ?", (project_id,)).fetchone()
        return row[0] if row else 0

    def workspace(self, project_id: str) -> Optional[Workspace]:
        with self._db() as c:
            row = c.execute("""SELECT w.project_id, w.root, w.profile, w.attached_at, COALESCE(a.version, 0)
                               FROM workspaces w LEFT JOIN access_policy a ON a.project_id = w.project_id
                               WHERE w.project_id = ?""", (project_id,)).fetchone()
        return Workspace(*row) if row else None

    def attach(self, project_id: str, root: str, profile: str = DEFAULT_PROFILE) -> Workspace:
        if profile not in PROFILES:
            raise AccessError(f"Unknown profile {profile!r}.", "bad_profile")
        real = check_root(root, self.home)
        with self._lock, self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                self._project_exists(c, project_id)
                old = c.execute("SELECT root FROM workspaces WHERE project_id = ?", (project_id,)).fetchone()
                if old and old[0] != real:  # a different folder: earlier grants do not carry over
                    c.execute("UPDATE grants SET revoked_at = ? WHERE project_id = ? AND revoked_at IS NULL",
                              (_now(), project_id))
                c.execute("INSERT INTO workspaces (project_id, root, profile, attached_at) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(project_id) DO UPDATE SET root = excluded.root, profile = excluded.profile, "
                          "attached_at = excluded.attached_at", (project_id, real, profile, _now()))
                self._bump(c, project_id)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return self.workspace(project_id)

    def set_profile(self, project_id: str, profile: str) -> Workspace:
        if profile not in PROFILES:
            raise AccessError(f"Unknown profile {profile!r}.", "bad_profile")
        with self._lock, self._db() as c:
            if c.execute("UPDATE workspaces SET profile = ? WHERE project_id = ?", (profile, project_id)).rowcount == 0:
                raise AccessError("This project has no workspace.", "no_workspace")
            self._bump(c, project_id)
        return self.workspace(project_id)

    def detach(self, project_id: str) -> None:
        """Forget the workspace folder (nothing in the folder is touched) and revoke its grants."""
        with self._lock, self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                if c.execute("DELETE FROM workspaces WHERE project_id = ?", (project_id,)).rowcount == 0:
                    raise AccessError("This project has no workspace.", "no_workspace")
                c.execute("UPDATE grants SET revoked_at = ? WHERE project_id = ? AND revoked_at IS NULL",
                          (_now(), project_id))
                self._bump(c, project_id)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- grants
    def grant(self, project_id: str, capability: str) -> Grant:
        if capability not in CAPABILITIES:
            raise AccessError(f"Unknown capability {capability!r}.", "bad_capability")
        ws = self.workspace(project_id)
        if ws is None:
            raise AccessError("This project has no workspace.", "no_workspace")
        existing = self.active_grant(project_id, capability)
        if existing:
            return existing
        gid = "grt_" + secrets.token_hex(8)
        with self._lock, self._db() as c:
            c.execute("INSERT INTO grants (id, project_id, capability, policy_version, granted_at) VALUES (?, ?, ?, ?, ?)",
                      (gid, project_id, capability, ws.policy_version, _now()))
        return self.active_grant(project_id, capability)

    def active_grant(self, project_id: str, capability: str) -> Optional[Grant]:
        with self._db() as c:
            row = c.execute("SELECT id, project_id, capability, granted_at FROM grants "
                            "WHERE project_id = ? AND capability = ? AND revoked_at IS NULL", (project_id, capability)).fetchone()
        return Grant(*row) if row else None

    def grants(self, project_id: str) -> List[Grant]:
        with self._db() as c:
            rows = c.execute("SELECT id, project_id, capability, granted_at FROM grants "
                             "WHERE project_id = ? AND revoked_at IS NULL ORDER BY granted_at", (project_id,)).fetchall()
        return [Grant(*r) for r in rows]

    def revoke(self, project_id: str, grant_id: str) -> None:
        with self._lock, self._db() as c:
            n = c.execute("UPDATE grants SET revoked_at = ? WHERE id = ? AND project_id = ? AND revoked_at IS NULL",
                          (_now(), grant_id, project_id)).rowcount
        if n == 0:
            raise AccessError("No such active grant in this project.", "no_such_grant")

    def view(self, project_id: str) -> dict:
        """Everything the access view shows for one project."""
        with self._db() as c:
            self._project_exists(c, project_id)
        ws = self.workspace(project_id)
        return {
            "project_id": project_id,
            "workspace": ws.as_dict() if ws else None,
            "policy_version": self.policy_version(project_id),
            "grants": [g.as_dict() for g in self.grants(project_id)],
            "profiles": [{"id": k, "label": v["label"], "description": v["description"], "rules": v["rules"]}
                         for k, v in PROFILES.items()],
            "always_blocked": "Paths outside the workspace, symbolic links, hard-linked files, and excluded "
                              "files (.env, keys, credentials, .git, .ssh, ...), under every profile.",
        }
