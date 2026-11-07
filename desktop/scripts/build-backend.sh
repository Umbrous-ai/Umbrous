#!/bin/sh
# Assemble the Python runtime that ships inside Umbrous.app:
#
#   build/desktop-backend/
#     python/   a relocatable CPython (python-build-standalone, as installed by uv),
#               trimmed to what the standard-library-only runtime needs
#     app/      umbrous/ and prototypes/umbrous-harness.html, exactly as in the repo
#
# Tauri copies this folder to Umbrous.app/Contents/Resources/backend/.
# Every .py is compiled ahead of time with unchecked-hash .pyc files, so the
# app never writes into its own (signed) bundle and copying does not make the
# caches stale.
#
#   desktop/scripts/build-backend.sh            uses CPython 3.13 from uv (installs it if needed)
#   UMBROUS_BUNDLE_PYTHON=/path/to/python3 ...  use another python-build-standalone install
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/build/desktop-backend"
PY_VERSION="${UMBROUS_BUNDLE_PY_VERSION:-3.13}"

if [ -n "${UMBROUS_BUNDLE_PYTHON:-}" ]; then
  SRC_PY="$UMBROUS_BUNDLE_PYTHON"
else
  command -v uv >/dev/null 2>&1 || { echo "uv not found. Install uv (https://docs.astral.sh/uv/) or set UMBROUS_BUNDLE_PYTHON." >&2; exit 1; }
  uv python install "$PY_VERSION" >/dev/null
  SRC_PY="$(uv python find --managed-python "$PY_VERSION")"
fi

PREFIX="$("$SRC_PY" -c 'import sys; print(sys.base_prefix)')"
VER="$("$SRC_PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
FULLVER="$("$SRC_PY" -c 'import platform; print(platform.python_version())')"
[ -f "$PREFIX/bin/python$VER" ] || { echo "No python$VER in $PREFIX/bin" >&2; exit 1; }
# python-build-standalone links Python statically; a framework or Homebrew build
# would depend on files outside the bundle and must not be used.
if otool -L "$PREFIX/bin/python$VER" | grep -Eq 'libpython|Python.framework|/opt/homebrew|/usr/local'; then
  echo "$PREFIX/bin/python$VER is not a relocatable standalone build (see otool -L). Use uv's managed Python." >&2
  exit 1
fi

echo "Bundling CPython $FULLVER from $PREFIX"
rm -rf "$OUT"
mkdir -p "$OUT/python/bin" "$OUT/python/lib" "$OUT/app/prototypes"

cp "$PREFIX/bin/python$VER" "$OUT/python/bin/python3"
# Standard library, minus what a headless, standard-library-only server never imports.
rsync -a \
  --exclude '__pycache__/' \
  --exclude 'site-packages/*' \
  --exclude 'test/' --exclude 'tests/' --exclude 'idlelib/' --exclude 'tkinter/' --exclude 'turtledemo/' \
  --exclude 'ensurepip/' --exclude 'lib2to3/' --exclude 'pydoc_data/' --exclude 'venv/' \
  --exclude 'config-*-darwin/' --exclude 'turtle.py' \
  --exclude 'lib-dynload/_tkinter*' --exclude 'lib-dynload/_test*' --exclude 'lib-dynload/xx*' \
  --exclude 'lib-dynload/_ctypes_test*' \
  "$PREFIX/lib/python$VER" "$OUT/python/lib/"
cp "$PREFIX/LICENSE"* "$OUT/python/" 2>/dev/null || true
[ -f "$PREFIX/lib/python$VER/LICENSE.txt" ] && cp "$PREFIX/lib/python$VER/LICENSE.txt" "$OUT/python/LICENSE.txt"

# The Umbrous runtime: the package and the page it serves, nothing else.
rsync -a --exclude '__pycache__/' --exclude '*.pyc' "$ROOT/umbrous" "$OUT/app/"
cp "$ROOT/prototypes/umbrous-harness.html" "$OUT/app/prototypes/"
cp "$ROOT/LICENSE" "$OUT/app/LICENSE"

"$OUT/python/bin/python3" -E -s -m compileall -q -j 0 --invalidation-mode unchecked-hash \
  "$OUT/python/lib/python$VER" "$OUT/app/umbrous" >/dev/null || {
  echo "compileall reported errors" >&2; exit 1; }

# Sanity check: the bundled interpreter runs the runtime from the bundle only.
(cd "$OUT/app" && env -i HOME="$HOME" "$OUT/python/bin/python3" -E -s -B -m umbrous version)
"$OUT/python/bin/python3" -E -s -B -c 'import sqlite3, ssl, json, http.server; print("sqlite", sqlite3.sqlite_version)'

printf '{"python": "%s", "distribution": "python-build-standalone"}\n' "$FULLVER" > "$OUT/BUILD-INFO.json"
du -sh "$OUT" | awk '{print "Backend bundle: " $1 " in " $2}'
