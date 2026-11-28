#!/bin/bash
# Install OpenClaw Docket for the current user:
#   - builds Docket.app and copies it to ~/Applications
#   - writes a default config if there is none
#   - copies the service's code to ~/Library/Application Support/docket/lib and registers two launchd
#     agents: the service (always on, run from that copy) and the app (at login)
#   - wires OpenClaw, if it is present (scripts/openclaw_setup.py): the plugin, the docket-pm agent,
#     the desk-dispatch skill. A skill or PM file changed by hand is backed up, never lost.
# Re-running it upgrades in place. Data in ~/Library/Application Support/docket is kept.
# DOCKET_SKIP_OPENCLAW=1 leaves OpenClaw alone; scripts/openclaw_setup.py --dry-run shows what it would change.
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

# The service runs from a copy of its code in Application Support, never from this clone:
# launchd's python3 may not read ~/Desktop or ~/Documents (scripts/deploy-service.sh).
bash "$REPO/scripts/deploy-service.sh" --no-restart

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

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
# bootout returns before a job is gone; bootstrapping too early fails with "5: Input/output error".
restart() {
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl print "$DOMAIN/$1" >/dev/null 2>&1 || break
    sleep 0.5
  done
  launchctl bootstrap "$DOMAIN" "$AGENTS/$1.plist" 2>/dev/null || { sleep 2; launchctl bootstrap "$DOMAIN" "$AGENTS/$1.plist"; }
}
for job in "$LABEL" "$LABEL.app"; do
  restart "$job"
done

if [ "${DOCKET_SKIP_OPENCLAW:-0}" != "1" ] && command -v openclaw >/dev/null; then
  say "Wiring OpenClaw: plugin, PM agent, skill"
  if ! /usr/bin/python3 -I "$REPO/scripts/openclaw_setup.py"; then
    echo "OpenClaw was not wired completely; see above, fix it and run: /usr/bin/python3 -I $REPO/scripts/openclaw_setup.py"
  fi
fi

cat <<EOF

OpenClaw Docket is running: the tray icon is in the menu bar, the pill on the right edge (⌥⌘D).

Next:
  1. Add two lines to ~/.openclaw/workspace/AGENTS.md:
       Hand coding work off with the desk-dispatch skill, reply with one line, and leave the rest to Docket.
       When I say more about a handed-off task, pass my words on with docket note and reply with one line.
  2. Optional, the judge (picks a card's background from the chat). Without it cards still arrive.
     Open Settings → Models and either save a TypeSafe key (https://console.typesafe.ai), or switch
     the backend to an OpenAI-compatible language model and fill in its endpoint, model and key.
  3. Check every link: $REPO/scripts/docket doctor

Settings: $DATA/config.json    Logs: $LOGS/daemon.log
EOF
