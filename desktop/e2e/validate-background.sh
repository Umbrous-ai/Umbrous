#!/bin/sh
# Navigation and deletion with real streams, disposable home and fake inference.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
W="$1"; mkdir -p "$W"
. "$ROOT/desktop/e2e/lib.sh"
H="$(mktemp -d "$W/home-XXXXXX")"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E"
PORT=18569
PYTHONPATH="$ROOT" python3 - "$PORT" > "$W/fake.log" 2>&1 <<'PY' &
import sys, threading
from tests.fake_ollama import FakeModel, FakeOllama, workspace_agent
fake = FakeOllama(port=int(sys.argv[1]), models=[
    FakeModel('background:3b', reply='Continuing work. ' * 800, chunk_delay=0.01),
    FakeModel('quick:3b', reply='Quick reply.'),
    FakeModel('workspace-reader:3b', agent=workspace_agent)]).start()
threading.Event().wait()
PY
FAKE=$!
trap 'kill "$FAKE" 2>/dev/null || true' EXIT
sleep 1
kill -0 "$FAKE"
printf '{"codex_enabled":false,"ollama_url":"http://127.0.0.1:%s"}' "$PORT" > "$H/config.json"
mkdir -p "$W/source"
printf 'Background approval restored' > "$W/source/readme.txt"
printf 'Should require approval' > "$W/source/blocked.txt"
S="$ROOT/desktop/e2e/scripts"
e2e background-delete background-delete.txt "$H"
python3 - "$H/umbrous.db" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT name FROM projects').fetchall() == [('Other',)]
    assert sorted(c.execute('SELECT title FROM chats').fetchall()) == [('Keep Other E',), ('read readme.txt',)]
    assert sorted(c.execute('SELECT status FROM messages WHERE role="assistant"').fetchall()) == [('complete',), ('stopped',)]
    assert c.execute('SELECT COUNT(*) FROM pending_turns').fetchone()[0] == 0
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
print('PASS: navigation kept work alive; deletion stopped only selected work; no deleted chats returned')
PY
exit "$STATUS"
