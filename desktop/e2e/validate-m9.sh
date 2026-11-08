#!/bin/sh
# M9 in the native app (docs/m9-coding.md, "The desktop app"): "work on <folder>"
# attaches it with the Code profile, a coding task edits a file, runs the tests,
# fixes the failure and finishes; Stop ends a running command; the runtime killed
# while the tests run is recovered (the edit kept, the command not re-run, its
# process stopped) and the resumed task finishes without making the edit twice.
#   desktop/e2e/validate-m9.sh WORKDIR
# Needs dist/Umbrous-e2e.app (desktop/scripts/build-native.sh --e2e). Uses a
# throwaway data folder, two throwaway projects and a fake Ollama; your real data
# and Ollama are not touched. The fake models run `python3` in those projects.
# Exit status: 0 only if setup worked, every assertion passed, the script reached
# its end and the app did not panic; 1 otherwise; 2 for a usage error.
set -u
[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STATUS=0
. "$ROOT/desktop/e2e/lib.sh"   # fail, alive, wait_gone, check_clean, check_results, e2e
FAKE=""
cleanup() { [ -n "$FAKE" ] && kill "$FAKE" 2>/dev/null; }
trap cleanup EXIT
abort() { fail "$*"; echo "m9-coding: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m9 "$W"/h-m9-approve "$W"/m9-fixture "$W"/m9-coding.* "$W"/m9-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m9"; PORT=18565
python3 "$ROOT/desktop/e2e/stress.py" fake-coding $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
A=$(python3 "$ROOT/desktop/e2e/stress.py" seed-coding-project "$W/m9-fixture/a") && [ -d "$A" ] || abort "could not create project A"
B=$(python3 "$ROOT/desktop/e2e/stress.py" seed-coding-project "$W/m9-fixture/b") && [ -d "$B" ] || abort "could not create project B"
mkdir -p "$H" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
for m in coder:3b slow-coder:3b; do U probe "$m" >/dev/null || abort "tool check on $m"; done
S="$ROOT/desktop/e2e/scripts"
e2e m9-coding m9-coding.txt "$H"
cleanup; FAKE=""
# What happened, checked from outside the app: both projects end with exactly
# one, fixed multiply; no temporary file and no test command left running; the
# task records say what each task changed and ran.
for P in "$A" "$B"; do
  python3 - "$P" <<'PY' || fail "project $P"
import sys
from pathlib import Path
p = Path(sys.argv[1])
calc = (p / "calc.py").read_text()
assert calc.count("def multiply") == 1 and "return a * b" in calc, calc
assert not [n for n in p.iterdir() if n.name.startswith(".umbrous-tmp")], list(p.iterdir())
print(f"ok   {p}: one multiply, fixed; no temporary file left")
PY
done
pgrep -f "sleep 30; python3 -B" >/dev/null && fail "a test command is still running" || echo "ok   no test command left running"
echo "tasks:"; U tasks --last 6
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" "$B" <<'PY' || fail "task records (see above)"
import json, os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute("SELECT task_id, status, code, workspace FROM tasks WHERE model != 'chatty:3b' "
                 "ORDER BY created_at").fetchall()
print("task statuses:", [(r[1], r[2]) for r in rows])
# coding in A, stopped, the killed task in B, its resume (the plain exchange is not counted)
assert [r[1] for r in rows] == ["completed", "cancelled", "unknown", "completed"], rows
assert rows[2][2] == "interrupted", rows
cut, resumed = rows[2][0], rows[3][0]
assert rows[2][3] == rows[3][3] == os.path.realpath(sys.argv[2]), rows
acts = dict(c.execute("SELECT tool, status FROM task_actions WHERE task_id = ? AND tool IN ('edit_file', 'run_command')", (cut,)).fetchall())
assert acts == {"edit_file": "completed", "run_command": "unknown"}, acts
kinds = [k for (k,) in c.execute("SELECT kind FROM task_events WHERE task_id = ?", (cut,))]
# the command ended with the runtime (its supervisor stopped its group) or recovery stopped it
assert ("command.gone_after_restart" in kinds or "command.stopped_after_restart" in kinds) and "write.intent" in kinds, kinds
redo = c.execute("SELECT COUNT(*) FROM task_actions a JOIN task_events e ON e.action_id = a.action_id "
                 "WHERE a.task_id = ? AND e.kind = 'write.intent'", (resumed,)).fetchone()[0]
report = json.loads(c.execute("SELECT body FROM task_artifacts WHERE task_id = ? AND kind = 'report'", (resumed,)).fetchone()[0])
print("resumed task changed:", [(x["path"], x["op"]) for x in report.get("changes", [])], "ran:", [(x["command"][:40], x["exit"]) for x in report.get("commands", [])])
assert redo == 1, redo                                    # only the fix; the first edit was not made again
assert report["commands"][-1]["exit"] == 0, report["commands"]
print("ok   interrupted: the edit kept, the command unknown and stopped; resumed: only the fix, tests pass")
PY
# The composer's access menu and approval cards, in a home of its own where coder:3b has
# not been tool-checked (the app runs the check), with a third project on Ask before commands.
H2="$W/h-m9-approve"
C=$(python3 "$ROOT/desktop/e2e/stress.py" seed-coding-project "$W/m9-fixture/c") && [ -d "$C" ] || abort "could not create project C"
D=$(python3 "$ROOT/desktop/e2e/stress.py" seed-coding-project "$W/m9-fixture/d") && [ -d "$D" ] || abort "could not create project D"
mkdir -p "$H2" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H2/config.json" || abort "could not write $H2/config.json"
python3 "$ROOT/desktop/e2e/stress.py" fake-coding $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start again (port $PORT in use?)"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$H2" workspace General "$C" --profile code_ask_commands >/dev/null || abort "could not attach project C"
e2e m9-approve m9-approve.txt "$H2"
cleanup; FAKE=""
echo "receipts (approvals):"; PYTHONPATH="$ROOT" python3 -m umbrous --home "$H2" receipts --last 12 | grep -E "cmd\.run" || fail "no command receipts"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$H2" receipts --last 30 | grep -q "Approved once by you" || fail "no receipt for the once approval"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$H2" receipts --last 30 | grep -q "Allowed by a grant" || fail "no receipt for the grant"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$H2" receipts --last 30 | grep -q "denied_by_you" || fail "no receipt for the denied long command"
[ $STATUS -eq 0 ] && echo "m9-coding: PASS" || echo "m9-coding: FAIL"
exit $STATUS
