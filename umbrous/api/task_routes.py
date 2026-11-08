"""TaskRoutes; authentication and body limits remain in the common dispatcher."""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import parse_qs

from umbrous.logging_setup import log_event
from umbrous.tasks.recover import resume_info
from umbrous.reset import ResetError
from umbrous.api.contracts import CHAT_PATH, TASK_ID_RE, RESET_STATUS

log = logging.getLogger("umbrous.api")


class TaskRoutes:
    def _tasks(self, path: str) -> Tuple[int, str]:
        """Task state, for the report and the trace (M4). Read-only: tasks are
        removed only by `bin/umbrous purge tasks`."""
        rt = self.runtime
        if self.command != "GET":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        if path == "/api/tasks":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            chat_id = (q.get("chat_id") or [""])[0]
            if not (chat_id and CHAT_PATH.match("/api/chats/" + chat_id)):
                self._send_json(400, {"error": "give chat_id"})
                return 400, "bad_query"
            listed = rt.tasks.for_chat(chat_id)
            for t in listed:     # M5: say whether an interrupted task can be resumed
                if t["status"] == "unknown":
                    t["resume"] = resume_info(rt.tasks, rt.history, t["task_id"])
            self._send_json(200, {"tasks": listed})
            return 200, "tasks"
        task_id = path[len("/api/tasks/"):]
        task = rt.tasks.get(task_id) if TASK_ID_RE.match(task_id) else None
        if task is None:
            self._send_json(404, {"error": "no_such_task"})
            return 404, "no_such_task"
        task["resume"] = resume_info(rt.tasks, rt.history, task_id, task)
        self._send_json(200, task)
        return 200, "task"

    def _reset(self, path: str) -> Tuple[int, str]:
        """M8: preview a selective reset, or confirm exactly a preview."""
        rt = self.runtime
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        try:
            if path == "/api/reset/preview":
                out = rt.reset.preview(body.get("selection"), lambda cid: rt.chat_locks.get(cid).locked())
                self._send_json(200, out)
                return 200, "reset_preview"
            if path == "/api/delete":
                out = rt.reset.delete_target(body.get("kind"), body.get("id"), rt.chat_locks, rt.runs)
            else:
                out = rt.reset.confirm(body.get("selection"), str(body.get("preview_id") or ""), rt.chat_locks)
        except ResetError as exc:
            status = RESET_STATUS.get(exc.code, 400)
            payload = {"error": exc.code, "message": exc.message}
            if exc.preview is not None:
                payload["preview"] = exc.preview
            self._send_json(status, payload)
            return status, exc.code
        # ids and counts only, never titles or memory text
        log_event(log, "reset.done", **{k: v for k, v in out["deleted"].items()},
                  ids=[i["id"] for i in out["items"]][:50])
        self._send_json(200, out)
        return 200, "reset_done"
