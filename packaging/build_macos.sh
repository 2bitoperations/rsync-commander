#!/bin/sh
# Build dist/rsync-commander.app and dist/rsync-commander-<version>-macos-<arch>.dmg
#
#   packaging/build_macos.sh
#
# Signing: ad-hoc by default (runs on this Mac; other Macs need right-click >
# Open the first time). For a distributable build set:
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE=<profile>   # created once: xcrun notarytool store-credentials
set -eu
cd "$(dirname "$0")/.."
VERSION=$(uv run --no-project python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
ARCH=$(uname -m)
APP=dist/rsync-commander.app
DMG=dist/rsync-commander-$VERSION-macos-$ARCH.dmg

echo "==> PyInstaller ($VERSION, $ARCH)"
rm -rf build dist
uv run --group build --group gui pyinstaller --noconfirm --clean packaging/rsync-commander.spec

echo "==> codesign"
ID="${CODESIGN_IDENTITY:--}"
if [ "$ID" = "-" ]; then
    codesign --force --deep --sign - "$APP"
else
    codesign --force --deep --options runtime --timestamp --sign "$ID" "$APP"
fi
codesign --verify --deep --strict "$APP"

echo "==> smoke test"
"$APP/Contents/MacOS/rsync-commander" --version
"$APP/Contents/MacOS/rsync-commander" copy --help >/dev/null

echo "==> dmg"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/README.txt" <<EOF
rsync-commander $VERSION

Drag rsync-commander to Applications.

Command line (tui, batch, copy, ...): after installing, run
  /Applications/rsync-commander.app/Contents/MacOS/rsync-commander install-cli
to put \`rsync-commander\` in ~/.local/bin.
EOF
hdiutil create -quiet -volname "rsync-commander $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
if [ "$ID" != "-" ]; then
    codesign --force --timestamp --sign "$ID" "$DMG"
    if [ -n "${NOTARY_PROFILE:-}" ]; then
        echo "==> notarize"
        xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
        xcrun stapler staple "$DMG"
    fi
fi
du -sh "$APP" "$DMG"
echo "built $DMG"
