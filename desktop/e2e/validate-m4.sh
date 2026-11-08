#!/bin/sh
# M4 in the native app (docs/m4-tasks.md, "The desktop app").
#   desktop/e2e/validate-m4.sh WORKDIR
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
abort() { fail "$*"; echo "m4-task: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m4 "$W"/m4-fixture "$W"/m4-task.* "$W"/m4-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m4"; PORT=18560
python3 "$ROOT/desktop/e2e/stress.py" fake-tasks $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
WS=$(python3 "$ROOT/desktop/e2e/stress.py" seed-workspace "$W/m4-fixture") && [ -d "$WS" ] || abort "could not create the test workspace"
mkdir -p "$H" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
for m in multi-step:3b slow-task:3b endless:3b; do U probe "$m" >/dev/null || abort "tool check on $m"; done
U workspace General "$WS" >/dev/null || abort "could not attach the workspace"
S="$ROOT/desktop/e2e/scripts"
e2e m4-task m4-task.txt "$H"
cleanup; FAKE=""
# What the runtime recorded, checked from outside the app: three tasks that ended
# completed, cancelled and completed (at the step limit), and nothing sensitive kept.
echo "tasks:"; U tasks --last 5
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" <<'PY' || fail "task records (see above)"
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute("SELECT status, code FROM tasks ORDER BY created_at").fetchall()
print("task statuses:", rows)
assert [r[0] for r in rows] == ["completed", "cancelled", "completed"], rows
limits = [r[0] for r in c.execute("SELECT data FROM task_events WHERE kind = 'limit'")]
assert any('"steps"' in d for d in limits), limits
dump = "\n".join(l for l in c.iterdump() if l.startswith('INSERT INTO "task'))
for s in ("ZEBRA-CANARY-991", "Ship M3 on Friday", "What is the plan"):
    assert s not in dump, s
print("ok   task tables hold no message, answer or file text")
PY
grep -q "ZEBRA-CANARY-991" "$H"/logs/*.log 2>/dev/null && fail "the message reached a log file" || echo "ok   logs hold no message text"
grep -h '"stop_to_idle_ms"' "$W"/m4-task.jsonl 2>/dev/null | sed 's/.*"stop_to_idle_ms":\([0-9]*\).*/Stop to idle: \1 ms (bound: 1000 ms)/'
ms=$(grep -h '"stop_to_idle_ms"' "$W"/m4-task.jsonl 2>/dev/null | sed 's/.*"stop_to_idle_ms":\([0-9]*\).*/\1/')
[ -n "$ms" ] && [ "$ms" -le 1000 ] || fail "Stop took ${ms:-?} ms (bound 1000 ms)"
[ $STATUS -eq 0 ] && echo "m4-task: PASS" || echo "m4-task: FAIL"
exit $STATUS
