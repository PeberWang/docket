#!/bin/bash
# Install OpenClaw Docket for the current user:
#   - builds Docket.app and copies it to ~/Applications
#   - writes a default config if there is none
#   - registers two launchd agents: the service (always on) and the app (at login)
#   - installs the desk-dispatch skill into OpenClaw, if OpenClaw is present
# Re-running it upgrades in place. Data in ~/Library/Application Support/docket is kept.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="io.github.peberwang.docket"
DATA="$HOME/Library/Application Support/docket"
LOGS="$HOME/Library/Logs/docket"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

command -v swift >/dev/null || { echo "Swift is needed: xcode-select --install"; exit 1; }
[ -x /usr/bin/python3 ] || { echo "/usr/bin/python3 is needed: xcode-select --install"; exit 1; }
command -v openclaw >/dev/null || echo "warning: openclaw is not on PATH; the PM needs it"

say "Building the app"
bash "$REPO/app/build.sh" >/dev/null
mkdir -p "$HOME/Applications" "$DATA" "$LOGS" "$AGENTS"
rm -rf "$HOME/Applications/Docket.app"
cp -R "$REPO/app/build/Docket.app" "$HOME/Applications/Docket.app"

if [ ! -f "$DATA/config.json" ]; then
  printf '{\n  "watch": ["*"],\n  "locale": "auto",\n  "engine": "opencode"\n}\n' > "$DATA/config.json"
  say "Wrote $DATA/config.json"
fi

# launchd starts with a bare PATH: give it the directories of the CLIs Docket calls.
path="/usr/bin:/bin:/usr/sbin:/sbin"
for tool in openclaw opencode claude codex node; do
  if found="$(command -v "$tool" 2>/dev/null)"; then
    dir="$(dirname "$found")"
    case ":$path:" in *":$dir:"*) ;; *) path="$dir:$path" ;; esac
  fi
done

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-I</string>
    <string>$(xml "$REPO/docket/daemon.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$REPO")</string>
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

cat > "$AGENTS/$LABEL.app.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL.app</string>
  <key>ProgramArguments</key>
  <array><string>$(xml "$HOME/Applications/Docket.app/Contents/MacOS/Docket")</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF

say "Starting the service and the app"
for job in "$LABEL" "$LABEL.app"; do
  launchctl bootout "$DOMAIN/$job" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$job.plist"
done

if [ -d "$HOME/.openclaw/workspace/skills" ]; then
  rm -rf "$HOME/.openclaw/workspace/skills/desk-dispatch"
  cp -R "$REPO/integrations/openclaw/desk-dispatch" "$HOME/.openclaw/workspace/skills/desk-dispatch"
  chmod +x "$HOME/.openclaw/workspace/skills/desk-dispatch/scripts/desk-dispatch"
  ln -sf "$REPO/scripts/docket" "$HOME/.openclaw/workspace/skills/desk-dispatch/scripts/docket"
  say "Installed the OpenClaw skill desk-dispatch"
fi

cat <<EOF

OpenClaw Docket is running: the tray icon is in the menu bar, the pill on the right edge (⌥⌘D).

Next:
  1. Tell OpenClaw to hand off through Docket. Add to ~/.openclaw/workspace/AGENTS.md:
       Hand coding work off with the desk-dispatch skill, reply with one line, and leave the rest to Docket.
  2. Optional, the judge (picks a card's background from the chat). Without it cards still arrive.
     Open Settings → Models and either save a TypeSafe key (https://console.typesafe.ai), or switch
     the backend to an OpenAI-compatible language model and fill in its endpoint, model and key.
  3. For OpenCode: install the opencode-openclaw-inbox plugin (0.4.0 or later), which provides
     opencode-dispatch and opens sessions for Docket.

Settings: $DATA/config.json    Logs: $LOGS/daemon.log
EOF
