#!/bin/sh
# M7 in the native app (docs/m7-review.md, "The desktop app"): a task suggests
# things to remember, you edit, accept and reject them in the Memory panel, a
# later reply uses the accepted item, and a file's claim about you is not
# offered as a personal fact.
#   desktop/e2e/validate-m7.sh WORKDIR
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
abort() { fail "$*"; echo "m7-review: setup failed, the app was not run"; exit 1; }

W="$1"; mkdir -p "$W" || abort "cannot create $W"
rm -rf "$W"/h-m7 "$W"/m7-fixture "$W"/m7-review.* "$W"/m7-*.png
[ -d "$ROOT/dist/Umbrous-e2e.app" ] || abort "dist/Umbrous-e2e.app is missing (desktop/scripts/build-native.sh --e2e)"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"; rm -rf "$E2E"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E" || abort "could not copy the e2e app"
H="$W/h-m7"; PORT=18563
python3 "$ROOT/desktop/e2e/stress.py" fake-review $PORT >/dev/null 2>&1 & FAKE=$!; sleep 1
kill -0 "$FAKE" 2>/dev/null || abort "the fake Ollama did not start (port $PORT in use?)"
WS=$(python3 "$ROOT/desktop/e2e/stress.py" seed-review-workspace "$W/m7-fixture") && [ -d "$WS" ] || abort "could not create the test workspace"
mkdir -p "$H" && echo "{\"ollama_url\": \"http://127.0.0.1:$PORT\"}" > "$H/config.json" || abort "could not write $H/config.json"
U() { PYTHONPATH="$ROOT" python3 -m umbrous --home "$H" "$@"; }
U probe memory-notes:3b >/dev/null || abort "tool check on memory-notes:3b"
U workspace General "$WS" >/dev/null || abort "could not attach the workspace"
S="$ROOT/desktop/e2e/scripts"
e2e m7-review m7-review.txt "$H"
cleanup; FAKE=""
# What the runtime recorded, checked from outside the app: only the two accepted
# items are in memory, both project memory with their source; the queue keeps no
# text once decided; the later reply was given the accepted item; nothing logged.
echo "memory:"; U memory; U memory suggestions --all
PYTHONPATH="$ROOT" python3 - "$H/umbrous.db" <<'PY' || fail "review records (see above)"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
personal = c.execute("SELECT id, text FROM personal_memory").fetchall()
project = c.execute("SELECT id, kind, text, source, approval FROM project_memory ORDER BY created_at").fetchall()
cands = c.execute("SELECT store, kind, status, text, quote, basis, memory_id FROM memory_candidates ORDER BY rowid").fetchall()
print("personal:", personal); print("project:", [(p[1], p[2]) for p in project]); print("candidates:", cands)
assert personal == [], personal                                    # nothing personal: one rejected, one never offered
assert [(p[1], p[2]) for p in project] == [("decision", "Release on Friday, after the tests pass"), ("lesson", "keep backups")], project
assert all(p[4] == "approved" for p in project), project
dec, lesson = (json.loads(p[3]) for p in project)
assert dec["type"] == "task" and dec["basis"] == "message" and dec["edited"] is True, dec
assert lesson["basis"] == "reply" and any(e["ref"] == "notes/claim.md" for e in lesson["evidence"]), lesson
assert [(x[0], x[1], x[2]) for x in cands] == [("personal", "preference", "rejected"), ("project", "decision", "accepted"),
                                                ("project", "lesson", "accepted")], cands
assert all(x[3] == "" and x[4] == "" for x in cands), "a decided suggestion kept its text"
assert [x[6] for x in cands][1:] == [p[0] for p in project], "accepted suggestions name their memory item"
replies = c.execute("SELECT memory_ids, task_id FROM messages WHERE role = 'assistant' ORDER BY created_at, seq").fetchall()
ids = [json.loads(r[0]) if r[0] else [] for r in replies]
print("memory ids per reply:", ids)
assert len(replies) == 3 and ids[0] == [] and ids[1] == [project[0][0]], ids     # the later reply used the accepted item
reports = [json.loads(c.execute("SELECT body FROM task_artifacts WHERE task_id = ? AND kind = 'report'", (r[1],)).fetchone()[0])
           for r in replies]
mc = [r.get("memory_candidates") for r in reports]
assert len(mc[0]["queued"]) == 2 and mc[1]["queued"] == [] and mc[2]["dropped"] == {"not_your_words": 1}, mc
mem_dump = "\n".join(l for l in c.iterdump() if l.startswith(('INSERT INTO "personal_memory"', 'INSERT INTO "project_memory"',
                                                               'INSERT INTO "memory_candidates"', 'INSERT INTO "task')))
assert "short lists" not in mem_dump and "passwords" not in mem_dump, "rejected or refused text kept"
print("ok   only accepted items reached memory, in the project store, with their source; decided suggestions keep no text; the later reply used the accepted item; the file's claim never became a suggestion")
PY
grep -qE "short lists|passwords by email|release on Friday" "$H"/logs/*.log 2>/dev/null && fail "suggested text reached a log file" || echo "ok   logs hold no suggested text"
[ $STATUS -eq 0 ] && echo "m7-review: PASS" || echo "m7-review: FAIL"
exit $STATUS
