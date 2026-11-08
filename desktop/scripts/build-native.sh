#!/bin/sh
# Build Umbrous.app (native GPUI interface) and Umbrous_<version>_aarch64.dmg.
#
#   desktop/scripts/build-native.sh          release build, ad-hoc signed (runs on this Mac)
#   desktop/scripts/build-native.sh --e2e    test build with the automation driver (never ship it)
#
# Needs: Rust (rustup) with the Xcode Command Line Tools, and uv (for the bundled
# Python). Full Xcode is not needed: GPUI's Metal shaders are compiled at launch.
# Distribution signing: set UMBROUS_SIGN_IDENTITY to a "Developer ID Application"
# identity (see docs/desktop.md, "Distribution").
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APPDIR="$ROOT/desktop/app"
OUT="$ROOT/dist"
FEATURES=""
NAME="Umbrous"
if [ "${1:-}" = "--e2e" ]; then FEATURES="--features e2e"; NAME="Umbrous-e2e"; fi
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$APPDIR/Cargo.toml" | head -1)"
IDENTITY="${UMBROUS_SIGN_IDENTITY:--}"

"$ROOT/desktop/scripts/build-backend.sh"

# shellcheck disable=SC2086
(cd "$APPDIR" && cargo build --release --locked $FEATURES)

APP="$OUT/$NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$APPDIR/target/release/umbrous-desktop" "$APP/Contents/MacOS/Umbrous"
cp "$APPDIR/assets/icon.icns" "$APP/Contents/Resources/Umbrous.icns"
mkdir -p "$APP/Contents/Resources/licenses"
cp "$APPDIR/assets/moicons/LICENSE.txt" "$APP/Contents/Resources/licenses/Moicons.txt"
cp "$APPDIR/assets/moicons/TABLER-LICENSE.txt" "$APP/Contents/Resources/licenses/Tabler.txt"
ditto "$ROOT/build/desktop-backend" "$APP/Contents/Resources/backend"
sed -e "s/@VERSION@/$VERSION/g" "$APPDIR/assets/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Sign inside-out: every Mach-O in the bundled Python, then the app.
# Hardened runtime everywhere; no entitlements (desktop/app/assets/Entitlements.plist is empty).
TS=""; [ "$IDENTITY" != "-" ] && TS="--timestamp"
find "$APP/Contents/Resources/backend" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) | while read -r f; do
  if file "$f" | grep -q 'Mach-O'; then
    codesign --force --options runtime $TS --sign "$IDENTITY" "$f"
  fi
done
codesign --force --options runtime $TS --entitlements "$APPDIR/assets/Entitlements.plist" --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"

if [ -z "$FEATURES" ]; then
  DMG="$OUT/${NAME}_${VERSION}_aarch64.dmg"
  STAGE="$(mktemp -d)"
  ditto "$APP" "$STAGE/$NAME.app"
  ln -s /Applications "$STAGE/Applications"
  rm -f "$DMG"
  hdiutil create -quiet -volname "$NAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO "$DMG"
  rm -rf "$STAGE"
  [ "$IDENTITY" != "-" ] && codesign --force $TS --sign "$IDENTITY" "$DMG"
  echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"
fi
echo "App: $APP ($(du -sh "$APP" | cut -f1))"
codesign -dv "$APP" 2>&1 | grep -E '^(Signature|TeamIdentifier|CodeDirectory)'
