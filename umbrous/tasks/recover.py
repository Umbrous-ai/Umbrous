"""Recover and resume (M5).

When the runtime stops in the middle of a task (a crash, a forced quit, a
restart, the Mac sleeping the process to death), the task's record says it was
started and nothing more. At the next start, `recover()` runs once, before any
request is served, and turns each such task into an honest record:

  action with a receipt      the receipt is what happened (the gate writes it
                             after the tool returns), so the action takes the
                             receipt's outcome: allowed -> completed, denied,
                             failed, uncertain -> unknown.
  started, no receipt        the tool was running and no result was recorded:
                             unknown. Not failed: it may have done its work.
  requested, no receipt      it never ran (it was waiting for its turn or for
                             an approval): cancelled, code interrupted.
  the task                   unknown, code interrupted. If you had pressed Stop
                             (a stop.requested event), cancelled instead, and
                             it is not offered for resuming.

The task gets a report rebuilt from the record (receipts give the evidence),
which says plainly that the runtime stopped mid-task.

Unknown is not failed. A failed action is known not to have done its work. An
unknown one may or may not have: only checking its effect can say. For the
read-only tools that check is unnecessary (reading again changes nothing), so
their retry class is "repeat". A future write tool would need a way to check
its effect before it could be resumed (docs/m5-resume.md).

Resume runs the task again as a new task, from the message that was waiting
(pending_turns), under the gate, profile, grants and approvals as they are
NOW. Nothing is replayed: not the old tool results, not old approvals. Before
it starts, the old task's actions are checked against the current scope; if a
read it made would now be blocked (a revoked grant, a detached workspace, a
stricter profile with no way to ask), the resume is refused and the task stays
interrupted.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from umbrous.gate.gate import ALLOW, ASK, TOOLS, Call, Gate
from umbrous.history.store import HistoryStore
from umbrous.logging_setup import log_event
from umbrous.receipts.store import ReceiptStore
from umbrous.tasks.store import OPEN, TaskStore

log = logging.getLogger("umbrous.tasks")

RECEIPT_TO_ACTION = {"allowed": "completed", "denied": "denied", "failed": "failed", "uncertain": "unknown"}
INTERRUPTED = "interrupted"
STOPPED_NOTE = ("Umbrous stopped while this task was running (a crash, a forced quit or a restart). What was "
                "recorded before that is kept; nothing after it is known.")
RESUME_WARNING = ("Resuming runs the task again under the current settings. Reads are repeated with new receipts, "
                  "and the files may have changed since the first attempt. Sending a new message here instead "
                  "discards the interrupted one.")
REASONS = {
    "not_interrupted": "Only a task that was interrupted can be resumed.",
    "already_resumed": "This task was already resumed.",
    "reply_saved": "The reply was saved before Umbrous stopped, so there is nothing to resume.",
    "message_gone": "The message of this task is no longer kept (a newer message replaced it, or it was purged).",
    "needs_verification": "An action of this task has an unknown outcome and cannot simply be repeated; its "
                          "effect must be checked first.",
}


def retry_class(tool: str) -> str:
    """repeat | verify. A tool without a declared class is treated as verify."""
    return (TOOLS.get(tool) or {}).get("retry", "verify")


def _ms_between(a: Optional[str], b: Optional[str]) -> int:
    try:
        return max(0, int((datetime.fromisoformat(b.replace("Z", "+00:00")) -
                           datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() * 1000))
    except (AttributeError, ValueError):
        return 0


def recover(tasks: TaskStore, receipts: ReceiptStore, history: HistoryStore) -> List[dict]:
    """Close every task left open by a stop mid-task. Run once at startup,
    before serving (runtime.json ensures one runtime per home)."""
    out = []
    for task_id in tasks.open_tasks():
        try:
            out.append(_recover_one(tasks, receipts, history, task_id))
        except Exception as exc:   # one bad record must not stop the runtime from starting
            log_event(log, "task.recover_failed", level=logging.WARNING, task_id=task_id,
                      error=exc.__class__.__name__)
    if out:
        log_event(log, "task.recovered", count=len(out),
                  interrupted=sum(1 for r in out if r["status"] == "unknown"))
    return out


def _recover_one(tasks: TaskStore, receipts: ReceiptStore, history: HistoryStore, task_id: str) -> dict:
    full = tasks.get(task_id)
    by_action: Dict[str, dict] = {r["action_id"]: r for r in receipts.for_task(task_id)}
    events = full["events"]
    stop = next((e for e in events if e["kind"] == "stop.requested"), None)

    for a in full["actions"]:
        if a["status"] not in OPEN:
            continue
        r = by_action.get(a["action_id"])
        if r is not None:
            code = (r.get("reason") or "").split(":", 1)[0] or None
            tasks.recover_action(a["action_id"], RECEIPT_TO_ACTION[r["decision"]], code, r["receipt_id"],
                                 reconciled_from="receipt")
        elif a["status"] == "started":
            tasks.recover_action(a["action_id"], "unknown", INTERRUPTED, None, retry=retry_class(a["tool"]))
        else:
            tasks.recover_action(a["action_id"], "cancelled", INTERRUPTED, None)

    full = tasks.get(task_id)
    reply_saved = history.has_reply(task_id)
    if stop is not None:
        status = "cancelled"
        code = "task_time_limit" if (stop["data"] or {}).get("by") == "time_limit" else "cancelled"
        history.drop_pending(task_id)    # stopped with no reply kept: the message is not kept (M2)
    else:
        status, code = "unknown", INTERRUPTED

    report = rebuild_report(full, by_action, status, code, reply_saved)
    tasks.artifact(task_id, "report", report)
    tasks.recover_task(task_id, status, code, steps=report["steps"], tool_calls=report["tool_calls"])
    return {"task_id": task_id, "status": status, "code": code, "reply_saved": reply_saved}


def rebuild_report(full: dict, by_action: Dict[str, dict], status: str, code: str, reply_saved: bool) -> dict:
    """The M4 report keys, rebuilt from the stored record instead of a live run."""
    task, actions, events = full["task"], full["actions"], full["events"]
    counts: Dict[str, int] = {}
    evidence, seen, failures = [], set(), []
    uncertainty = [STOPPED_NOTE]
    for a in actions:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
        where = f"{a['tool']} {a['resource']}"
        if a["status"] == "completed":
            for e in (by_action.get(a["action_id"]) or {}).get("evidence") or []:
                key = (e["kind"], e["ref"], tuple(e["lines"]) if e.get("lines") else None)
                if key not in seen:
                    seen.add(key)
                    evidence.append({k: e.get(k) for k in ("kind", "ref", "sha256", "lines") if e.get(k) is not None})
            continue
        if len(failures) < 20:
            failures.append({"action_id": a["action_id"], "tool": a["tool"], "resource": a["resource"],
                             "status": a["status"], "code": a["code"],
                             "message": ("Running when Umbrous stopped; no result was recorded."
                                         if a["status"] == "unknown" and a["code"] == INTERRUPTED else
                                         "Not run: Umbrous stopped first." if a["code"] == INTERRUPTED else
                                         (by_action.get(a["action_id"]) or {}).get("reason") or a["code"])})
        if a["status"] == "unknown":
            uncertainty.append(f"{where} was running when Umbrous stopped and no result was recorded, so its "
                               f"outcome is unknown (retry class: {retry_class(a['tool'])}).")
    if reply_saved:
        uncertainty.append("The reply was saved before Umbrous stopped, but the task's own report was not; "
                           "this report was rebuilt from the record.")
    elif status == "unknown":
        uncertainty.append("Reply text that had arrived before the stop was not saved.")
    steps = max([e["data"].get("step", 0) for e in events if e["kind"] == "step.started"] or [0])
    limits_hit = [e["data"].get("limit") for e in events if e["kind"] == "limit" and e["data"].get("limit")]
    return {
        "task_id": task["task_id"], "status": status, "code": code, "steps": steps, "tool_calls": len(actions),
        "actions": counts, "evidence": evidence[:50], "failures": failures, "uncertainty": uncertainty[:20],
        "limits_hit": limits_hit, "limits": task["limits"],
        "ms": _ms_between(task["created_at"], events[-1]["at"] if events else None),
        "citations_checked": 0, "recovered": True,
    }


# ---------------------------------------------------------------- resuming
def resume_info(tasks: TaskStore, history: HistoryStore, task_id: str, full: Optional[dict] = None) -> dict:
    """Whether a task can be resumed, from the record alone. The current scope
    is checked again when you resume (`scope_check`)."""
    full = full or tasks.get(task_id)
    if full is None:
        return {"possible": False, "reason": "no_such_task", "message": "That task does not exist."}
    t = full["task"]

    def no(reason: str, **extra) -> dict:
        return dict({"possible": False, "reason": reason, "message": REASONS[reason]}, **extra)

    if t["status"] != "unknown" or t["code"] != INTERRUPTED:
        return no("not_interrupted")
    resumed = next((e for e in full["events"] if e["kind"] == "task.resumed"), None)
    if resumed:
        return no("already_resumed", resumed_by=resumed["data"].get("by"))
    pending = history.pending(task_id)
    if pending is None:
        return no("reply_saved" if history.has_reply(task_id) else "message_gone")
    unknown = [a for a in full["actions"] if a["status"] == "unknown"]
    if any(retry_class(a["tool"]) != "repeat" for a in unknown):
        return no("needs_verification")
    return {"possible": True, "reason": None, "message": RESUME_WARNING, "text": pending.user_text,
            "repeats": [{"tool": a["tool"], "resource": a["resource"], "status": a["status"]}
                        for a in full["actions"] if a["status"] in ("completed", "unknown")][:20]}


def scope_check(gate: Gate, project_id: str, full: dict, approvals: bool) -> Optional[dict]:
    """Would the reads the interrupted task made still be allowed now? None if
    yes; otherwise {code, message} saying why the resume is refused.

    Old approvals are never carried over: a read that now needs approval is
    asked again (when the app can ask), and refused when it cannot."""
    for a in full["actions"]:
        if a["status"] not in ("completed", "unknown"):
            continue
        args = {} if a["resource"] in (None, ".") else {"path": a["resource"]}
        if a["tool"] == "search_files":
            args["query"] = "resume-check"     # the words are not kept; only the folder matters here
        v = gate.evaluate(project_id, Call(a["tool"], args))
        if v.outcome == ALLOW:
            continue
        if v.outcome == ASK and approvals:
            continue      # asked again during the resume; the old answer is not reused
        if v.outcome == ASK:
            return {"code": "resume_needs_approval",
                    "message": f"{a['tool']} {a['resource']} now needs your approval, and this app cannot ask "
                               "for it. The task stays interrupted. Resume it from the browser page, or change "
                               "the project's access."}
        return {"code": "resume_scope_changed",
                "message": f"{a['tool']} {a['resource']} is no longer allowed ({v.reason}) The task stays "
                           "interrupted and is not replayed."}
    return None
