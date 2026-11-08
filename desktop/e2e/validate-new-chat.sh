#!/bin/sh
# Disposable home and fake provider: send after deleting the last project.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
W="$1"; mkdir -p "$W"
. "$ROOT/desktop/e2e/lib.sh"
H="$(mktemp -d "$W/home-XXXXXX")"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E"
PORT=18568
python3 "$ROOT/desktop/e2e/stress.py" fake-memory "$PORT" > "$W/fake.log" 2>&1 & FAKE=$!
trap 'kill "$FAKE" 2>/dev/null || true' EXIT
sleep 1
kill -0 "$FAKE"
printf '{"codex_enabled":false,"ollama_url":"http://127.0.0.1:%s"}' "$PORT" > "$H/config.json"
S="$ROOT/desktop/e2e/scripts"
e2e new-chat-no-project new-chat-no-project.txt "$H"
python3 - "$H/umbrous.db" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT name FROM projects').fetchall() == [('General',)]
    assert c.execute('SELECT COUNT(*) FROM chats').fetchone()[0] == 2
    assert c.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 4
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
print('PASS: new chats send and persist after deleting the last project')
PY
exit "$STATUS"
