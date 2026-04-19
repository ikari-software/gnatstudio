#!/bin/bash
# Build a drag-to-Applications style .dmg wrapping a macOS .app bundle.
#
# Usage:
#   dodmg.sh <AppName> <Version> <SrcDir> <AppBundlePath> <OutputDmgPath>

set -e

APP_NAME="$1"
VERSION="$2"
SRCDIR="$3"
APP_BUNDLE="$4"
DMG="$5"

if [ -z "$APP_NAME" ] || [ -z "$VERSION" ] || [ -z "$SRCDIR" ] \
   || [ -z "$APP_BUNDLE" ] || [ -z "$DMG" ]; then
    echo "Usage: $0 <AppName> <Version> <SrcDir> <AppBundle> <OutputDmg>" >&2
    exit 1
fi

if [ ! -d "$APP_BUNDLE" ]; then
    echo "ERROR: app bundle not found: $APP_BUNDLE" >&2
    exit 1
fi

VOL_NAME="$APP_NAME $VERSION"
BG_NAME="logo.png"

echo "=== Preparing dmg staging folder"
STAGE=$(mktemp -d -t gnatstudio-dmg.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT

cp -R "$APP_BUNDLE" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

if [ -f "$SRCDIR/srcs/$BG_NAME" ]; then
    mkdir "$STAGE/.bg"
    cp "$SRCDIR/srcs/$BG_NAME" "$STAGE/.bg/"
fi

echo "=== Creating read-write dmg"
TMPDMG=$(mktemp "${TMPDIR:-/tmp}/gnatstudio-dmg-temp.XXXXXX").dmg
rm -f "$TMPDMG"
hdiutil create -srcfolder "$STAGE" -volname "$VOL_NAME" -fs HFS+ \
    -fsargs "-c c=64,a=16,e=16" -format UDRW -ov "$TMPDMG"

echo "=== Mounting dmg to tweak Finder view"
device=$(hdiutil attach -readwrite -noverify -noautoopen "$TMPDMG" | \
         egrep '^/dev/' | sed 1q | awk '{print $1}')
if [ -z "$device" ]; then
    echo "ERROR: could not mount dmg" >&2
    exit 1
fi
volume=$(mount | grep "$device" | sed -e 's^.* on /Volumes/\(.*\) (.*^\1^')

# The Finder-view step is cosmetic. If System Settings > Privacy & Security >
# Automation doesn't grant Terminal permission to drive Finder, osascript
# fails but we still produce a working dmg -- so don't abort on failure.
osascript <<EOF || echo "(warning) Finder view tweak failed -- dmg layout will be default"
tell application "Finder"
  tell disk "$volume"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set bounds of container window to {400, 100, 900, 450}
    set theViewOptions to the icon view options of container window
    set arrangement of theViewOptions to not arranged
    set icon size of theViewOptions to 96
    try
      set background picture of theViewOptions to file ".bg:$BG_NAME"
    end try
    set position of item "$APP_NAME.app" of container window to {120, 180}
    set position of item "Applications" of container window to {380, 180}
    update without registering applications
    close
  end tell
end tell
EOF

sync
chmod -Rf go-w "/Volumes/$volume" 2>/dev/null || true
hdiutil detach "$device"

echo "=== Converting to compressed read-only dmg"
rm -f "$DMG"
hdiutil convert -ov -format UDZO -imagekey zlib-level=9 "$TMPDMG" -o "$DMG"
rm -f "$TMPDMG"

echo "=== Done: $DMG"
