#!/bin/sh
# Validate the packaged native app (docs/desktop.md, "Validation").
#   desktop/e2e/validate-native.sh WORKDIR
# Uses dist/Umbrous_<v>_aarch64.dmg (release, lifecycle checks) and
# dist/Umbrous-e2e.app (same code + test driver, UI checks), copied to WORKDIR,
# throwaway data folders, the real local Ollama and a fake one.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
W="$1"; mkdir -p "$W"; rm -rf "$W"/apps "$W"/h-* "$W"/*.jsonl "$W"/*.png
S="$ROOT/desktop/e2e/scripts"
APPS="$W/apps"; mkdir -p "$APPS"
MNT="$(mktemp -d)"; hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$ROOT"/dist/Umbrous_*_aarch64.dmg >/dev/null
ditto "$MNT/Umbrous.app" "$APPS/Umbrous.app"; hdiutil detach "$MNT" >/dev/null
ditto "$ROOT/dist/Umbrous-e2e.app" "$APPS/Umbrous-e2e.app"
REL="$APPS/Umbrous.app"; E2E="$APPS/Umbrous-e2e.app"

now() { python3 -c 'import time; print(int(time.time()*1000))'; }
alive() { pgrep -f "$1/Contents" >/dev/null; }
wait_gone() { for i in $(seq 1 200); do alive "$1" || return 0; sleep 0.1; done; return 1; }
quit_app() { perl -e 'alarm 15; exec @ARGV' osascript -e 'tell application id "ai.umbrous.desktop" to quit' >/dev/null 2>&1; wait_gone "$1"; }
check_clean() { # app home label
  if alive "$1"; then echo "FAIL $3: processes left"; return; fi
  [ -e "$2/run/runtime.json" ] && echo "FAIL $3: runtime.json left" || echo "ok   $3: no processes, runtime.json removed, $(grep backend.stopped "$2/logs/desktop.log" | tail -1 | sed 's/.*"how":"\([a-z]*\)".*/stop=\1/')"
}
e2e() { # name script home [extra env]
  name=$1; script=$2; home=$3; shift 3
  sed -e "s|@OUT@|$W|g" -e "s|@NAME@|$name|g" "$S/$script" > "$W/$name.txt"
  open -n -W --env UMBROUS_HOME="$home" --env UMBROUS_E2E_SCRIPT="$W/$name.txt" --env UMBROUS_E2E_OUT="$W/$name.jsonl" "$@" "$E2E"
  wait_gone "$E2E"
  echo "== $name"; cat "$W/$name.jsonl" 2>/dev/null | cut -c1-700
  if [ "$name" = already-running ]; then  # the terminal runtime's runtime.json must stay
    alive "$E2E" && echo "FAIL $name: app still running" || echo "ok   $name: app exited"
    return
  fi
  check_clean "$E2E" "$home" "$name"
}

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
kill -0 "$BP" 2>/dev/null && echo "FAIL shell killed: runtime $BP still running" || echo "ok   shell killed with SIGKILL: runtime exited on its own within $((i * 100)) ms"

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
if grep -q '"error"' "$W"/*.jsonl 2>/dev/null; then echo "FAIL: e2e assertions failed:"; grep -h '"error"' "$W"/*.jsonl; fi
grep -h '"app.panic"' "$W"/h-*/logs/desktop.log && echo "FAIL: the app panicked" || echo "ok   no panics logged"
[ "$(tail -1 "$W/nav-stream.jsonl" | grep -c '"record":"end"')" = 1 ] && echo "ok   nav-stream: all $(grep -c '"ok":true' "$W/nav-stream.jsonl") assertions passed" || echo "FAIL: nav-stream did not reach its end"
echo "### a runtime started from a terminal is reported and left running"
mkdir -p "$W/h-cli"
PYTHONPATH="$ROOT" python3 -m umbrous --home "$W/h-cli" serve --no-open --port 0 >/dev/null 2>&1 & CLI=$!; sleep 2
e2e already-running already-running.txt "$W/h-cli"
kill -0 $CLI 2>/dev/null && echo "ok   terminal runtime $CLI still running" || echo "FAIL: terminal runtime was stopped"
kill -INT $CLI; wait $CLI 2>/dev/null
mkdir -p "$W/h-badconfig"; echo '{"host": "0.0.0.0"}' > "$W/h-badconfig/config.json"
e2e bad-config failure.txt "$W/h-badconfig"
