#!/bin/sh
# Build dist/rsync-commander-<version>-linux-<arch>.AppImage and a .tar.gz of
# the same onedir bundle. Run on Linux (CI uses Ubuntu 22.04 for an old glibc).
#
# Qt needs some xcb/system libs at build time so PyInstaller can bundle them:
#   sudo apt-get install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 \
#     libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
#     libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libegl1 libfontconfig1 libdbus-1-3
set -eu
cd "$(dirname "$0")/.."
VERSION=$(uv run --no-project python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
ARCH=$(uname -m)
NAME=rsync-commander-$VERSION-linux-$ARCH

echo "==> PyInstaller ($VERSION, $ARCH)"
rm -rf build dist
uv run --group build --group gui pyinstaller --noconfirm --clean packaging/rsync-commander.spec

echo "==> smoke test"
dist/rsync-commander/rsync-commander --version
TMP=$(mktemp -d)
mkdir -p "$TMP/src/sub" && head -c 3000000 /dev/urandom > "$TMP/src/sub/a.bin"
dist/rsync-commander/rsync-commander copy "$TMP/src" "$TMP/dst" >/dev/null
cmp "$TMP/src/sub/a.bin" "$TMP/dst/src/sub/a.bin"
QT_QPA_PLATFORM=offscreen uv run --group gui python -c \
  "import sys;from PySide6.QtGui import QGuiApplication;a=QGuiApplication(sys.argv);\
from rcommander.gui.icon import render;render(256).save('$TMP/icon.png')"

echo "==> AppImage"
APPDIR=build/AppDir
rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/bin"
cp -a dist/rsync-commander/. "$APPDIR/usr/bin/"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/bin/rsync-commander" "$@"
EOF
chmod +x "$APPDIR/AppRun"
cp "$TMP/icon.png" "$APPDIR/rsync-commander.png"
cat > "$APPDIR/rsync-commander.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=rsync-commander
Comment=Dual-panel file commander with block-level ssh sync
Exec=rsync-commander
Icon=rsync-commander
Categories=Utility;Network;
Terminal=false
X-AppImage-Version=$VERSION
EOF
TOOL=build/appimagetool
if [ ! -x "$TOOL" ]; then
    curl -sSfL -o "$TOOL" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$TOOL"
fi
# CI runners have no FUSE: let appimagetool run extracted
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=$ARCH "$TOOL" --no-appstream "$APPDIR" "dist/$NAME.AppImage"
APPIMAGE_EXTRACT_AND_RUN=1 "dist/$NAME.AppImage" --version

echo "==> tarball"
tar -czf "dist/$NAME.tar.gz" -C dist rsync-commander
rm -rf "$TMP"
du -sh "dist/$NAME.AppImage" "dist/$NAME.tar.gz"
