#!/bin/sh
# M5 in the native app (docs/m5-resume.md, "The desktop app"): kill the runtime
# in the middle of a task, restart it, see the interrupted task, resume it.
#   desktop/e2e/validate-m5.sh WORKDIR
# Needs dist/Umbrous-e2e.app (desktop/scripts/build-native.sh --e2e). Uses a
# throwaway data folder, a throwaway workspace and a fake Ollama; your real data
# and Ollama are not touched.
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
abort() { fail "$*"; echo "m5-resume: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m5 "$W"/m5-fixture "$W"/m5-resume.* "$W"/m5-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m5"; PORT=18561
python3 "$ROOT/desktop/e2e/stress.py" fake-tasks $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
WS=$(python3 "$ROOT/desktop/e2e/stress.py" seed-workspace "$W/m5-fixture") && [ -d "$WS" ] || abort "could not create the test workspace"
mkdir -p "$H" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
seed_general "$H"
for m in multi-step:3b slow-task:3b; do U probe "$m" >/dev/null || abort "tool check on $m"; done
U workspace General "$WS" >/dev/null || abort "could not attach the workspace"
S="$ROOT/desktop/e2e/scripts"
e2e m5-resume m5-resume.txt "$H"
cleanup; FAKE=""
# What the runtime recorded, checked from outside the app: the first task
# completed; the killed one was recovered as interrupted (its completed read
# kept) and then resumed; the resumed one completed with its own new receipt.
echo "tasks:"; U tasks --last 5
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" <<'PY' || fail "task records (see above)"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute("SELECT task_id, status, code FROM tasks ORDER BY created_at").fetchall()
print("task statuses:", [(r[1], r[2]) for r in rows])
assert [(r[1], r[2]) for r in rows] == [("completed", None), ("unknown", "interrupted"), ("completed", None)], rows
first, cut, resumed = (r[0] for r in rows)
kinds = lambda t: [k for (k,) in c.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY seq", (t,))]
assert "task.interrupted" in kinds(cut) and "task.resumed" in kinds(cut), kinds(cut)
assert "task.resume_of" in kinds(resumed), kinds(resumed)
acts = c.execute("SELECT status FROM task_actions WHERE task_id = ?", (cut,)).fetchall()
assert acts and all(a[0] == "completed" for a in acts), acts      # the read before the kill was kept
rec_old = c.execute("SELECT COUNT(*) FROM receipts WHERE task_id = ?", (cut,)).fetchone()[0]
rec_new = c.execute("SELECT COUNT(*) FROM receipts WHERE task_id = ?", (resumed,)).fetchone()[0]
assert rec_old >= 1 and rec_new >= 1, (rec_old, rec_new)           # repeated with a new receipt
report = json.loads(c.execute("SELECT body FROM task_artifacts WHERE task_id = ? AND kind = 'report'", (resumed,)).fetchone()[0])
assert report.get("resume_of") == cut, report
assert c.execute("SELECT COUNT(*) FROM pending_turns").fetchone()[0] == 0, "a message is still waiting"
msgs = c.execute("SELECT role, content, task_id FROM messages ORDER BY seq").fetchall()
assert [m[0] for m in msgs] == ["user", "assistant", "user", "assistant"], msgs
assert "ZEBRA-CANARY-552" in msgs[2][1] and msgs[2][2] == resumed, msgs[2]
dump = "\n".join(l for l in c.iterdump() if l.startswith('INSERT INTO "task'))
assert "ZEBRA-CANARY-552" not in dump
print("ok   interrupted, kept its completed read, resumed with a new receipt; no message waiting; task tables hold no message text")
PY
grep -q "ZEBRA-CANARY-552" "$H"/logs/*.log 2>/dev/null && fail "the message reached a log file" || echo "ok   logs hold no message text"
[ $STATUS -eq 0 ] && echo "m5-resume: PASS" || echo "m5-resume: FAIL"
exit $STATUS
