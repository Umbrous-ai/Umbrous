#!/bin/sh
# Validate the packaged native app (docs/desktop.md, "Validation").
#   desktop/e2e/validate-native.sh WORKDIR
# Uses dist/Umbrous_<v>_aarch64.dmg (release, lifecycle checks) and
# dist/Umbrous-e2e.app (same code + test driver, UI checks), copied to WORKDIR,
# throwaway data folders, the real local Ollama and a fake one.
set -u
# Exit status: 0 only if no check below printed FAIL; 1 otherwise (a setup
# failure stops the run); 2 for a usage error.
[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
STATUS=0
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/desktop/e2e/lib.sh"   # fail, alive, wait_gone, check_clean, check_results, e2e
abort() { fail "$*"; echo "validate-native: FAIL (setup)"; exit 1; }
W="$1"; mkdir -p "$W" || abort "cannot create $W"; rm -rf "$W"/apps "$W"/h-* "$W"/*.jsonl "$W"/*.png
S="$ROOT/desktop/e2e/scripts"
APPS="$W/apps"; mkdir -p "$APPS"
MNT="$(mktemp -d)"; hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$ROOT"/dist/Umbrous_*_aarch64.dmg >/dev/null || abort "could not open dist/Umbrous_*_aarch64.dmg"
ditto "$MNT/Umbrous.app" "$APPS/Umbrous.app" || { hdiutil detach "$MNT" >/dev/null; abort "could not copy Umbrous.app out of the .dmg"; }
hdiutil detach "$MNT" >/dev/null
ditto "$ROOT/dist/Umbrous-e2e.app" "$APPS/Umbrous-e2e.app" || abort "could not copy dist/Umbrous-e2e.app (build-native.sh --e2e)"
REL="$APPS/Umbrous.app"; E2E="$APPS/Umbrous-e2e.app"

now() { python3 -c 'import time; print(int(time.time()*1000))'; }
quit_app() { perl -e 'alarm 15; exec @ARGV' osascript -e 'tell application id "ai.umbrous.desktop" to quit' >/dev/null 2>&1; wait_gone "$1"; }

echo "### release build: startup, duplicate launch, crash, quit, shell killed"
H="$W/h-release"; mkdir -p "$H"
for r in 1 2 3 4 5 6; do
  t0=$(now); open --env UMBROUS_HOME="$H" "$REL"
  for i in $(seq 1 150); do [ "$(grep -c ui.first_full_frame "$H/logs/desktop.log" 2>/dev/null)" = "$r" ] && break; sleep 0.05; done
  st=$(grep app.started "$H/logs/desktop.log" | tail -1 | sed 's/.*"ts_ms":\([0-9]*\).*/\1/')
  ff=$(grep ui.first_full_frame "$H/logs/desktop.log" | tail -1 | sed 's/.*"ts_ms":\([0-9]*\).*/\1/')
  rd=$(grep backend.ready "$H/logs/desktop.log" | tail -1 | sed 's/.*"ms":"\([0-9]*\)".*/\1/')
  echo "launch $r: open->process $((st - t0)) ms, open->first full frame $((ff - t0)) ms, process->first full frame $((ff - st)) ms, python ready $rd ms"
  [ $r -lt 6 ] && { quit_app "$REL"; check_clean "$REL" "$H" "quit $r" >/dev/null; }
done
open -n --env UMBROUS_HOME="$H" "$REL"; sleep 2
echo "duplicate launch: $(pgrep -f "$REL/Contents/MacOS" | wc -l | tr -d ' ') shell, $(pgrep -f "$REL/Contents/Resources/backend" | wc -l | tr -d ' ') runtime; $(grep -c app.second_launch "$H/logs/desktop.log") second launch(es) redirected"
BP=$(pgrep -f "$REL/Contents/Resources/backend" | head -1); kill -9 "$BP"; sleep 1
echo "runtime killed: shell alive=$(pgrep -f "$REL/Contents/MacOS" >/dev/null && echo yes || echo no); $(grep backend.exited "$H/logs/desktop.log" | tail -1)"
quit_app "$REL"; check_clean "$REL" "$H" "quit after runtime crash"
open --env UMBROUS_HOME="$H" "$REL"; for i in $(seq 1 100); do pgrep -f "$REL/Contents/Resources/backend" >/dev/null && break; sleep 0.1; done; sleep 1
SP=$(pgrep -f "$REL/Contents/MacOS" | head -1); BP=$(pgrep -f "$REL/Contents/Resources/backend" | head -1); kill -9 "$SP"
for i in $(seq 1 50); do kill -0 "$BP" 2>/dev/null || break; sleep 0.1; done
kill -0 "$BP" 2>/dev/null && fail "shell killed: runtime $BP still running" || echo "ok   shell killed with SIGKILL: runtime exited on its own within $((i * 100)) ms"

echo "### e2e build: UI checks"
e2e persist-1 persist-1.txt "$W/h-persist"
e2e persist-2 persist-2.txt "$W/h-persist"
e2e crash crash.txt "$W/h-crash"
mkdir -p "$W/h-noollama"; echo '{"ollama_url": "http://127.0.0.1:9"}' > "$W/h-noollama/config.json"
e2e no-ollama states.txt "$W/h-noollama"
python3 "$ROOT/desktop/e2e/stress.py" fake-empty 18557 >/dev/null 2>&1 & FAKE=$!; sleep 1
mkdir -p "$W/h-nomodels"; echo '{"ollama_url": "http://127.0.0.1:18557"}' > "$W/h-nomodels/config.json"
e2e no-models states.txt "$W/h-nomodels"; kill $FAKE

echo "### regression: navigation while a reply streams, stale responses"
python3 "$ROOT/desktop/e2e/stress.py" fake-ollama 18558 >/dev/null 2>&1 & FAKE=$!; sleep 1
python3 "$ROOT/desktop/e2e/stress.py" seed-reference "$W/h-nav" >/dev/null
echo '{"ollama_url": "http://127.0.0.1:18558"}' > "$W/h-nav/config.json"
e2e nav-stream nav-stream.txt "$W/h-nav"; kill $FAKE
if grep -q '"error"' "$W"/*.jsonl 2>/dev/null; then fail "e2e assertions failed:"; grep -h '"error"' "$W"/*.jsonl; fi
echo "### M3: Ask before read in the app, the switch, reads (validate-m3.sh)"
"$ROOT/desktop/e2e/validate-m3.sh" "$W" || fail "validate-m3.sh: M3 in the app failed (see above)"
echo "### M4: a task's report, Stop during a step, a forced limit (validate-m4.sh)"
"$ROOT/desktop/e2e/validate-m4.sh" "$W" || fail "validate-m4.sh: M4 in the app failed (see above)"

echo "### M5: kill the runtime mid-task, restart, resume (validate-m5.sh)"
"$ROOT/desktop/e2e/validate-m5.sh" "$W" || fail "validate-m5.sh: M5 in the app failed (see above)"
echo "### M6: personal and project memory, a reply that uses them, delete (validate-m6.sh)"
"$ROOT/desktop/e2e/validate-m6.sh" "$W" || fail "validate-m6.sh: M6 in the app failed (see above)"
echo "### M7: suggestions to review after a task, accept and reject (validate-m7.sh)"
"$ROOT/desktop/e2e/validate-m7.sh" "$W" || fail "validate-m7.sh: M7 in the app failed (see above)"
echo "### M8: Start fresh, a selective reset with a preview (validate-m8.sh)"
"$ROOT/desktop/e2e/validate-m8.sh" "$W" || fail "validate-m8.sh: M8 in the app failed (see above)"
echo "### M9: coding in a folder, Stop during a command, kill and resume (validate-m9.sh)"
"$ROOT/desktop/e2e/validate-m9.sh" "$W" || fail "validate-m9.sh: M9 in the app failed (see above)"
echo "### a runtime started from a terminal is reported and left running"
mkdir -p "$W/h-cli"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$W/h-cli" serve --no-open --port 0 >/dev/null 2>&1 & CLI=$!; sleep 2
e2e already-running already-running.txt "$W/h-cli"
kill -0 $CLI 2>/dev/null && echo "ok   terminal runtime $CLI still running" || fail "terminal runtime was stopped"
# stop it as Ctrl-C would; escalate if it ignores that (a background job may start with SIGINT ignored)
kill -INT $CLI 2>/dev/null; for i in $(seq 1 50); do kill -0 $CLI 2>/dev/null || break; sleep 0.1; done
kill -0 $CLI 2>/dev/null && kill -TERM $CLI 2>/dev/null; wait $CLI 2>/dev/null
mkdir -p "$W/h-badconfig"; echo '{"host": "0.0.0.0"}' > "$W/h-badconfig/config.json"
e2e bad-config failure.txt "$W/h-badconfig"
if grep -q '"error"' "$W"/*.jsonl 2>/dev/null; then fail "e2e assertions failed in: $(grep -l '"error"' "$W"/*.jsonl | tr '\n' ' ')"; fi
# and once more over every data folder, after the last scenario (also catches the release-build launches)
grep -h '"app.panic"' "$W"/h-*/logs/desktop.log 2>/dev/null && fail "the app panicked (see above)" || echo "ok   no panics logged in any scenario"
[ $STATUS -eq 0 ] && echo "validate-native: PASS" || echo "validate-native: FAIL"
exit $STATUS
