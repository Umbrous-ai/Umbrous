#!/bin/sh
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 WORKDIR" >&2; exit 2; }
W="$1"; mkdir -p "$W"
. "$ROOT/desktop/e2e/lib.sh"
H="$(mktemp -d "$W/home-XXXXXX")"
E2E="$W/apps/Umbrous-e2e.app"; mkdir -p "$W/apps"
ditto "$ROOT/dist/Umbrous-e2e.app" "$E2E"
PORT=18570
PYTHONPATH="$ROOT" python3 - "$PORT" > "$W/fake.log" 2>&1 <<'PY' &
import sys, threading
from tests.fake_ollama import FakeModel, FakeOllama, workspace_agent
fake = FakeOllama(port=int(sys.argv[1]), models=[FakeModel('workspace-reader:3b', agent=workspace_agent)]).start()
threading.Event().wait()
PY
FAKE=$!
trap 'kill "$FAKE" 2>/dev/null || true' EXIT
sleep 1
kill -0 "$FAKE"
printf '{"codex_enabled":false,"ollama_url":"http://127.0.0.1:%s"}' "$PORT" > "$H/config.json"
mkdir -p "$W/source"
git -C "$W/source" init -q
printf 'Original text' > "$W/source/readme.txt"
git -C "$W/source" add readme.txt
git -C "$W/source" -c user.name=Test -c user.email=test@example.invalid commit -qm fixture
printf 'Project setup works' > "$W/source/readme.txt"
S="$ROOT/desktop/e2e/scripts"
e2e project-setup project-setup.txt "$H"
exit "$STATUS"
