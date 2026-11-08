"""M9: prove the regression tests catch what they guard.

Each mutation removes one M9 behaviour from a copy of the code (never this
checkout) and runs the tests that guard it. A mutation passes the proof only
if those tests then FAIL; the unmutated copy must pass them all first.

    python3 -m tests.prove_m9          # about a minute; exit 0 only if every proof holds
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = "tests.test_m9_coding."

# (what is removed, file, old text, new text, tests that must now fail)
MUTATIONS = [
    ("the hash check against the version the task read",
     "umbrous/tools/edit.py", "if expect is not None and before != expect:", "if False:",
     [T + "ToolTests.test_a_newer_edit_is_reported_not_overwritten"]),
    ("the last check just before the rename",
     "umbrous/tools/edit.py", "if now_bytes is None or sha(now_bytes) != before:", "if False:",
     [T + "ToolTests.test_conflict_found_at_the_last_moment_leaves_the_file_alone"]),
    ("create-only for write_file without an expected hash",
     "umbrous/tools/edit.py", "if old_bytes is not None and create_only:", "if False:",
     [T + "ToolTests.test_create_refuses_to_overwrite_an_existing_file"]),
    ("recognising an edit that was already made",
     "umbrous/tools/edit.py", 'return text, "edited_already"', 'raise FsError("no_match", "x")',
     [T + "ToolTests.test_edit_results_no_match_ambiguous_and_already_applied"]),
    ("recording the intent before writing (moved after the write)",
     "umbrous/tools/edit.py",
     '            intent({"path": path, "before": before, "after": after, "tmp": "/".join(parts[:-1] + (tmp,)),\n'
     '                    "op": "create" if before is None else "replace"})\n',
     '            _late = lambda: intent({"path": path, "before": before, "after": after, "tmp": tmp, "op": "x"})\n',
     [T + "ToolTests.test_the_write_is_atomic_and_keeps_the_mode", T + "ToolTests.test_no_intent_means_no_write"]),
    ("Stop reaching a running command",
     "umbrous/tools/run.py", "            if cancelled():", "            if False:",
     [T + "ToolTests.test_cancel_stops_a_command_quickly", T + "LoopTests.test_stop_during_a_command_ends_it_quickly"]),
    ("dropping the session token from a command's environment",
     "umbrous/tools/run.py", 'env.pop("UMBROUS_SESSION_TOKEN", None)', "pass",
     [T + "ToolTests.test_command_runs_in_the_workspace_without_the_session_token"]),
    ("checking an interrupted change on disk (back to M5: unknown)",
     "umbrous/tasks/recover.py", "        elif a[\"status\"] == \"started\" and a[\"tool\"] in WRITE_TOOLS:\n",
     "        elif False:\n",
     [T + "RecoveryTests.test_crash_after_the_write", T + "RecoveryTests.test_crash_after_the_intent_before_the_write",
      T + "CrashTests.test_kill_during_the_tests_then_resume_without_duplicating_the_edit"]),
    ("stopping a command left running by the dead runtime",
     "umbrous/tasks/recover.py", "            _stop_leftover(tasks, task_id, a, events)", "            pass",
     [T + "RecoveryTests.test_an_interrupted_command_is_unknown_and_its_process_is_stopped"]),
    ("telling a resumed task what was already done",
     "umbrous/tasks/recover.py", "    if not (done or unclear or cmds_ok or cmds_unknown):", "    if True:",
     [T + "RecoveryTests.test_resume_tells_the_model_what_was_done_and_does_not_redo_it"]),
    ("re-checking the current permissions before a resume",
     "umbrous/tasks/recover.py", '        elif a["tool"] == "run_command":\n            args = {"command": "true"}\n',
     '        elif a["tool"] == "run_command":\n            continue\n',
     [T + "RecoveryTests.test_resume_rechecks_command_permission"]),
    ("the report's note that no command checked the changes",
     "umbrous/tasks/run.py", 'self.note(f"{len(changed)} file change(s) were made, and no command was run to check them.")',
     "pass",
     [T + "LoopTests.test_the_report_says_when_the_last_check_failed_or_none_ran"]),
    ("stopping what a finished command left running in its group",
     "umbrous/tools/run.py", "            if not c.done() or group_alive(c.pid):\n", "            if not c.done():\n",
     [T + "ToolTests.test_ending_the_task_stops_what_a_finished_command_left_running"]),
    ("checking a process's identity before signalling it after a restart",
     "umbrous/tools/run.py", "    return identity(pid) == {\"boot\": ident[\"boot\"], \"start\": ident[\"start\"]}",
     "    return True",
     [T + "RecoveryTests.test_a_reused_process_id_is_not_signalled"]),
]


def _case_insensitive_disk() -> bool:
    with tempfile.TemporaryDirectory() as d:
        Path(d, "Probe").write_text("")
        return Path(d, "probe").exists()


# Only a disk that does not tell calc.py from CALC.py (the macOS default) can show this one.
CASE_MUTATION = ("remembering a file's version under the disk's own spelling",
                 "umbrous/gate/gate.py", "        return edit.canonical_path(root, rel)", "        return rel",
                 [T + "ToolTests.test_a_remembered_version_is_found_through_the_disk_spelling"])
if _case_insensitive_disk():
    MUTATIONS.append(CASE_MUTATION)


def run_tests(copy: Path, tests) -> bool:
    r = subprocess.run([sys.executable, "-m", "unittest", "-q", *tests], cwd=copy, capture_output=True, text=True,
                       timeout=600)
    return r.returncode == 0


def main() -> int:
    every = sorted({t for m in MUTATIONS for t in m[4]})
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "repo"
        for d in ("umbrous", "tests", "bin", "prototypes"):
            shutil.copytree(ROOT / d, base / d, ignore=shutil.ignore_patterns("__pycache__"))
        if not run_tests(base, every):
            print("FAIL the unmutated copy does not pass the guarding tests")
            return 1
        print(f"ok   unmutated: all {len(every)} guarding tests pass")
        if CASE_MUTATION not in MUTATIONS:
            print(f"skip without {CASE_MUTATION[0]}: this disk tells file names apart by case")
        for what, rel, old, new, tests in MUTATIONS:
            f = base / rel
            original = f.read_text()
            if original.count(old) != 1:
                print(f"FAIL {what}: the text to remove is not in {rel} exactly once (update this proof)")
                bad += 1
                continue
            f.write_text(original.replace(old, new))
            try:
                caught = not run_tests(base, tests)
            finally:
                f.write_text(original)
            print(("ok   " if caught else "FAIL ") + f"without {what}: " +
                  ("the tests fail, as they should" if caught else "the tests still PASS (they do not guard it)"))
            bad += 0 if caught else 1
    print("prove-m9: " + ("PASS" if not bad else f"FAIL ({bad})"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
