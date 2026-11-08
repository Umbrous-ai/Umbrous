"""MemoryRoutes; authentication and body limits remain in the common dispatcher."""

from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import parse_qs

from umbrous.logging_setup import log_event
from umbrous.history.store import HistoryError
from umbrous.memory.store import MemoryError_
from umbrous.api.contracts import PROJECT_ID_RE, MEMORY_PATH, CANDIDATE_PATH

log = logging.getLogger("umbrous.api")


class MemoryRoutes:
    def _memory(self, path: str) -> Tuple[int, str]:
        """The two memory stores (M6): inspect, add, edit, delete. Nothing here
        reads or changes chats; deleting an item removes only that item."""
        rt, cmd = self.runtime, self.command
        mem, hist = rt.memory, rt.history

        def exists(project_id: str) -> bool:
            try:
                hist.get_project(project_id)
                return True
            except HistoryError:
                return False

        if path == "/api/memory" and cmd == "GET":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (q.get("project_id") or [""])[0]
            if project_id and not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            personal = []
            for m in mem.personal.list():
                d = m.as_dict()
                d["applies"] = m.scope == "all" or (bool(project_id) and project_id in m.scope)
                personal.append(d)
            out = {"personal": personal, "project_id": project_id or None, "project": [],
                   "limits": {"max_items": rt.config.memory_max_items, "max_tokens": rt.config.memory_tokens}}
            if project_id:
                hist.get_project(project_id)
                out["project"] = [m.as_dict() for m in mem.project.list(project_id)]
                out["candidates"] = [c.as_dict() for c in rt.review.list(project_id)]   # M7: waiting for you
            else:
                out["candidates"] = []
            self._send_json(200, out)
            return 200, "memory"
        if path == "/api/memory/candidates" and cmd == "GET":
            q = parse_qs(self.path.split("?", 1)[1].split("#", 1)[0] if "?" in self.path else "")
            project_id = (q.get("project_id") or [""])[0] or None
            status = (q.get("status") or ["pending"])[0]
            if project_id and not PROJECT_ID_RE.match(project_id):
                self._send_json(400, {"error": "bad_project_id"})
                return 400, "bad_project_id"
            if status not in ("pending", "accepted", "rejected", "all"):
                self._send_json(400, {"error": "bad_status"})
                return 400, "bad_status"
            items = rt.review.list(project_id, None if status == "all" else status)
            self._send_json(200, {"candidates": [c.as_dict() for c in items]})
            return 200, "candidates"
        if cmd != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if path == "/api/memory":
            store = body.get("store")
            if store == "personal":
                item = mem.personal.add(body.get("text"), body.get("kind") or "fact", body.get("scope", "all"),
                                        project_exists=exists)
            elif store == "project":
                project_id = str(body.get("project_id") or "")
                if not PROJECT_ID_RE.match(project_id):
                    self._send_json(400, {"error": "bad_project_id"})
                    return 400, "bad_project_id"
                item = mem.project.add(project_id, body.get("text"), body.get("kind") or "decision")
            else:
                self._send_json(400, {"error": "bad_store", "message": 'store is "personal" or "project"'})
                return 400, "bad_store"
            log_event(log, "memory.added", memory_id=item.id, store=item.store, kind=item.kind)  # never the text
            self._send_json(201, {"item": item.as_dict()})
            return 201, "memory_added"
        cm = CANDIDATE_PATH.match(path)
        if cm:
            return self._candidate(cm.group(1), (cm.group(2) or "").lstrip("/"), body, exists)
        m = MEMORY_PATH.match(path)
        if not m:
            self._send_json(404, {"error": "not_found"})
            return 404, "not_found"
        item_id, delete = m.group(1), bool(m.group(2))
        item = mem.get(item_id)
        if item is None:
            raise MemoryError_("That memory item does not exist.", "no_such_memory")
        if delete:
            mem.delete(item_id)
            log_event(log, "memory.deleted", memory_id=item_id, store=item.store)
            self._send_json(200, {"deleted": item_id, "store": item.store})
            return 200, "memory_deleted"
        if item.store == "personal":
            item = mem.personal.update(item_id, body.get("text"), body.get("kind"), body.get("scope"),
                                       project_exists=exists)
        else:
            if "scope" in body or "project_id" in body:
                raise MemoryError_("A project item stays in its project. Delete it and add it to the other "
                                   "project instead.", "bad_scope")
            item = mem.project.update(item_id, body.get("text"), body.get("kind"))
        log_event(log, "memory.edited", memory_id=item_id, store=item.store)
        self._send_json(200, {"item": item.as_dict()})
        return 200, "memory_edited"

    def _candidate(self, cand_id: str, action: str, body: dict, exists) -> Tuple[int, str]:
        """M7: review one suggestion. Accept is the only way a suggestion
        reaches memory, and it is checked again first."""
        rev = self.runtime.review
        edits = {k: body.get(k) for k in ("text", "kind", "store", "scope")}
        if action == "reject":
            cand = rev.reject(cand_id)
            log_event(log, "memory.candidate_rejected", candidate_id=cand_id)   # never the text
            self._send_json(200, {"candidate": cand.as_dict()})
            return 200, "candidate_rejected"
        if action == "accept":
            cand, item = rev.accept(cand_id, project_exists=exists, **edits)
            log_event(log, "memory.candidate_accepted", candidate_id=cand_id, memory_id=item.id, store=item.store,
                      kind=item.kind, edited=cand.edited)
            self._send_json(200, {"candidate": cand.as_dict(), "item": item.as_dict()})
            return 200, "candidate_accepted"
        cand = rev.edit(cand_id, project_exists=exists, **edits)
        log_event(log, "memory.candidate_edited", candidate_id=cand_id)
        self._send_json(200, {"candidate": cand.as_dict()})
        return 200, "candidate_edited"
