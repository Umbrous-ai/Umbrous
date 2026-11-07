#!/bin/sh
# M3 in the native app (docs/m3-gate.md, "The desktop app").
#   desktop/e2e/validate-m3.sh WORKDIR
# Needs dist/Umbrous-e2e.app (desktop/scripts/build-native.sh --e2e). Uses a
# throwaway data folder, a throwaway workspace and a fake Ollama; your real data
# and Ollama are not touched.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
W="$1"; mkdir -p "$W"; rm -rf "$W"/h-m3 "$W"/m3-fixture "$W"/m3-access.* "$W"/m3-*.png
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"; ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E"
H="$W/h-m3"; PORT=18559
python3 "$ROOT/desktop/e2e/stress.py" fake-workspace $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
WS=$(python3 "$ROOT/desktop/e2e/stress.py" seed-workspace "$W/m3-fixture")
mkdir -p "$H"; echo "{\"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
U probe workspace-reader:3b >/dev/null || echo "FAIL: tool check on the fake model"
U workspace General "$WS" --profile ask_before_read >/dev/null || echo "FAIL: could not attach the workspace"
sed -e "s|@OUT@|$W|g" "$ROOT/desktop/e2e/scripts/m3-access.txt" > "$W/m3-access.txt"
open -n -W --env UMBROUS_HOME="$H" --env UMBROUS_E2E_SCRIPT="$W/m3-access.txt" --env UMBROUS_E2E_OUT="$W/m3-access.jsonl" "$E2E"
kill $FAKE 2>/dev/null
echo "== m3-access"; cut -c1-500 "$W/m3-access.jsonl" 2>/dev/null
grep -q '"error"' "$W/m3-access.jsonl" 2>/dev/null && echo "FAIL: m3-access assertions failed"
[ "$(tail -1 "$W/m3-access.jsonl" 2>/dev/null | grep -c '"record":"end"')" = 1 ] \
  && echo "ok   m3-access: all $(grep -c '"ok":true' "$W/m3-access.jsonl") assertions passed" || echo "FAIL: m3-access did not reach its end"
grep -h '"app.panic"' "$H/logs/desktop.log" 2>/dev/null && echo "FAIL: the app panicked"
echo "receipts:"; U receipts --last 10
echo "profile now: $(U workspace General)"
