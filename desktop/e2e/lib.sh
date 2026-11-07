# Shared by validate-native.sh and validate-m3.sh: run one scripted scenario in
# the e2e app and decide pass or fail. Sourced, not run.
# Needs: W (work folder), S (scripts folder), E2E (path of the e2e app copy).
# Every scenario script ends with `record end` just before `quit`; a scenario
# passes only if the app launched, wrote results, no step failed, that end
# record is the last line, the app logged no panic while it ran, and it then
# exited and cleaned up.

STATUS=${STATUS:-0}
fail() { echo "FAIL $*"; STATUS=1; }

alive() { pgrep -f "$1/Contents" >/dev/null; }
wait_gone() { for i in $(seq 1 200); do alive "$1" || return 0; sleep 0.1; done; return 1; }

check_clean() { # app home label
  if alive "$1"; then fail "$3: processes left"; return; fi
  [ -e "$2/run/runtime.json" ] && fail "$3: runtime.json left" || echo "ok   $3: no processes, runtime.json removed, $(grep backend.stopped "$2/logs/desktop.log" 2>/dev/null | tail -1 | sed 's/.*"how":"\([a-z]*\)".*/stop=\1/')"
}

# check_results FILE LABEL: the results file of one scenario. Returns 1 on failure.
check_results() {
  out=$1; label=$2; before=$STATUS; STATUS=0
  if [ ! -s "$out" ]; then
    fail "$label: no results (the app did not run the script)"
  else
    grep -q '"error"' "$out" && fail "$label: a step failed: $(grep -h '"error"' "$out" | head -1 | cut -c1-300)"
    if [ "$(tail -1 "$out" | grep -c '"record":"end"')" = 1 ]; then
      echo "ok   $label: reached its end, $(grep -c '"ok":true' "$out") assertions passed"
    else
      fail "$label: did not reach its end"
    fi
  fi
  rc=$STATUS; [ $before -ne 0 ] && STATUS=$before
  return $rc
}

# panics HOME: how many app.panic lines its desktop log holds so far (homes are
# reused across scenarios, so each scenario compares before and after itself)
panics() { grep -c '"app.panic"' "$1/logs/desktop.log" 2>/dev/null || true; }

e2e() { # name script home [extra open args]
  name=$1; script=$2; home=$3; shift 3
  rm -f "$W/$name.jsonl"
  p0=$(panics "$home"); p0=${p0:-0}
  sed -e "s|@OUT@|$W|g" -e "s|@NAME@|$name|g" "$S/$script" > "$W/$name.txt" || { fail "$name: could not prepare $script"; return; }
  open -n -W --env UMBROUS_HOME="$home" --env UMBROUS_E2E_SCRIPT="$W/$name.txt" --env UMBROUS_E2E_OUT="$W/$name.jsonl" "$@" "$E2E"
  rc=$?
  [ $rc -eq 0 ] || fail "$name: the app could not be launched (open exited $rc)"
  wait_gone "$E2E" || fail "$name: the app was still running after its script"
  echo "== $name"; cut -c1-700 "$W/$name.jsonl" 2>/dev/null
  check_results "$W/$name.jsonl" "$name"
  p1=$(panics "$home"); p1=${p1:-0}
  if [ "$p1" -gt "$p0" ]; then fail "$name: the app panicked: $(grep -h '"app.panic"' "$home/logs/desktop.log" | tail -1 | cut -c1-300)"
  else echo "ok   $name: no panic logged"; fi
  if [ "$name" = already-running ]; then  # the terminal runtime's runtime.json must stay
    alive "$E2E" && fail "$name: app still running" || echo "ok   $name: app exited"
    return
  fi
  check_clean "$E2E" "$home" "$name"
}
