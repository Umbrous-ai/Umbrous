"""HistoryRoutes; authentication and body limits remain in the common dispatcher."""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import parse_qs

from umbrous.logging_setup import log_event
from umbrous.api.contracts import CHAT_PATH, PROJECT_ID_RE

log = logging.getLogger("umbrous.api")


class HistoryRoutes:
    def _history(self, path: str) -> Tuple[int, str]:
        """Projects and chats (M2). Archive and unarchive are the only changes
        to an existing chat; nothing here deletes."""
        hist, cmd = self.runtime.history, self.command
        if path == "/api/projects" and cmd == "GET":
            self._send_json(200, {"projects": [p.as_dict() for p in hist.list_projects()]})
            return 200, "projects"
        if path == "/api/chats" and cmd == "GET":
            query = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (query.get("project_id") or [None])[0]
            if project_id is not None and not PROJECT_ID_RE.fullmatch(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            archived = (query.get("archived") or ["0"])[0] in ("1", "true")
            self._send_json(200, {"project_id": project_id, "archived": archived,
                                  "chats": [c.as_dict() for c in hist.list_chats(project_id, archived)]})
            return 200, "chats"
        match = CHAT_PATH.match(path)
        if match and cmd == "GET" and not match.group(2):
            chat = hist.get_chat(match.group(1))
            self._send_json(200, {"chat": chat.as_dict(), "messages": [m.as_dict() for m in hist.messages(chat.id)]})
            return 200, "chat"
        if not match and path not in ("/api/projects", "/api/chats"):
            self._send_json(404, {"error": "not_found"})
            return 404, "not_found"
        if cmd != "POST" or (match and not match.group(2)):  # e.g. DELETE: there is no delete
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if path == "/api/projects":
            project = hist.create_project(body.get("name"))
            log_event(log, "history.project_created", project_id=project.id)
            self._send_json(201, {"project": project.as_dict()})
            return 201, "project_created"
        if path == "/api/chats":
            project_id = body.get("project_id")
            if project_id is not None and (not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id)):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            access = body.get("access")
            chat = hist.create_chat(project_id, access=str(access) if access is not None else None)
            log_event(log, "history.chat_created", chat_id=chat.id, project_id=project_id)
            self._send_json(201, {"chat": chat.as_dict()})
            return 201, "chat_created"
        if match.group(2) == "/access":
            # the chat's own access, or null for the project's; the gate reads it at every call
            access = body.get("access")
            chat = hist.set_access(match.group(1), str(access) if access is not None else None)
            log_event(log, "history.chat_access", chat_id=chat.id, access=chat.access or "project")
            self._send_json(200, {"chat": chat.as_dict()})
            return 200, "chat_access"
        archive = match.group(2) == "/archive"
        chat_id = hist.get_chat(match.group(1)).id  # raises for an unknown chat
        # Archiving takes the same per-chat lock as a reply. While a reply is
        # running the archive is refused, so the reply is always saved first
        # (the page stops the reply, waits for it to end, then archives).
        lock = self.runtime.chat_locks.get(chat_id)
        if not lock.acquire(blocking=False):
            self._send_json(409, {"error": "chat_busy", "message": "A reply is still running in this chat. "
                                  "Stop it or let it finish, then archive."})
            return 409, "chat_busy"
        try:
            chat = hist.set_archived(chat_id, archive)
        finally:
            lock.release()
        log_event(log, "history.chat_archived" if archive else "history.chat_unarchived", chat_id=chat.id)
        self._send_json(200, {"chat": chat.as_dict()})
        return 200, "archived" if archive else "unarchived"
