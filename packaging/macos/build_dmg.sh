#!/usr/bin/env bash
# Build the .dmg from the PyInstaller bundle (macOS, arm64).
#
#   uv run pyinstaller --noconfirm packaging/relaybay.spec
#   packaging/macos/build_dmg.sh 0.6.0
#
# Output: dist/relaybay-<version>-macos-arm64.dmg with relaybay.app and an Applications link to
# drag it onto. The app is ad-hoc signed by PyInstaller; it is not notarized.
set -euo pipefail

version="${1:?usage: build_dmg.sh VERSION}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
app="$root/dist/relaybay.app"
if [ ! -d "$app" ]; then
  echo "no bundle at $app: run pyinstaller packaging/relaybay.spec first" >&2
  exit 1
fi
codesign --verify --deep --strict "$app"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R "$app" "$stage/"
ln -s /Applications "$stage/Applications"

out="$root/dist/relaybay-${version}-macos-arm64.dmg"
hdiutil create -volname "relaybay $version" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$out"
echo "$out"
