"""AccessRoutes; authentication and body limits remain in the common dispatcher."""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import parse_qs

from umbrous.logging_setup import log_event
from umbrous.gate.gate import GateError
from umbrous.api.contracts import CHAT_PATH, ACCESS_PATH, APPROVAL_PATH, TASK_ID_RE

log = logging.getLogger("umbrous.api")


class AccessRoutes:
    def _access(self, path: str) -> Tuple[int, str]:
        """The access view, approvals and receipts (M3)."""
        rt, cmd = self.runtime, self.command
        if path == "/api/receipts":
            if cmd != "GET":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            chat_id, task_id = (q.get("chat_id") or [""])[0], (q.get("task_id") or [""])[0]
            if task_id and TASK_ID_RE.match(task_id):
                records = rt.receipts.for_task(task_id)
            elif chat_id and CHAT_PATH.match("/api/chats/" + chat_id):
                records = rt.receipts.for_chat(chat_id)
            else:
                self._send_json(400, {"error": "give chat_id or task_id"})
                return 400, "bad_query"
            self._send_json(200, {"receipts": records})
            return 200, "receipts"
        m = APPROVAL_PATH.match(path)
        if m:
            if cmd != "POST":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            body = self._read_json()
            if body is None:
                self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
                return 400, "bad_json"
            try:
                rt.gate.approvals.answer(m.group(1), str(body.get("choice", "")))
            except GateError as exc:
                self._send_json(409, {"error": "approval_closed", "message": str(exc)})
                return 409, "approval_closed"
            log_event(log, "gate.approval_answered", approval_id=m.group(1), choice=body.get("choice"))
            self._send_json(200, {"ok": True})
            return 200, "approval_answered"
        m = ACCESS_PATH.match(path)
        project_id, what = m.group(1), m.group(2)
        rt.history.get_project(project_id)  # 404 for an unknown project
        if what == "access":
            if cmd != "GET":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            self._send_json(200, rt.access.view(project_id))
            return 200, "access_view"
        if cmd != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if what == "workspace":
            rt.access.attach(project_id, body.get("root"), str(body.get("profile") or "workspace_read"))
            reason = "workspace_attached"
        elif what == "workspace/detach":
            rt.access.detach(project_id)
            reason = "workspace_detached"
        elif what == "profile":
            rt.access.set_profile(project_id, str(body.get("profile", "")))
            reason = "profile_changed"
        else:
            rt.access.revoke(project_id, m.group(3))
            reason = "grant_revoked"
        log_event(log, "access." + reason, project_id=project_id)  # never the folder path
        self._send_json(200, rt.access.view(project_id))
        return 200, reason
