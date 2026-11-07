"""Recording one task while it runs, and its final report (M4).

The run loop calls a `TaskRun` at each point of the task: a model step starts
or ends, an action is requested, starts, ends, a limit is reached. Each call
appends to the task's event log; a checkpoint is written after every step.
At the end, Observe + Verify turns what was recorded into the report:

  evidence      references from actions that completed (a file with its hash
                and the lines read, a folder listed, a search with the words
                hashed and the lines it matched). The model's text is never
                evidence, and neither is a denied, failed or unknown action.
  failures      every action that did not complete, with its code and reason.
  uncertainty   anything that makes the answer less than fully checked: a
                search that hit its time or size limit or skipped files, a
                partial read, a result cut to fit the context, an action
                whose outcome is unknown, a limit reached, and file:line
                citations in the answer that no completed read covers.

Recording must never break the reply: if the store fails, the error is logged
(ids only) and the run continues.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from umbrous.logging_setup import log_event
from umbrous.tasks.store import TaskStore

log = logging.getLogger("umbrous.tasks")

# A citation in the answer: path/with.ext:12 or :12-30 (also "path line 12").
CITE_RE = re.compile(r"(?<![\w/.-])([\w][\w./-]*\.[A-Za-z0-9]{1,8})(?::| lines? )(\d{1,7})(?:\s*[-–]\s*(\d{1,7}))?")
MAX_LIST = 20


class TaskRun:
    def __init__(self, store: Optional[TaskStore], task_id: str, project_id: Optional[str], chat_id: Optional[str],
                 model: str, limits: Dict[str, int]) -> None:
        self.store = store
        self.task_id = task_id
        self.limits = dict(limits)
        self.started = time.monotonic()
        self.step = 0
        self.tool_calls = 0
        self.actions: Dict[str, dict] = {}          # action_id -> {tool, resource, status, code, step}
        self.evidence: List[dict] = []
        self.failures: List[dict] = []
        self.uncertainty: List[str] = []
        self.limits_hit: List[str] = []
        self._safe(store.create if store else None, task_id, project_id, chat_id, model, self.limits)
        self._safe(store.set_status if store else None, task_id, "started")

    def _safe(self, fn, *args, **kw) -> None:
        if fn is None:
            return
        try:
            fn(*args, **kw)
        except Exception as exc:  # a recording failure never stops the reply
            log_event(log, "task.record_failed", level=logging.WARNING, task_id=self.task_id,
                      error=exc.__class__.__name__)

    def note(self, text: str) -> None:
        if text not in self.uncertainty and len(self.uncertainty) < MAX_LIST:
            self.uncertainty.append(text)

    # ---------------------------------------------------------------- steps
    def step_started(self, offered_tools: bool) -> None:
        self.step += 1
        self._safe(self.store and self.store.event, self.task_id, "step.started", step=self.step,
                   tools_offered=offered_tools)

    def step_ended(self, calls: int, reply_chars: int, done_reason: Optional[str] = None) -> None:
        self._safe(self.store and self.store.event, self.task_id, "step.completed", step=self.step, tool_calls=calls,
                   reply_chars=reply_chars, done_reason=done_reason)

    def checkpoint(self, used_tokens: int) -> None:
        counts = self.counts()
        self._safe(self.store and self.store.checkpoint, self.task_id, self.step, {
            "step": self.step, "tool_calls": self.tool_calls, "actions": counts,
            "evidence": len(self.evidence), "used_tokens": used_tokens, "limits_hit": list(self.limits_hit),
            "ms": self.ms()})

    def limit(self, name: str, detail: str) -> None:
        if name not in self.limits_hit:
            self.limits_hit.append(name)
            self.note(detail)
            self._safe(self.store and self.store.event, self.task_id, "limit", limit=name)

    # ---------------------------------------------------------------- actions
    def action_requested(self, action_id: str, tool: object, args: object) -> None:
        self.tool_calls += 1
        tool_s = tool if isinstance(tool, str) else "?"
        path = args.get("path") if isinstance(args, dict) else None
        resource = path if isinstance(path, str) and path else "."
        self.actions[action_id] = {"tool": tool_s, "resource": resource, "status": "requested", "step": self.step}
        self._safe(self.store and self.store.add_action, self.task_id, action_id, self.step, tool_s, resource)

    def action_started(self, action_id: str) -> None:
        self.actions[action_id]["status"] = "started"
        self._safe(self.store and self.store.action_status, action_id, "started")

    def action_ended(self, action_id: str, status: str, code: str, message: str,
                     receipt_id: Optional[str] = None, observed: Optional[dict] = None,
                     evidence: Tuple[dict, ...] = (), cut: bool = False) -> None:
        a = self.actions[action_id]
        a.update(status=status, code=code)
        data = {k: v for k, v in (observed or {}).items() if v}
        if cut:
            data["cut_to_fit"] = True
        self._safe(self.store and self.store.action_status, action_id, status, code, receipt_id, **data)
        where = f"{a['tool']} {a['resource']}"
        place = "the workspace folder" if a["resource"] == "." else a["resource"]
        if status == "completed":
            self.evidence.extend(evidence)
            o = observed or {}
            if o.get("timed_out"):
                self.note(f"The search in {place} hit its time limit, so it may have missed matches.")
            elif o.get("truncated"):
                self.note(f"{where} was cut off at its size limit, so results are incomplete.")
            if o.get("skipped"):
                self.note(f"The search in {place} skipped {o['skipped']} file(s) it could not read "
                          "(binary, too large, links or excluded).")
            if o.get("partial"):
                self.note(f"Only part of {place} was read.")
            if cut:
                self.note(f"Part of the result of {where} was left out to fit the context budget.")
        else:
            if len(self.failures) < MAX_LIST:
                self.failures.append({"action_id": action_id, "tool": a["tool"], "resource": a["resource"],
                                      "status": status, "code": code, "message": message})
            if status == "unknown":
                self.note(f"{where} did not report back in time; its outcome is unknown.")

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for a in self.actions.values():
            out[a["status"]] = out.get(a["status"], 0) + 1
        return out

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    # ---------------------------------------------------------------- verify + finish
    def verify(self, answer: str) -> None:
        """Check the answer's file:line citations against the reads that completed.
        Only the outcome is kept (the cited path and line), never the answer."""
        ranges: Dict[str, List[Tuple[int, int]]] = {}
        for e in self.evidence:
            if e.get("kind") == "file":
                lines = e.get("lines")
                ranges.setdefault(e["ref"], []).append(tuple(lines) if lines else (0, 0))
        checked = 0
        for m in CITE_RE.finditer(answer or ""):
            if checked >= 40:
                break
            checked += 1
            path, a, b = m.group(1).lstrip("./"), int(m.group(2)), int(m.group(3) or m.group(2))
            if path not in ranges:
                self.note(f"The answer cites {path}, which was not read in this task.")
            elif not any(lo <= a and b <= hi for lo, hi in ranges[path]):
                self.note(f"The answer cites {path}:{a}" + (f"-{b}" if b != a else "") + ", outside the lines read.")

    def finish(self, status: str, code: Optional[str], answer: str = "") -> dict:
        if status == "completed":
            self.verify(answer)
        report = {
            "task_id": self.task_id, "status": status, "code": code, "steps": self.step,
            "tool_calls": self.tool_calls, "actions": self.counts(), "evidence": _dedupe(self.evidence),
            "failures": self.failures, "uncertainty": self.uncertainty, "limits_hit": self.limits_hit,
            "limits": self.limits, "ms": self.ms(),
        }
        self._safe(self.store and self.store.artifact, self.task_id, "report", report)
        self._safe(self.store and self.store.set_status, self.task_id, status, code,
                   steps=self.step, tool_calls=self.tool_calls)
        log_event(log, "task.end", task_id=self.task_id, status=status, code=code, steps=self.step,
                  tool_calls=self.tool_calls, ms=report["ms"])
        return report


def _dedupe(evidence: list) -> list:
    seen, out = set(), []
    for e in evidence:
        key = (e["kind"], e["ref"], tuple(e["lines"]) if e.get("lines") else None)
        if key not in seen:
            seen.add(key)
            out.append({k: e.get(k) for k in ("kind", "ref", "sha256", "lines") if e.get(k) is not None})
    return out[:50]
