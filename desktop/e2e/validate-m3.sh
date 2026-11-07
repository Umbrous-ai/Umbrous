#!/bin/sh
# M3 in the native app (docs/m3-gate.md, "The desktop app").
#   desktop/e2e/validate-m3.sh WORKDIR
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
abort() { fail "$*"; echo "m3-access: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m3 "$W"/m3-fixture "$W"/m3-access.* "$W"/m3-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m3"; PORT=18559
python3 "$ROOT/desktop/e2e/stress.py" fake-workspace $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
WS=$(python3 "$ROOT/desktop/e2e/stress.py" seed-workspace "$W/m3-fixture") && [ -d "$WS" ] || abort "could not create the test workspace"
mkdir -p "$H" && echo "{\"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
U probe workspace-reader:3b >/dev/null || abort "tool check on the fake model"
U workspace General "$WS" --profile ask_before_read >/dev/null || abort "could not attach the workspace"
S="$ROOT/desktop/e2e/scripts"
e2e m3-access m3-access.txt "$H"
cleanup; FAKE=""
grep -h '"app.panic"' "$H/logs/desktop.log" 2>/dev/null && fail "the app panicked"
echo "receipts:"; U receipts --last 10
echo "profile now: $(U workspace General)"
[ $STATUS -eq 0 ] && echo "m3-access: PASS" || echo "m3-access: FAIL"
exit $STATUS
