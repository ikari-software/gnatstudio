#!/bin/bash
# Notarize and staple a GNAT Studio .dmg that already contains a signed .app.
#
# The signing of the .app is done by the osx_bundle Makefile when
# CODESIGN_IDENTITY is set. This script handles the remaining steps:
# notarytool submit + stapler staple.
#
# Prerequisites (one-time setup):
#   xcrun notarytool store-credentials "gnatstudio" \
#     --apple-id "your@email.com" \
#     --team-id "YOUR_TEAM_ID" \
#     --password "your-app-specific-password"
#
# Usage (from osx_bundle/):
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" make dmg
#   ./scripts/sign_and_notarize.sh GNATStudio-<version>.dmg

set -e

DMG="${1:-}"
PROFILE="${NOTARY_PROFILE:-gnatstudio}"

if [ -z "$DMG" ] || [ ! -f "$DMG" ]; then
    echo "Usage: $0 <path-to-dmg>" >&2
    echo "  (set NOTARY_PROFILE to override the keychain profile name, default: gnatstudio)" >&2
    exit 1
fi

echo "Notarizing $DMG (profile: $PROFILE)..."
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "Stapling notarization ticket..."
xcrun stapler staple "$DMG"

echo "Verifying stapled ticket..."
xcrun stapler validate "$DMG"

echo "Done: $DMG is notarized and stapled."
