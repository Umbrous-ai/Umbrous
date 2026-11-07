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

    def test_usage_errors(self):
        for script in ("validate-m3.sh", "validate-m4.sh", "validate-m5.sh", "validate-m6.sh", "validate-native.sh"):
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
    from umbrous.receipts.model import new_task_id
    from umbrous.tasks.store import TaskStore
    db.connect(Path(db_path)).close()
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
    from umbrous.receipts.model import Receipt, new_action_id, new_task_id
    from umbrous.receipts.store import ReceiptStore
    from umbrous.tasks.store import TaskStore
    db.connect(Path(db_path)).close()
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
    db.connect(Path(db_path)).close()
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "seed-tasks":
        seed_tasks(sys.argv[2], sys.argv[3])
    if len(sys.argv) > 1 and sys.argv[1] == "seed-resume":
        seed_resume(sys.argv[2], sys.argv[3])
    if len(sys.argv) > 1 and sys.argv[1] == "seed-memory":
        seed_memory(sys.argv[2], sys.argv[3])
    unittest.main()
