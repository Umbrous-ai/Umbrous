#!/bin/sh
# M6 in the native app (docs/m6-memory.md, "The desktop app"): personal and
# project memory in the right panel, a reply that uses them and names their
# ids, memory off, edit, delete, the old chat unchanged, another project.
#   desktop/e2e/validate-m6.sh WORKDIR
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
abort() { fail "$*"; echo "m6-memory: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m6 "$W"/m6-memory.* "$W"/m6-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m6"; PORT=18562
python3 "$ROOT/desktop/e2e/stress.py" fake-memory $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
mkdir -p "$H" && echo "{\"codex_enabled\": false, \"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
S="$ROOT/desktop/e2e/scripts"
e2e m6-memory m6-memory.txt "$H"
cleanup; FAKE=""
# What the runtime recorded, checked from outside the app: the two stores, the
# ids stored with each reply, the deleted item gone from its store but still
# named by the old reply (and its text still in that reply), nothing logged.
echo "memory:"; PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" memory
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" <<'PY' || fail "memory records (see above)"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
personal = c.execute("SELECT id, kind, text, scope FROM personal_memory").fetchall()
project = c.execute("SELECT id, project_id, text FROM project_memory").fetchall()
print("personal:", personal); print("project:", project)
assert len(personal) == 1 and personal[0][2].endswith("kept short") and personal[0][3] == "all", personal
assert project == [], project                                   # the decision was deleted
pid = personal[0][0]
replies = c.execute("SELECT m.chat_id, ch.project_id, m.content, m.memory_ids, m.status FROM messages m JOIN chats ch "
                    "ON ch.id = m.chat_id WHERE m.role = 'assistant' ORDER BY m.created_at, m.seq").fetchall()
ids = [json.loads(r[3]) if r[3] else [] for r in replies]
print("memory ids per reply:", ids)
assert len(replies) == 5, replies
assert len(ids[0]) == 2 and pid in ids[0], ids[0]                # both items, by id
deleted = next(i for i in ids[0] if i != pid)
assert "ZEBRA-SHIP-731" in replies[0][2]                         # the old reply still says it
assert ids[1] == [], ids[1]                                      # memory off
assert ids[2] == [pid] and ids[3] == [pid], ids                  # after the delete; and in the other project
assert replies[3][1] != replies[0][1], "the fourth reply should be in the other project"
assert replies[4][4] != "complete" and ids[4] == [pid], (replies[4][4], ids[4])   # the partial reply keeps its ids
assert c.execute("SELECT COUNT(*) FROM personal_memory WHERE id = ?", (deleted,)).fetchone()[0] == 0
print("ok   two stores; ids stored with each reply (also the partial one); the deleted item is gone and no later reply used it; the old reply kept its text")
PY
grep -q "ZEBRA-SHIP-731" "$H"/logs/*.log 2>/dev/null && fail "memory text reached a log file" || echo "ok   logs hold no memory text"
[ $STATUS -eq 0 ] && echo "m6-memory: PASS" || echo "m6-memory: FAIL"
exit $STATUS
