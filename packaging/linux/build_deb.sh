#!/usr/bin/env bash
# Build the .deb from the PyInstaller onedir build (run on ubuntu-22.04: glibc 2.35 is the floor).
#
#   uv run pyinstaller --noconfirm packaging/relaybay.spec
#   packaging/linux/build_deb.sh 0.6.0
#
# Output: dist/relaybay_<version>_amd64.deb with /opt/relaybay, the /usr/bin/relaybay symlink, a
# desktop entry and the icon.
set -euo pipefail

version="${1:?usage: build_deb.sh VERSION}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
build="$root/dist/relaybay"
if [ ! -x "$build/relaybay" ]; then
  echo "no PyInstaller build in $build: run pyinstaller packaging/relaybay.spec first" >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
pkg="$stage/relaybay"
install -d "$pkg/DEBIAN" "$pkg/opt" "$pkg/usr/bin" "$pkg/usr/share/applications" \
  "$pkg/usr/share/icons/hicolor/scalable/apps"
cp -a "$build" "$pkg/opt/relaybay"
ln -s /opt/relaybay/relaybay "$pkg/usr/bin/relaybay"
install -m 0644 "$root/packaging/linux/relaybay.desktop" "$pkg/usr/share/applications/"
install -m 0644 "$root/packaging/icons/relaybay.svg" "$pkg/usr/share/icons/hicolor/scalable/apps/"
chmod -R u=rwX,go=rX "$pkg/opt/relaybay"

size="$(du -sk --exclude=DEBIAN "$pkg" | cut -f1)"
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" "$root/packaging/linux/control.in" \
  > "$pkg/DEBIAN/control"

out="$root/dist/relaybay_${version}_amd64.deb"
dpkg-deb --root-owner-group --build "$pkg" "$out"
echo "$out"
