#!/bin/bash
# Copy the service's code into ~/Library/Application Support/docket/lib and run it from there.
#
# launchd's python3 needs macOS's permission to read ~/Desktop or ~/Documents, and that permission
# can be withdrawn or never granted: a service run from a clone there fails with "Operation not
# permitted" (seen 2026-11-27). From Application Support it needs none, and the clone can move or
# change without touching the running service. Run it after pulling a new version; install.sh runs it.
#
#   scripts/deploy-service.sh            sync the code, point the launch agent at it, restart the service
#   scripts/deploy-service.sh --no-restart
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="io.github.peberwang.docket"
DATA="$HOME/Library/Application Support/docket"
LIB="$DATA/lib"
LOGS="$HOME/Library/Logs/docket"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$LIB" "$LOGS" "$(dirname "$PLIST")"
for package in docket judge; do
  rsync -a --delete --exclude "__pycache__" "$REPO/$package/" "$LIB/$package/"
done
git -C "$REPO" rev-parse --short HEAD > "$LIB/VERSION" 2>/dev/null || echo "unknown" > "$LIB/VERSION"

# launchd starts with a bare PATH: give it the directories of the CLIs Docket calls.
path="/usr/bin:/bin:/usr/sbin:/sbin"
for tool in openclaw opencode claude codex node; do
  if found="$(command -v "$tool" 2>/dev/null)"; then
    dir="$(dirname "$found")"
    case ":$path:" in *":$dir:"*) ;; *) path="$dir:$path" ;; esac
  fi
done

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-I</string>
    <string>$(xml "$LIB/docket/daemon.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$LIB")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml "$path")</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$(xml "$LOGS/daemon.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOGS/daemon.log")</string>
</dict>
</plist>
EOF

if [ "${1:-}" != "--no-restart" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
fi
echo "service code $(cat "$LIB/VERSION") in $LIB"
