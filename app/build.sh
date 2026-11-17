#!/bin/bash
# Build Desk.app (OpenClaw Desk; the bundle stays Desk.app because launchd points at that path): swift build -c release, assemble build/Desk.app, ad-hoc sign.
set -euo pipefail

cd "$(dirname "$0")"

swift build -c release
BIN_DIR="$(swift build -c release --show-bin-path)"

APP="build/Desk.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/Desk" "$APP/Contents/MacOS/Desk"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleLocalizations</key>
    <array>
        <string>en</string>
        <string>zh-Hans</string>
    </array>
    <key>CFBundleExecutable</key>
    <string>Desk</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.peberwang.openclaw-desk</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>OpenClaw Desk</string>
    <key>CFBundleDisplayName</key>
    <string>OpenClaw Desk</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST

codesign -s - --force --deep "$APP"

echo "$(pwd)/$APP"
