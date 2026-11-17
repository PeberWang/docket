#!/bin/bash
# Remove OpenClaw Desk's launchd agents, the app and the OpenClaw skill.
# Data (ledger, config) stays unless you pass --purge.
set -euo pipefail

LABEL="io.github.peberwang.openclaw-desk"
DOMAIN="gui/$(id -u)"

for job in "$LABEL.app" "$LABEL"; do
  launchctl bootout "$DOMAIN/$job" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$job.plist"
done
rm -rf "$HOME/Applications/Desk.app" "$HOME/.openclaw/workspace/skills/desk-dispatch"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/Library/Application Support/openclaw-desk" "$HOME/Library/Logs/openclaw-desk"
  echo "Removed OpenClaw Desk and its data."
else
  echo "Removed OpenClaw Desk. Data kept in ~/Library/Application Support/openclaw-desk (use --purge to delete it)."
fi
