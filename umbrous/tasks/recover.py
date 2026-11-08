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

M9 adds tools that change things, each with its own check:

  write_file / edit_file   retry class "verify". Before writing, the change
                           recorded a `write.intent` event: the path, the
                           file's hash before (or absent) and the hash it was
                           about to have. Recovery reads the file now: the
                           "after" hash means it happened (completed); the
                           "before" hash (or still absent) means it did not
                           (cancelled); anything else means something else
                           changed it (unknown, code conflict: shown to you,
                           never retried blindly). No intent means nothing
                           was written yet. A leftover temporary file is removed.
  run_command              retry class "command". Its effects cannot be
                           checked afterwards: it stays unknown, is never run
                           again by itself, and resuming says so. A command
                           still running from the dead runtime (its process
                           group is still there) is stopped.

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
from umbrous.tools import edit
from umbrous.tools.run import kill_group, process_alive, same_process

WRITE_TOOLS = ("write_file", "edit_file")
COMMAND_TOOLS = ("run_command", "check_command")

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
CODE_RESUME_WARNING = ("Resuming runs the task again under the current settings, in the same folder. Changes that "
                       "were made stay as they are and the model is told about them, so they are not made twice. "
                       "Commands are not re-run by themselves.")


def retry_class(tool: str) -> str:
    """repeat | verify. A tool without a declared class is treated as verify."""
    return (TOOLS.get(tool) or {}).get("retry", "verify")


def _ms_between(a: Optional[str], b: Optional[str]) -> int:
    try:
        return max(0, int((datetime.fromisoformat(b.replace("Z", "+00:00")) -
                           datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() * 1000))
    except (AttributeError, ValueError):
        return 0


def recover(tasks: TaskStore, receipts: ReceiptStore, history: HistoryStore,
            workspace_of=lambda project_id: None) -> List[dict]:
    """Close every task left open by a stop mid-task. Run once at startup,
    before serving (runtime.json ensures one runtime per home).
    `workspace_of(project_id)` (M9) gives the project's folder now, for tasks
    that did not record their own."""
    out = []
    for task_id in tasks.open_tasks():
        try:
            out.append(_recover_one(tasks, receipts, history, task_id, workspace_of))
        except Exception as exc:   # one bad record must not stop the runtime from starting
            log_event(log, "task.recover_failed", level=logging.WARNING, task_id=task_id,
                      error=exc.__class__.__name__)
    if out:
        log_event(log, "task.recovered", count=len(out),
                  interrupted=sum(1 for r in out if r["status"] == "unknown"))
    return out


def _recover_one(tasks: TaskStore, receipts: ReceiptStore, history: HistoryStore, task_id: str,
                 workspace_of=lambda project_id: None) -> dict:
    full = tasks.get(task_id)
    by_action: Dict[str, dict] = {r["action_id"]: r for r in receipts.for_task(task_id)}
    events = full["events"]
    stop = next((e for e in events if e["kind"] == "stop.requested"), None)
    root = full["task"].get("workspace") or workspace_of(full["task"]["project_id"])

    for a in full["actions"]:
        r = by_action.get(a["action_id"])
        if a["tool"] in COMMAND_TOOLS:
            _stop_leftover(tasks, task_id, a, events)     # even if it ended: a background one may still run
        if a["status"] not in OPEN:
            continue
        if r is not None:
            code = (r.get("reason") or "").split(":", 1)[0] or None
            tasks.recover_action(a["action_id"], RECEIPT_TO_ACTION[r["decision"]], code, r["receipt_id"],
                                 reconciled_from="receipt")
        elif a["status"] == "started" and a["tool"] in WRITE_TOOLS:
            _reconcile_write(tasks, a, events, root)
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


def _intent(events: list, action_id: str, kind: str = "write.intent") -> Optional[dict]:
    return next((e["data"] for e in events if e["kind"] == kind and e["action_id"] == action_id), None)


def _reconcile_write(tasks: TaskStore, a: dict, events: list, root: Optional[str]) -> None:
    """M9: settle a change that was running when the runtime stopped, from the disk."""
    intent = _intent(events, a["action_id"])
    if intent is None:
        # the intent is recorded before anything is written: without it, nothing was
        tasks.recover_action(a["action_id"], "cancelled", INTERRUPTED, None, reconciled_from="disk",
                             outcome="not_done", retry="verify")
        return
    if root is None:
        tasks.recover_action(a["action_id"], "unknown", INTERRUPTED, None, reconciled_from="disk",
                             outcome="unchecked", retry="verify", path=intent.get("path"))
        return
    if intent.get("tmp"):
        edit.remove_tmp(root, intent["tmp"])
    state, now = edit.current_hash(root, intent.get("path") or "")
    before, after = intent.get("before"), intent.get("after")
    if state == "present" and now == after:
        tasks.recover_action(a["action_id"], "completed", "ok", None, reconciled_from="disk", outcome="done",
                             path=intent["path"], op=intent.get("op"), before=before, after=after)
    elif (state == "present" and now == before) or (state == "absent" and before is None):
        tasks.recover_action(a["action_id"], "cancelled", INTERRUPTED, None, reconciled_from="disk",
                             outcome="not_done", path=intent["path"], before=before, after=after)
    else:
        tasks.recover_action(a["action_id"], "unknown", "conflict", None, reconciled_from="disk", outcome="conflict",
                             path=intent.get("path"), before=before, after=after, now=now, state=state)


def _stop_leftover(tasks: TaskStore, task_id: str, a: dict, events: list) -> None:
    """M9: a command the dead runtime started may still be running (its own
    process group). It belonged to an ended task: stop it, and say so."""
    started = _intent(events, a["action_id"], "command.started") or {}
    pid = started.get("pid")
    if not (isinstance(pid, int) and pid > 1):
        return
    if not process_alive(pid):
        # usual: its supervisor stopped the group when the runtime went away
        tasks.event(task_id, "command.gone_after_restart", a["action_id"], pid=pid)
        return
    # The id alone proves nothing: after a restart (or a reboot) it may belong to
    # an unrelated process. Signal it only when it is still the very process that
    # was started: same boot, same start time, still leading its own group.
    if same_process(pid, started.get("identity")):
        kill_group(pid)
        tasks.event(task_id, "command.stopped_after_restart", a["action_id"], pid=pid)
    else:
        tasks.event(task_id, "command.not_signalled", a["action_id"], pid=pid)


def rebuild_report(full: dict, by_action: Dict[str, dict], status: str, code: str, reply_saved: bool) -> dict:
    """The M4 report keys, rebuilt from the stored record instead of a live run."""
    task, actions, events = full["task"], full["actions"], full["events"]
    counts: Dict[str, int] = {}
    evidence, seen, failures = [], set(), []
    uncertainty = [STOPPED_NOTE]
    changes, commands = [], []
    for e in events:
        if e["kind"] == "action.completed" and (e["data"] or {}).get("command"):
            d = e["data"]
            commands.append({"command": d["command"], "status": d.get("status"), "exit": d.get("exit"),
                             "ms": d.get("ms"), "check": bool(d.get("check"))})
        elif e["kind"] == "command.stopped_after_restart":
            uncertainty.append(f"A command this task started was still running after the restart and was stopped "
                               f"(process {e['data'].get('pid')}).")
        elif e["kind"] == "command.not_signalled":
            uncertainty.append(f"Process {e['data'].get('pid')} is running, but it could not be shown to be the "
                               f"command this task started (its id may have been reused), so it was left alone. "
                               f"Check it yourself.")
    for a in actions:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
        where = f"{a['tool']} {a['resource']}"
        if a["status"] == "completed":
            rec = _reconciled(events, a["action_id"])
            if a["tool"] in WRITE_TOOLS:
                ch = _change_of(by_action.get(a["action_id"]), rec)
                if ch:
                    changes.append(ch)
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
        rec = _reconciled(events, a["action_id"]) or {}
        if a["status"] == "unknown" and rec.get("outcome") == "conflict":
            uncertainty.append(f"{where}: Umbrous stopped while changing it, and the file is now neither as it was "
                               "nor as the change would have left it (something else changed it). It was left "
                               "alone; check it yourself.")
        elif a["status"] == "unknown" and a["tool"] in COMMAND_TOOLS:
            uncertainty.append(f"{where} was running when Umbrous stopped. What it did is unknown (it may have "
                               "changed files), and it is not run again by itself.")
        elif a["status"] == "unknown":
            uncertainty.append(f"{where} was running when Umbrous stopped and no result was recorded, so its "
                               f"outcome is unknown (retry class: {retry_class(a['tool'])}).")
        elif rec.get("outcome") == "not_done" and a["tool"] in WRITE_TOOLS:
            uncertainty.append(f"{where} was about to be changed when Umbrous stopped; the file shows it was not.")
    if reply_saved:
        uncertainty.append("The reply was saved before Umbrous stopped, but the task's own report was not; "
                           "this report was rebuilt from the record.")
    elif status == "unknown":
        uncertainty.append("Reply text that had arrived before the stop was not saved.")
    steps = max([e["data"].get("step", 0) for e in events if e["kind"] == "step.started"] or [0])
    limits_hit = [e["data"].get("limit") for e in events if e["kind"] == "limit" and e["data"].get("limit")]
    extra = {}
    if task.get("workspace"):
        extra["workspace"] = task["workspace"]
    if changes or commands:
        extra.update(changes=changes[:50], commands=commands[:50])
    return {**extra,
        "task_id": task["task_id"], "status": status, "code": code, "steps": steps, "tool_calls": len(actions),
        "actions": counts, "evidence": evidence[:50], "failures": failures, "uncertainty": uncertainty[:20],
        "limits_hit": limits_hit, "limits": task["limits"],
        "ms": _ms_between(task["created_at"], events[-1]["at"] if events else None),
        "citations_checked": 0, "recovered": True,
    }


def _reconciled(events: list, action_id: str) -> Optional[dict]:
    return next((e["data"] for e in events if e["kind"] == "action.reconciled" and e["action_id"] == action_id), None)


def _change_of(receipt: Optional[dict], rec: Optional[dict]) -> Optional[dict]:
    """A completed change, from its receipt (path, hash after, hash before) or from the disk check."""
    if receipt:
        ev = receipt.get("evidence") or []
        ch = next((e for e in ev if e.get("kind") == "change"), None)
        if ch:
            before = next((e.get("sha256") for e in ev if e.get("kind") == "other" and
                           (e.get("ref") or "").startswith("before:")), None)
            return {"path": ch["ref"], "op": "replaced" if before else "created", "before": before,
                    "after": ch.get("sha256"), "added": None, "removed": None}
    if rec and rec.get("outcome") == "done":
        return {"path": rec.get("path"), "op": "created" if not rec.get("before") else "replaced",
                "before": rec.get("before"), "after": rec.get("after"), "added": None, "removed": None,
                "recovered": True}
    return None


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
    # M9: a change checked on disk (a conflict) and a command are shown, not blockers:
    # you decide. Only an action nobody could check blocks the resume.
    if any(retry_class(a["tool"]) not in ("repeat", "command") and a["code"] != "conflict" for a in unknown):
        return no("needs_verification")
    coding = any(a["tool"] in WRITE_TOOLS + COMMAND_TOOLS for a in full["actions"])
    out = {"possible": True, "reason": None, "message": CODE_RESUME_WARNING if coding else RESUME_WARNING,
           "text": pending.user_text,
           "repeats": [{"tool": a["tool"], "resource": a["resource"], "status": a["status"]}
                       for a in full["actions"] if a["status"] in ("completed", "unknown")
                       and a["tool"] not in WRITE_TOOLS + COMMAND_TOOLS][:20]}
    if coding:
        out["done"] = [{"tool": a["tool"], "resource": a["resource"]} for a in full["actions"]
                       if a["status"] == "completed" and a["tool"] in WRITE_TOOLS][:20]
        out["conflicts"] = [a["resource"] for a in unknown if a["code"] == "conflict"][:20]
        out["commands_unknown"] = [a["resource"] for a in unknown if a["tool"] in COMMAND_TOOLS][:20]
        out["workspace"] = t.get("workspace")
    return out


def scope_check(gate: Gate, project_id: str, full: dict, approvals: bool,
                profile: Optional[str] = None) -> Optional[dict]:
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
        # M9: only the kind of action and its path matter to the check; the rest is a stand-in
        elif a["tool"] == "find_files":
            args["pattern"] = "resume-check"
        elif a["tool"] == "write_file":
            args["content"] = ""
        elif a["tool"] == "edit_file":
            args.update(old_text="resume-check", new_text="")
        elif a["tool"] == "run_command":
            args = {"command": "true"}
        elif a["tool"] == "check_command":
            continue
        v = gate.evaluate(project_id, Call(a["tool"], args), profile=profile)
        if v.outcome == ALLOW:
            continue
        if v.outcome == ASK and approvals:
            continue      # asked again during the resume; the old answer is not reused
        if v.outcome == ASK:
            return {"code": "resume_needs_approval",
                    "message": f"{a['tool']} {a['resource']} now needs your approval, and this client cannot ask "
                               "for it. The task stays interrupted. Resume it from Umbrous.app or the browser page, "
                               "or change the chat's access."}
        return {"code": "resume_scope_changed",
                "message": f"{a['tool']} {a['resource']} is no longer allowed ({v.reason}) The task stays "
                           "interrupted and is not replayed."}
    return None


def resume_brief(gate: Gate, tasks: TaskStore, full: dict, root: str) -> Optional[str]:
    """M9: what a resumed coding task is told about the interrupted attempt, from
    the record and the disk (never from the old model text): the changes that
    were made and whether each file is still as written, changes whose state is
    unclear, and commands whose effects are unknown. So the model continues the
    remaining work instead of doing the same changes again."""
    by_action = {r["action_id"]: r for r in gate.receipts.for_task(full["task"]["task_id"])}
    events = full["events"]
    done, unclear, cmds_ok, cmds_unknown = [], [], [], []
    for a in full["actions"]:
        if a["tool"] in WRITE_TOOLS and a["status"] == "completed":
            ch = _change_of(by_action.get(a["action_id"]), _reconciled(events, a["action_id"]))
            if not ch or not ch.get("path"):
                continue
            state, now = edit.current_hash(root, ch["path"])
            still = "still as written" if now == ch.get("after") else (
                "since removed" if state == "absent" else "changed again since")
            done.append(f"- {a['tool']} {ch['path']} ({ch['op']}; {still})")
        elif a["tool"] in WRITE_TOOLS and a["status"] == "unknown":
            unclear.append(f"- {a['tool']} {a['resource']}: interrupted, and the file now matches neither the old "
                           "nor the new version; read it before touching it")
        elif a["tool"] == "run_command" and a["status"] == "completed":
            d = next((e["data"] for e in events if e["kind"] == "action.completed" and e["action_id"] == a["action_id"]),
                     {}) or {}
            if d.get("command"):
                cmds_ok.append(f"- `{d['command']}`: {d.get('status')}" +
                               (f" (exit {d.get('exit')})" if d.get("exit") is not None else ""))
        elif a["tool"] == "run_command" and a["status"] == "unknown":
            d = _intent(events, a["action_id"], "command.intent") or {}
            cmds_unknown.append(f"- `{d.get('command') or '?'}`: interrupted; it may have changed files")
    if not (done or unclear or cmds_ok or cmds_unknown):
        return None
    parts = ["This task was interrupted (Umbrous stopped) and is now resumed in the same folder. From the record of "
             "the interrupted attempt and the files as they are now:"]
    if done:
        parts += ["Changes already made (do not make them again; read a file if you need its content):"] + done[:20]
    if unclear:
        parts += ["Changes whose state is unclear:"] + unclear[:10]
    if cmds_ok:
        parts += ["Commands that finished before the interruption (results may be stale):"] + cmds_ok[:10]
    if cmds_unknown:
        parts += ["Commands that were running when it stopped (do not just run them again; check the workspace "
                  "first, and only re-run one if that is safe):"] + cmds_unknown[:10]
    parts.append("Continue with what remains of the request. Nothing in this note is from a file or the user.")
    return "\n".join(parts)
