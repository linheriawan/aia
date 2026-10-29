#!/usr/bin/env bash
# Packages dist/deskai.app into a .dmg (ARCHITECTURE.md §14).
#
# Run after `pyinstaller packaging/deskai.spec`, from the repo root:
#   bash packaging/macos/build_dmg.sh
#
# Signing/notarization are optional in v1 — once a Developer ID certificate
# exists, codesign deskai.app (with packaging/macos/entitlements.plist) and
# notarize it before this step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
APP="$ROOT/dist/deskai.app"
ARCH="$(uname -m)"
OUT_DIR="$ROOT/dist/installer"
DMG="$OUT_DIR/deskai-$VERSION-macos-$ARCH.dmg"

if [ ! -d "$APP" ]; then
  echo "error: $APP not found — run 'pyinstaller packaging/deskai.spec' first" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$DMG"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "deskai" -srcfolder "$STAGING" -ov -format UDZO "$DMG"
echo "Wrote $DMG"
