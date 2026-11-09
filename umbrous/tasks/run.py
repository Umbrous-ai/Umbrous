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

# A citation in the answer: name:12 or name:12-30, where the name may have
# folders, need not have an extension, and may start with a digit
# (README:1, src/Makefile:3, migrations/001_init.sql:3). The
# "name line 12" form counts only for path-like names (a "." or "/") or
# well-known extensionless files, so "in line 3" is not taken for a file.
CITE_RE = re.compile(r"(?<![\w/.:@-])((?:\.?/)?(?:[\w.-]+/)*\w[\w.-]*)"
                     r"(:| lines? )(\d{1,7})(?:\s*[-–]\s*(\d{1,7}))?(?::\d+)?(?!\d)")   # a :column is ignored
BARE_NAMES = {"readme", "makefile", "dockerfile", "license", "licence", "changelog", "notice", "authors",
              "contributing", "codeowners", "procfile", "gemfile", "rakefile", "vagrantfile", "jenkinsfile",
              "brewfile", "justfile", "copying", "install", "todo"}
NOT_FILES = {"localhost"}       # host:port is not a citation


def citations(answer: str) -> List[Tuple[str, int, int]]:
    """(path, first line, last line) for each file citation in the answer, at most 40."""
    out = []
    for m in CITE_RE.finditer(answer or ""):
        name, sep, a, b = m.group(1), m.group(2), int(m.group(3)), int(m.group(4) or m.group(3))
        path = name[2:] if name.startswith("./") else name.lstrip("/")
        base = path.rsplit("/", 1)[-1]
        if base.lower() in NOT_FILES:
            continue
        if "/" not in path and re.fullmatch(r"[\d.]+", base):
            continue        # a time (10:30), a ratio (3:1) or a version, not a file; 001_init.sql is kept
        if sep != ":" and "." not in base and "/" not in path and base.lower() not in BARE_NAMES:
            continue
        out.append((path, a, b))
        if len(out) >= 40:
            break
    return out
MAX_LIST = 20


class TaskRun:
    def __init__(self, store: Optional[TaskStore], task_id: str, project_id: Optional[str], chat_id: Optional[str],
                 model: str, limits: Dict[str, int], workspace: Optional[str] = None) -> None:
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
        # Provenance for Verify: lines a completed read_file returned, and lines
        # that only appeared in search results (each cut to 200 characters).
        self.read_ranges: Dict[str, List[Tuple[int, int]]] = {}
        self.search_lines: Dict[str, set] = {}
        self.citations_checked = 0
        # M5: set when this task resumes an interrupted one: its id, and the
        # hash of each file the interrupted task read (from its receipts).
        self.resume_of: Optional[str] = None
        self.previous_reads: Dict[str, str] = {}
        # M7: what Observe + Verify suggested for memory (ids, stores, kinds,
        # counts dropped; never the text). Set before finish().
        self.memory_suggestions: Optional[dict] = None
        # M9: what the task changed and ran, from the tools' own results (never the model's text).
        self.workspace = workspace
        self.changes: List[dict] = []
        self.commands: List[dict] = []
        self.notes_end: List[str] = []       # said after the checks below (self-source, stopped commands)
        self._safe(store.create if store else None, task_id, project_id, chat_id, model, self.limits, workspace)
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

    def intent(self, action_id: str, data: dict) -> None:
        """M9: record what a change or command is about to do, BEFORE it is done.
        Not `_safe`: if this cannot be recorded, the tool must not act (it raises)."""
        if self.store is None:
            return
        kind = "command.intent" if data.get("op") == "command" else "write.intent"
        self.store.event(self.task_id, kind, action_id, **data)

    def command_started(self, action_id: str, data: dict) -> None:
        self._safe(self.store and self.store.event, self.task_id, "command.started", action_id, **data)

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
            o9 = observed or {}
            if o9.get("op") and o9.get("path"):
                if len(self.changes) < 100:
                    self.changes.append({"action_id": action_id, "path": o9["path"], "op": o9["op"],
                                         "before": o9.get("before"), "after": o9.get("after"),
                                         "added": o9.get("added", 0), "removed": o9.get("removed", 0),
                                         "step": a["step"],
                                         **({"parses": False} if (o9.get("check") or {}).get("ok") is False
                                            else {})})
            elif o9.get("command"):
                if len(self.commands) < 100:
                    self.commands.append({"action_id": action_id, "command": o9["command"], "status": o9.get("status"),
                                          "exit": o9.get("exit"), "ms": o9.get("ms"), "cmd_id": o9.get("cmd_id"),
                                          "check": bool(o9.get("check")), "step": a["step"],
                                          "after_change": len(self.changes)})
            self.evidence.extend(evidence)
            for e in evidence:
                if e.get("kind") != "file":
                    continue
                lines = tuple(e["lines"]) if e.get("lines") else (0, 0)
                if a["tool"] == "read_file":
                    self.read_ranges.setdefault(e["ref"], []).append(lines)
                elif a["tool"] == "search_files":
                    self.search_lines.setdefault(e["ref"], set()).add(lines[0])
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
        """Check the answer's file:line citations against what completed reads
        returned. Only a read counts: a search shows a line cut to 200
        characters, so a line seen only in search results is reported as such.
        Only the outcome is kept (the cited path and line), never the answer."""
        cites = citations(answer)
        self.citations_checked = len(cites)
        for path, a, b in cites:
            where = f"{path}:{a}" + (f"-{b}" if b != a else "")
            if path in self.read_ranges:
                if not any(lo <= a and b <= hi for lo, hi in self.read_ranges[path]):
                    self.note(f"The answer cites {where}, outside the lines read.")
            elif path in self.search_lines:
                self.note(f"The answer cites {where}, which was only seen in search results "
                          "(lines cut to 200 characters), not read.")
            else:
                self.note(f"The answer cites {path}, which was not read in this task.")

    def compare_with_previous(self) -> None:
        """M5: a resumed task read the files again. Say which ones changed
        since the interrupted attempt read them, and which did not."""
        if not self.resume_of:
            return
        self.note("This task resumed an interrupted one. Its reads were repeated under the current settings, "
                  "with new receipts; nothing from the interrupted attempt was reused.")
        now = {e["ref"]: e.get("sha256") for e in self.evidence if e.get("kind") == "file" and e.get("sha256")}
        for path, old in list(self.previous_reads.items())[:10]:
            if path not in now:
                continue
            if now[path] != old:
                self.note(f"{path} changed since the interrupted attempt read it.")
            else:
                self.note(f"{path} is unchanged since the interrupted attempt read it.")

    def check_work(self) -> None:
        """M9: what the record says about the work, so the report never takes the
        model's word for it: a command whose last run failed, changes made after
        the last command, changes never checked by any command."""
        real = [c for c in self.commands if c["status"] in ("exited", "timed_out", "stopped")]
        last: Dict[str, dict] = {}
        for c in real:
            last[c["command"]] = c
        for cmd, c in list(last.items())[:10]:
            if c["status"] == "exited" and c["exit"] not in (0, None):
                self.note(f"The last run of `{cmd[:80]}` exited with status {c['exit']}.")
            elif c["status"] == "timed_out":
                self.note(f"`{cmd[:80]}` did not finish within its time limit and was stopped.")
        changed = [x for x in self.changes if x["op"] in ("created", "replaced", "edited")]
        if changed and not real:
            self.note(f"{len(changed)} file change(s) were made, and no command was run to check them.")
        elif changed and real and max(c["after_change"] for c in real) < len(self.changes):
            later = len(self.changes) - max(c["after_change"] for c in real)
            self.note(f"{later} change(s) were made after the last command, so no command checked them.")
        for n in self.notes_end:
            self.note(n)

    def finish(self, status: str, code: Optional[str], answer: str = "") -> dict:
        self.compare_with_previous()
        self.check_work()
        if status == "completed":
            self.verify(answer)
        report = {
            "task_id": self.task_id, "status": status, "code": code, "steps": self.step,
            "tool_calls": self.tool_calls, "actions": self.counts(), "evidence": _dedupe(self.evidence),
            "failures": self.failures, "uncertainty": self.uncertainty, "limits_hit": self.limits_hit,
            "limits": self.limits, "ms": self.ms(), "citations_checked": self.citations_checked,
        }
        if self.resume_of:
            report["resume_of"] = self.resume_of
        if self.workspace:
            report["workspace"] = self.workspace
        if self.changes or self.commands:
            report["changes"] = [{k: c[k] for k in ("path", "op", "before", "after", "added", "removed")}
                                 for c in self.changes][:50]
            report["commands"] = [{k: c[k] for k in ("command", "status", "exit", "ms", "check")}
                                  for c in self.commands][:50]
        if self.memory_suggestions is not None:
            report["memory_candidates"] = self.memory_suggestions
            self._safe(self.store and self.store.event, self.task_id, "memory.suggested",
                       queued=len(self.memory_suggestions.get("queued", [])),
                       dropped=sum(self.memory_suggestions.get("dropped", {}).values()))
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
