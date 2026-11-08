#!/bin/sh
# M8 in the native app (docs/m8-reset.md, "The desktop app"): Start fresh
# previews exactly what a reset deletes and what stays, Cancel changes nothing,
# a preview that no longer matches is refused, and a confirmed reset deletes
# exactly its preview; the next reply is given none of the reset memory.
#   desktop/e2e/validate-m8.sh WORKDIR
# Needs dist/Umbrous-e2e.app (desktop/scripts/build-native.sh --e2e). Uses a
# throwaway data folder and a fake Ollama; your real data and Ollama are not touched.
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
abort() { fail "$*"; echo "m8-reset: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m8 "$W"/m8-reset.* "$W"/m8-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m8"; PORT=18564
python3 "$ROOT/desktop/e2e/stress.py" fake-memory $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
mkdir -p "$H" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
U memory add personal "My codename is ZEBRA-SHIP-731" >/dev/null || abort "could not add personal memory"
U memory add personal "I like answers in British English" --kind preference >/dev/null || abort "could not add personal memory"
U memory add project "The release branch is ship-friday" --project General >/dev/null || abort "could not add project memory"
S="$ROOT/desktop/e2e/scripts"
e2e m8-reset m8-reset.txt "$H"
cleanup; FAKE=""
# What the runtime kept, checked from outside the app: exactly the confirmed
# preview is gone, and nothing of it is left in the database file; the kept
# chat's reply still holds the codename (as the preview said); logs hold ids only.
echo "after the reset:"; U memory
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" <<'PY' || fail "reset records (see above)"
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
chats = c.execute("SELECT title FROM chats ORDER BY created_at, rowid").fetchall()
personal = [r[0] for r in c.execute("SELECT text FROM personal_memory")]
project = c.execute("SELECT text FROM project_memory").fetchall()
print("chats:", chats); print("personal:", personal); print("project:", project)
assert [t for (t,) in chats] == ["Keep this chat", "What do you remember?"], chats   # the reset chat is gone; the fresh one is new
assert personal == ["I like answers in British English"], personal
assert project == [], project
with_code = c.execute("SELECT ch.title FROM messages m JOIN chats ch ON ch.id = m.chat_id "
                      "WHERE m.content LIKE '%ZEBRA-SHIP-731%'").fetchall()
assert with_code == [("Keep this chat",)], with_code       # only the kept reply, which the preview named as kept
c.close()
raw = open(sys.argv[1], "rb").read()
# the kept reply repeats every item it was given (the preview said so); any other copy would be a trace.
# "Keep this chat": its title, the message, and the reply quoting it
for text, n in ((b"My codename is", 1), (b"ship-friday", 1), (b"Tests run on every push", 0), (b"Keep this chat", 3)):
    assert raw.count(text) == n, f"{text!r} appears {raw.count(text)} times in the file, expected {n}"
print("ok   exactly the confirmed preview is gone, also from the file's free space; the kept chat keeps its reply")
PY
grep -qE "ZEBRA-SHIP-731|ship-friday|British English" "$H"/logs/*.log 2>/dev/null && fail "memory text reached a log file" || echo "ok   logs hold no memory text"
grep -q '"reset.done"' "$H"/logs/*.log 2>/dev/null && echo "ok   the reset is logged (ids and counts)" || fail "no reset.done in the log"
# the CLI shows the same preview and deletes nothing without --confirm
U reset --all-chats General | tail -3
[ "$(U reset --all-chats General | grep -c '^  chat ')" = 2 ] && echo "ok   the CLI preview lists the 2 chats left" || fail "CLI preview"
[ "$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM chats").fetchone()[0])' "$H/umbrous.db")" = 2 ] && echo "ok   a preview deletes nothing" || fail "a preview deleted something"
[ $STATUS -eq 0 ] && echo "m8-reset: PASS" || echo "m8-reset: FAIL"
exit $STATUS
