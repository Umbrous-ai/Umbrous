#!/bin/sh
# Disposable data only: compact deletion, cancel, archives, last project and files.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
W="$1"; mkdir -p "$W"
. "$ROOT/desktop/e2e/lib.sh"
H="$(mktemp -d "$W/home-XXXXXX")"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E"
PYTHONPATH="$ROOT" python3 - "$H" <<'PY'
import json, sys
from pathlib import Path
from umbrous.persistence import db
from umbrous.history.store import HistoryStore
from umbrous.gate.access import AccessStore
from umbrous.memory.store import Memory
home = Path(sys.argv[1])
db.connect(home / 'umbrous.db').close()
(home / 'config.json').write_text(json.dumps({'codex_enabled': False, 'ollama_url': 'http://127.0.0.1:1'}))
hist = HistoryStore(home / 'umbrous.db')
p = hist.create_project('Disposable')
for title in ('Active chat', 'Archive then delete', 'Already archived'):
    chat = hist.create_chat(p.id)
    hist.append_exchange(chat.id, title, 'Disposable reply', 'test', 'complete')
    if title == 'Already archived': hist.set_archived(chat.id, True)
source = home.parent / 'source'
source.mkdir(exist_ok=True)
(source / 'keep.txt').write_text('keep my source')
AccessStore(home / 'umbrous.db', home).attach(p.id, str(source), 'workspace_read')
mem = Memory(home / 'umbrous.db')
mem.project.add(p.id, 'Disposable project memory', 'decision')
mem.personal.add('Keep personal memory', 'fact')
PY
S="$ROOT/desktop/e2e/scripts"
e2e delete-simple delete-simple.txt "$H"
python3 - "$H/umbrous.db" "$W/source/keep.txt" <<'PY'
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT name FROM projects').fetchall() == [('Fresh project',)]
    for table in ('chats', 'messages', 'project_memory', 'workspaces', 'grants'):
        assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
    assert c.execute('SELECT text FROM personal_memory').fetchall() == [('Keep personal memory',)]
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
assert Path(sys.argv[2]).read_text() == 'keep my source'
print('PASS: one confirmation, cancellation, archives, last project, personal memory and source files')
PY
exit "$STATUS"
