"""The native validation scripts must not report success they did not get.

They are run here for real, with stand-ins for the macOS tools they drive
(`open`, `ditto`, `hdiutil`, `osascript`, `pgrep`, `sleep`), so these tests
run on any platform with a POSIX shell. The `open` stand-in plays the e2e app:
per scenario it writes a complete result, a failed step, a result that stops
early, nothing at all, or fails to launch. Only a fully clean run may exit 0.
(Codex review, 2026-11-07: an injected failed assertion printed FAIL but
exited 0; then a failed launch with no results printed "ok".)
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# MODE applies to every scenario, or BAD=<scenario> gets MODE and the others pass.
OPEN_STUB = r"""#!/bin/sh
OUT=""; H=""
for a in "$@"; do case "$a" in UMBROUS_E2E_OUT=*) OUT=${a#*=};; UMBROUS_HOME=*) H=${a#*=};; esac; done
[ -z "$OUT" ] && exit 0                       # the release app, opened without a script
name=$(basename "$OUT" .jsonl)
mode=pass; if [ -z "${BAD:-}" ] || [ "$BAD" = "$name" ]; then mode=$MODE; fi
case "$mode" in
pass)     printf '{"expect":"x","ok":true}\n{"record":"end"}\n' > "$OUT"
          if [ "$name" = m4-task ]; then   # play the M4 run: its records in the data folder, Stop timing
            printf '{"step":"stop","stop_to_idle_ms":%s}\n{"record":"end"}\n' "${STOP_MS:-120}" > "$OUT"
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-tasks "$H/umbrous.db" "${TASKS:-good}"
          fi
          if [ "$name" = m5-resume ]; then  # play the M5 run: interrupted, then resumed
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-resume "$H/umbrous.db" "${RESUME:-good}"
          fi
          if [ "$name" = m6-memory ]; then  # play the M6 run: two items, four replies, one item deleted
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-memory "$H/umbrous.db" "${MEMORY:-good}"
          fi
          if [ "$name" = m7-review ]; then  # play the M7 run: three suggestions, two accepted, one rejected
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-review "$H/umbrous.db" "${REVIEW:-good}"
          fi
          if [ "$name" = m8-reset ]; then  # play the M8 run: two chats, a mixed reset, a fresh chat
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-reset "$H/umbrous.db" "${RESET:-good}"
          fi
          if [ "$name" = m9-coding ]; then  # play the M9 run: coding, Stop, killed and resumed
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-coding "$H/umbrous.db" "${CODING:-good}"
          fi
          if [ "$name" = m9-approve ]; then  # play the approval run: a command approved once, then a grant
            PYTHONPATH="$STUB_ROOT" python3 -m tests.test_validate_scripts seed-approve "$H/umbrous.db" "${APPROVE:-good}"
          fi;;
assert)   printf '{"expect":"x","ok":true}\n{"step":"expect y","error":"got z"}\n' > "$OUT";;
noend)    printf '{"expect":"x","ok":true}\n' > "$OUT";;
none)     ;;
panic)    printf '{"record":"end"}\n' > "$OUT"; mkdir -p "$H/logs"; echo '{"event":"app.panic"}' >> "$H/logs/desktop.log";;
openfail) exit 1;;
esac
"""
STUBS = {
    "open": OPEN_STUB,
    "ditto": '#!/bin/sh\nmkdir -p "$(dirname "$2")"; cp -R "$1" "$2"\n',
    "hdiutil": '#!/bin/sh\nif [ "$1" = attach ]; then for a; do m=$a; done; '
               'eval "mp=\\${$(($#-1))}"; mkdir -p "$mp/Umbrous.app"; fi\nexit 0\n',
    "osascript": "#!/bin/sh\nexit 0\n",
    "pgrep": "#!/bin/sh\nexit 1\n",            # no app process is ever left running
    "sleep": "#!/bin/sh\nexit 0\n",
}


@unittest.skipUnless(shutil.which("sh") and shutil.which("perl"), "needs a POSIX shell and perl")
class NativeValidationExitStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"   # a copy, so stand-in apps never land in the real dist/
        shutil.copytree(ROOT / "umbrous", self.root / "umbrous", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", self.root / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "desktop" / "e2e", self.root / "desktop" / "e2e")
        (self.root / "dist" / "Umbrous-e2e.app").mkdir(parents=True)
        (self.root / "dist" / "Umbrous_0.3.0_aarch64.dmg").write_text("stand-in")
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, text in STUBS.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.work = base / "work"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, mode="pass", bad=None, args=None, **extra):
        env = dict(os.environ, MODE=mode, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                   STUB_ROOT=str(self.root), **extra)
        env.pop("BAD", None)
        if bad:
            env["BAD"] = bad
        cmd = ["sh", str(self.root / "desktop" / "e2e" / script)] + ([str(self.work)] if args is None else args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)

    # ---- validate-m3.sh
    def test_m3_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m3.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m3-access: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m3-access: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m3.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m3-access: FAIL", r.stdout, mode)
            self.assertNotIn("m3-access: PASS", r.stdout, mode)

    def test_m3_setup_failure_stops_before_the_app(self):
        shutil.rmtree(self.root / "dist" / "Umbrous-e2e.app")
        r = self.run_script("validate-m3.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Umbrous-e2e.app is missing", r.stdout)
        self.assertIn("the app was not run", r.stdout)

    # ---- validate-m4.sh
    def test_m4_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m4.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m4-task: PASS", r.stdout)
        self.assertIn("Stop to idle: 120 ms", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m4-task: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m4.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m4-task: FAIL", r.stdout, mode)

    def test_m4_checks_the_records_and_the_stop_bound(self):
        for extra, why in (({"TASKS": "bad-status"}, "FAIL task records"),
                           ({"TASKS": "leak"}, "FAIL task records"),
                           ({"STOP_MS": "1500"}, "FAIL Stop took 1500 ms")):
            r = self.run_script("validate-m4.sh", **extra)
            self.assertEqual(r.returncode, 1, (extra, r.stdout + r.stderr))
            self.assertIn(why, r.stdout, extra)
            self.assertIn("m4-task: FAIL", r.stdout, extra)

    # ---- validate-m5.sh
    def test_m5_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m5.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m5-resume: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m5-resume: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m5.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m5-resume: FAIL", r.stdout, mode)

    def test_m5_checks_the_records(self):
        for kind in ("not-recovered", "not-resumed", "pending-left", "old-receipt-only", "leak"):
            r = self.run_script("validate-m5.sh", RESUME=kind)
            self.assertEqual(r.returncode, 1, (kind, r.stdout + r.stderr))
            self.assertIn("FAIL task records", r.stdout, kind)
            self.assertIn("m5-resume: FAIL", r.stdout, kind)

    # ---- validate-m6.sh
    def test_m6_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m6.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m6-memory: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m6-memory: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m6.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m6-memory: FAIL", r.stdout, mode)

    def test_m6_checks_the_records(self):
        for kind in ("not-deleted", "used-after-delete", "used-when-off", "no-ids", "old-reply-rewritten",
                     "same-project", "partial-no-ids"):
            r = self.run_script("validate-m6.sh", MEMORY=kind)
            self.assertEqual(r.returncode, 1, (kind, r.stdout + r.stderr))
            self.assertIn("FAIL memory records", r.stdout, kind)
            self.assertIn("m6-memory: FAIL", r.stdout, kind)
        r = self.run_script("validate-m6.sh", MEMORY="logged")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL memory text reached a log file", r.stdout)

    # ---- validate-m7.sh
    def test_m7_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m7.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m7-review: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m7-review: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m7.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m7-review: FAIL", r.stdout, mode)

    def test_m7_checks_the_records(self):
        for kind in ("personal-written", "pending-left", "rejected-kept-text", "not-used-later", "unapproved-row",
                     "claim-offered", "no-source"):
            r = self.run_script("validate-m7.sh", REVIEW=kind)
            self.assertEqual(r.returncode, 1, (kind, r.stdout + r.stderr))
            self.assertIn("FAIL review records", r.stdout, kind)
            self.assertIn("m7-review: FAIL", r.stdout, kind)
        r = self.run_script("validate-m7.sh", REVIEW="logged")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL suggested text reached a log file", r.stdout)

    # ---- validate-m8.sh
    def test_m8_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m8.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m8-reset: PASS", r.stdout)
        self.assertIn("ok   the CLI preview lists the 2 chats left", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m8-reset: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m8.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m8-reset: FAIL", r.stdout, mode)

    def test_m8_checks_the_records(self):
        for kind in ("not-deleted", "too-broad", "memory-left", "trace-left", "fresh-used"):
            r = self.run_script("validate-m8.sh", RESET=kind)
            self.assertEqual(r.returncode, 1, (kind, r.stdout + r.stderr))
            self.assertIn("FAIL reset records", r.stdout, kind)
            self.assertIn("m8-reset: FAIL", r.stdout, kind)
        r = self.run_script("validate-m8.sh", RESET="logged")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL memory text reached a log file", r.stdout)
        r = self.run_script("validate-m8.sh", RESET="no-reset-log")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL no reset.done in the log", r.stdout)

    # ---- validate-m9.sh
    def test_m9_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m9.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m9-coding: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m9-coding: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m9.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m9-coding: FAIL", r.stdout, mode)

    def test_m9_checks_the_approval_receipts(self):
        r = self.run_script("validate-m9.sh", APPROVE="nogrant")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAIL no receipt for the grant", r.stdout)

    def test_m9_checks_the_projects_and_the_records(self):
        for kind, why in (("duplicated", "FAIL project"), ("unfixed", "FAIL project"), ("tmp-left", "FAIL project"),
                          ("redone", "FAIL task records"), ("command-ok", "FAIL task records"),
                          ("not-stopped", "FAIL task records")):
            r = self.run_script("validate-m9.sh", CODING=kind)
            self.assertEqual(r.returncode, 1, (kind, r.stdout + r.stderr))
            self.assertIn(why, r.stdout, kind)
            self.assertIn("m9-coding: FAIL", r.stdout, kind)

    def test_native_fails_on_an_m9_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m9-coding")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m9.sh", r.stdout)

    def test_usage_errors(self):
        for script in ("validate-m3.sh", "validate-m4.sh", "validate-m5.sh", "validate-m6.sh", "validate-m7.sh",
                       "validate-m8.sh", "validate-m9.sh", "validate-native.sh"):
            self.assertEqual(self.run_script(script, args=[]).returncode, 2, script)

    # ---- validate-native.sh, the whole run
    def test_native_clean_run_passes(self):
        r = self.run_script("validate-native.sh")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
        self.assertIn("validate-native: PASS", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_native_failed_launch_with_no_results_fails(self):
        # the reviewer's reproduction: one scenario's app never launches and writes nothing
        for scenario in ("persist-1", "crash", "no-ollama", "already-running", "bad-config"):
            r = self.run_script("validate-native.sh", "openfail", bad=scenario)
            self.assertEqual(r.returncode, 1, (scenario, r.stdout[-2000:]))
            self.assertIn(f"FAIL {scenario}: the app could not be launched", r.stdout, scenario)
            self.assertIn(f"FAIL {scenario}: no results", r.stdout, scenario)
            self.assertNotIn(f"ok   {scenario}: reached its end", r.stdout, scenario)
            self.assertIn("validate-native: FAIL", r.stdout, scenario)

    def test_native_every_kind_of_scenario_failure_fails(self):
        for mode, why in (("none", "no results"), ("noend", "did not reach its end"), ("assert", "a step failed")):
            for scenario in ("no-models", "nav-stream"):
                r = self.run_script("validate-native.sh", mode, bad=scenario)
                self.assertEqual(r.returncode, 1, (mode, scenario, r.stdout[-2000:]))
                self.assertIn(f"FAIL {scenario}: {why}", r.stdout, (mode, scenario))
                self.assertNotIn(f"ok   {scenario}: reached its end", r.stdout, (mode, scenario))
                self.assertIn("validate-native: FAIL", r.stdout, (mode, scenario))

    def test_native_panic_in_any_scenario_fails(self):
        # the reviewer's case: the panic check ran before the last two scenarios
        for scenario in ("persist-2", "nav-stream", "already-running", "bad-config"):
            r = self.run_script("validate-native.sh", "panic", bad=scenario)
            self.assertEqual(r.returncode, 1, (scenario, r.stdout[-2000:]))
            self.assertIn(f"FAIL {scenario}: the app panicked", r.stdout, scenario)
            self.assertNotIn(f"ok   {scenario}: no panic logged", r.stdout, scenario)
            self.assertIn("validate-native: FAIL", r.stdout, scenario)

    def test_native_passes_on_an_m3_failure(self):
        r = self.run_script("validate-native.sh", "noend", bad="m3-access")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m3.sh", r.stdout)

    def test_native_fails_on_an_m4_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m4-task")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m4.sh", r.stdout)

    def test_native_fails_on_an_m5_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m5-resume")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m5.sh", r.stdout)

    def test_native_fails_on_an_m6_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m6-memory")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m6.sh", r.stdout)

    def test_native_fails_on_an_m7_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m7-review")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m7.sh", r.stdout)

    def test_native_fails_on_an_m8_failure(self):
        r = self.run_script("validate-native.sh", "assert", bad="m8-reset")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m8.sh", r.stdout)

    def test_every_scenario_script_ends_with_its_end_record(self):
        for f in (ROOT / "desktop" / "e2e" / "scripts").glob("*.txt"):
            lines = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
            self.assertEqual(lines[-2:], ["record end", "quit"], f.name)


def seed_tasks(db_path: str, kind: str) -> None:
    """What the M4 scenario leaves in its data folder, for the open stand-in:
    completed, cancelled (Stop), completed at the step limit. `kind` bad-status
    or leak breaks one thing for the checks to catch."""
    import sys
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.receipts.model import new_task_id
    from umbrous.tasks.store import TaskStore
    project_fixture_db(Path(db_path)).close()
    store = TaskStore(Path(db_path))
    ends = ["completed", "cancelled", "completed"] if kind != "bad-status" else ["completed", "completed", "completed"]
    for i, end in enumerate(ends):
        tid = new_task_id()
        store.create(tid, "prj_general", None, "m", {})
        store.set_status(tid, "started")
        if i == 2:
            store.event(tid, "limit", limit="steps")
        if kind == "leak" and i == 0:
            store.checkpoint(tid, 1, {"note": "ZEBRA-CANARY-991"})   # what the checks must catch
        store.set_status(tid, end)
    sys.exit(0)


def seed_resume(db_path: str, kind: str) -> None:
    """What the M5 scenario leaves: a completed task, one recovered as
    interrupted and resumed, and the resumed one, with four messages. Any
    other `kind` breaks one thing for the checks to catch."""
    import sys
    from datetime import datetime, timedelta, timezone
    from umbrous.history.store import HistoryStore
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.receipts.model import Receipt, new_action_id, new_task_id
    from umbrous.receipts.store import ReceiptStore
    from umbrous.tasks.store import TaskStore
    project_fixture_db(Path(db_path)).close()
    tasks, history, receipts = TaskStore(Path(db_path)), HistoryStore(Path(db_path)), ReceiptStore(Path(db_path))
    chat = history.create_chat("prj_general").id

    def receipt(tid):
        aid, now = new_action_id(), datetime.now(timezone.utc)
        tasks.add_action(tid, aid, 1, "list_files", ".")
        tasks.action_status(aid, "started")
        receipts.add(Receipt(tid, aid, "fs.list", "project:prj_general", "allowed", now,
                             now + timedelta(milliseconds=3)), "prj_general", chat)
        tasks.action_status(aid, "completed", "ok")

    first, cut, resumed = new_task_id(), new_task_id(), new_task_id()
    for tid in (first, cut, resumed):
        tasks.create(tid, "prj_general", chat, "m", {})
        tasks.set_status(tid, "started")
    receipt(first)
    tasks.set_status(first, "completed")
    history.append_exchange(chat, "What is the plan? Resume check", "Ship it.", "m", "complete", task_id=first)
    receipt(cut)
    if kind != "not-recovered":
        tasks.recover_task(cut, "unknown", "interrupted")
    if kind != "not-resumed":
        tasks.event(cut, "task.resumed", by=resumed)
        tasks.event(resumed, "task.resume_of", of=cut)
    if kind != "old-receipt-only":
        receipt(resumed)
    tasks.artifact(resumed, "report", {"status": "completed", "resume_of": cut})
    tasks.set_status(resumed, "completed")
    text = "Walk through the workspace ZEBRA-CANARY-552"
    history.append_exchange(chat, text, "Looking through the workspace, step by step.", "m", "complete",
                            task_id=resumed)
    if kind == "pending-left":
        history.begin_pending(cut, chat, text, "m")
    if kind == "leak":
        tasks.event(resumed, "note", text=text)   # what the checks must catch
    sys.exit(0)


def seed_memory(db_path: str, kind: str) -> None:
    """What the M6 scenario leaves: one personal item (edited), a deleted project
    decision, and four replies: with both items, with memory off, after the
    delete, and in another project. Any other `kind` breaks one thing."""
    import sys
    from umbrous.history.store import HistoryStore
    from umbrous.memory.store import Memory
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    project_fixture_db(Path(db_path)).close()
    history, mem = HistoryStore(Path(db_path)), Memory(Path(db_path))
    other = history.create_project("Other").id
    pref = mem.personal.add("I like answers in British English, kept short", "fact")
    dec = mem.project.add("prj_general", "The release branch is called ZEBRA-SHIP-731")
    chat = history.create_chat("prj_general").id
    first = "Using what you saved: ZEBRA-SHIP-731" if kind != "old-reply-rewritten" else "rewritten"
    history.append_exchange(chat, "What is the release branch?", first, "m", "complete",
                            memory_ids=None if kind == "no-ids" else [dec.id, pref.id])
    history.append_exchange(chat, "What is the release branch?", "no memory", "m", "complete",
                            memory_ids=[pref.id] if kind == "used-when-off" else None)
    if kind != "not-deleted":
        mem.delete(dec.id)
    history.append_exchange(chat, "What is the release branch?", "kept short", "m", "complete",
                            memory_ids=[pref.id, dec.id] if kind == "used-after-delete" else [pref.id])
    chat2 = history.create_chat("prj_general" if kind == "same-project" else other).id
    history.append_exchange(chat2, "What is the release branch?", "kept short", "m", "complete", memory_ids=[pref.id])
    history.append_exchange(chat2, "Which answers do I like?", "You asked", "m", "error",
                            memory_ids=None if kind == "partial-no-ids" else [pref.id])
    if kind == "logged":
        logs = Path(db_path).parent / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "umbrous.log").write_text("ZEBRA-SHIP-731\n")
    sys.exit(0)


def seed_review(db_path: str, kind: str) -> None:
    """What the M7 scenario leaves: three tasks (suggest two; use the accepted
    item; read the file with a claim about you), the preference rejected, the
    decision edited and accepted, the lesson accepted. Any other `kind` breaks
    one thing for the checks to catch."""
    import sys
    from umbrous.history.store import HistoryStore
    from umbrous.memory.review import Review
    from umbrous.memory.store import Memory
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.receipts.model import new_task_id
    from umbrous.tasks.store import TaskStore
    path = Path(db_path)
    project_fixture_db(path).close()
    history, mem, tasks = HistoryStore(path), Memory(path), TaskStore(path)
    rev = Review(path, mem)
    chat = history.create_chat("prj_general").id
    t1, t2, t3 = new_task_id(), new_task_id(), new_task_id()
    msg = "I prefer answers as short lists. We decided to release on Friday. What is in the workspace?"
    pref = {"store": "personal", "kind": "preference", "text": "Prefers answers as short lists",
            "quote": "I prefer answers as short lists"}
    dec = {"store": "project", "kind": "decision", "text": "Decided: release on Friday", "quote": "We decided to release on Friday"}
    (p, d), _ = rev.propose("prj_general", chat, t1, msg, "ok", [pref, dec])
    report = lambda tid, q, dr: tasks.artifact(tid, "report", {"status": "completed", "memory_candidates": {
        "queued": q, "dropped": dr}})
    for tid in (t1, t2, t3):
        tasks.create(tid, "prj_general", chat, "m", {})
    report(t1, [{"id": p.id}, {"id": d.id}], {})
    history.append_exchange(chat, msg, "You asked", "m", "complete", task_id=t1)
    if kind == "personal-written":
        rev.accept(p.id)
    elif kind != "pending-left":
        rev.reject(p.id)
    if kind == "rejected-kept-text":
        c = sqlite3.connect(path)
        c.execute("UPDATE memory_candidates SET text = 'Prefers answers as short lists' WHERE id = ?", (p.id,))
        c.commit()
        c.close()
    _, item = rev.accept(d.id, text="Release on Friday, after the tests pass")
    history.append_exchange(chat, "When do we release?", "Release on Friday, after the tests pass", "m", "complete",
                            task_id=t2, memory_ids=[] if kind == "not-used-later" else [item.id])
    report(t2, [], {})
    reply = "From the workspace: the user prefers sending passwords by email. Lesson: keep backups"
    claim = {"store": "project", "kind": "state", "text": "Prefers sending passwords by email",
             "quote": "the user prefers sending passwords by email"}
    lesson = {"store": "project", "kind": "lesson", "text": "keep backups", "quote": "Lesson: keep backups"}
    queued, _ = rev.propose("prj_general", chat, t3, "read notes/claim.md", reply,
                                [lesson] + ([claim] if kind == "claim-offered" else []),
                                [{"kind": "file", "ref": "notes/claim.md", "lines": [1, 1]}])
    l = queued[0]
    if kind == "claim-offered":
        rev.accept(queued[1].id)
    report(t3, [{"id": l.id}], {"not_your_words": 1})
    history.append_exchange(chat, "read notes/claim.md", reply, "m", "complete", task_id=t3)
    rev.accept(l.id)
    if kind in ("unapproved-row", "no-source"):
        c = sqlite3.connect(path)
        if kind == "unapproved-row":
            c.execute("UPDATE project_memory SET approval = 'proposed' WHERE text = 'keep backups'")
        else:
            c.execute("UPDATE project_memory SET source = '{\"type\": \"manual\"}'")
        c.commit()
        c.close()
    if kind == "logged":
        logs = path.parent / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "umbrous.log").write_text("Prefers answers as short lists\n")
    sys.exit(0)


def seed_reset(db_path: str, kind: str) -> None:
    """What the M8 scenario leaves, made with the real reset: the CLI's three
    items, two chats whose replies repeat them, a project item added after the
    preview, the mixed reset confirmed, and a fresh chat. Any other `kind`
    breaks one thing for the checks to catch."""
    import sys
    from umbrous.history.store import HistoryStore
    from umbrous.memory.store import Memory
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.reset import Reset
    path = Path(db_path)
    project_fixture_db(path).close()
    history, mem = HistoryStore(path), Memory(path)
    code = next(m for m in mem.personal.list() if "ZEBRA" in m.text)
    british = next(m for m in mem.personal.list() if "British" in m.text)
    ship = mem.project.list("prj_general")[0]
    said = f"Using what you saved: [{code.id}] {code.text} [{british.id}] {british.text} [{ship.id}] {ship.text}"
    ids = [code.id, british.id, ship.id]
    gone = history.create_chat("prj_general").id
    history.append_exchange(gone, "What do you remember?", "You asked: What do you remember? " + said, "m", "complete",
                            memory_ids=ids)
    keep = history.create_chat("prj_general").id
    history.append_exchange(keep, "Keep this chat", "You asked: Keep this chat " + said, "m", "complete", memory_ids=ids)
    mem.project.add("prj_general", "Tests run on every push")
    sel = {"chats": [gone] + ([keep] if kind == "too-broad" else []), "personal": [code.id],
           "project": {} if kind == "memory-left" else {"prj_general": "all"}, "start_fresh": True}
    if kind == "trace-left":    # deleted without overwriting the freed space
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete = OFF")
        c.execute("DELETE FROM messages WHERE chat_id = ?", (gone,))
        c.execute("DELETE FROM chats WHERE id = ?", (gone,))
        c.execute("DELETE FROM personal_memory WHERE id = ?", (code.id,))
        c.execute("DELETE FROM project_memory")
        c.commit()
        c.close()
    elif kind != "not-deleted":
        rs = Reset(path)
        rs.confirm(sel, rs.preview(sel)["preview_id"])
    fresh = history.create_chat("prj_general").id
    history.append_exchange(fresh, "What do you remember?", "You asked: What do you remember? Using what you saved: "
                            + (code.text if kind == "fresh-used" else f"[{british.id}] {british.text}"), "m", "complete")
    logs = path.parent / "logs"
    logs.mkdir(exist_ok=True)
    lines = [] if kind == "no-reset-log" else ['{"event": "reset.done", "chats": 1}']
    if kind == "logged":
        lines.append("ZEBRA-SHIP-731")
    (logs / "umbrous.log").write_text("\n".join(lines) + "\n")
    sys.exit(0)


def seed_approve(db_path: str, kind: str) -> None:
    """What the composer-approval scenario leaves: command receipts, one approved
    once and one allowed by the grant you gave. `nogrant` leaves out the grant."""
    from datetime import datetime, timedelta, timezone
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.receipts.model import Receipt, new_action_id, new_task_id
    from umbrous.receipts.store import ReceiptStore
    project_fixture_db(Path(db_path)).close()
    receipts = ReceiptStore(Path(db_path))
    tid = new_task_id()
    reasons = [("denied", "denied_by_you: You denied it."), ("allowed", "Approved once by you.")] + (
        [] if kind == "nogrant" else [("allowed", "Allowed by a grant for this project (cmd.run).")])
    for decision, reason in reasons:
        now = datetime.now(timezone.utc)
        receipts.add(Receipt(tid, new_action_id(), "cmd.run", "project:prj_general resource:.", decision, now,
                             now + timedelta(milliseconds=3), reason), "prj_general", None)


def seed_coding(db_path: str, kind: str) -> None:
    """What the M9 scenario leaves: both projects fixed (one multiply each), and
    the task records of coding, Stop, the killed task and its resume. Any other
    `kind` breaks one thing for the checks to catch."""
    import sys
    import time
    from tests.test_m9_coding import FIXED
    from umbrous.persistence import db
    from tests.helpers import project_fixture_db
    from umbrous.receipts.model import new_action_id, new_task_id
    from umbrous.tasks.store import TaskStore
    path = Path(db_path)
    work = path.parent.parent
    for name in ("a", "b"):
        calc = work / "m9-fixture" / name / "proj" / "calc.py"
        text = FIXED
        if name == "b" and kind == "duplicated":
            text = FIXED + "\n\ndef multiply(a, b):\n    return a * b\n"
        if name == "b" and kind == "unfixed":
            text = FIXED.replace("a * b", "a + b")
        calc.write_text(text)
        if name == "b" and kind == "tmp-left":
            (calc.parent / ".umbrous-tmp-0011aabb").write_text(text)
    project_fixture_db(path).close()
    store = TaskStore(path)
    b = os.path.realpath(work / "m9-fixture" / "b" / "proj")
    ids = []
    for status, code, ws in (("completed", None, os.path.realpath(work / "m9-fixture" / "a" / "proj")),
                             ("cancelled", "cancelled", None), ("unknown", "interrupted", b), ("completed", None, b)):
        tid = new_task_id()
        ids.append(tid)
        store.create(tid, "prj_general", None, "coder:3b", {}, workspace=ws)
        store.set_status(tid, "started")
        if status == "unknown":
            store.recover_task(tid, "unknown", "interrupted")
        else:
            store.set_status(tid, status, code)
        time.sleep(0.01)
    cut, resumed = ids[2], ids[3]
    e, r = new_action_id(), new_action_id()
    store.add_action(cut, e, 1, "edit_file", "calc.py")
    store.action_status(e, "started")
    store.event(cut, "write.intent", e, path="calc.py")
    store.action_status(e, "completed", "ok")
    store.add_action(cut, r, 2, "run_command", ".")
    store.action_status(r, "started")
    if kind != "command-ok":
        store.recover_action(r, "unknown", "interrupted")
    else:
        store.action_status(r, "completed", "ok")
    if kind != "not-stopped":
        store.event(cut, "command.stopped_after_restart", r, pid=4242)
    for n in range(2 if kind == "redone" else 1):
        a = new_action_id()
        store.add_action(resumed, a, 1 + n, "edit_file", "calc.py")
        store.event(resumed, "write.intent", a, path="calc.py")
    store.artifact(resumed, "report", {"changes": [{"path": "calc.py", "op": "edited"}],
                                       "commands": [{"command": "python3 -B -m unittest -q test_calc", "exit": 1},
                                                    {"command": "python3 -B -m unittest -q test_calc", "exit": 0}]})
    sys.exit(0)


if __name__ == "__main__":
    import sys
    SEEDS = {"seed-approve": seed_approve, "seed-coding": seed_coding, "seed-tasks": seed_tasks,
             "seed-resume": seed_resume, "seed-memory": seed_memory, "seed-reset": seed_reset,
             "seed-review": seed_review}
    if len(sys.argv) > 1 and sys.argv[1] in SEEDS:
        SEEDS[sys.argv[1]](sys.argv[2], sys.argv[3])
        sys.exit(0)
    unittest.main()
