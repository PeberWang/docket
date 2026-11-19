#!/bin/bash
# Remove OpenClaw Docket's launchd agents, the app and the OpenClaw skill.
# Data (ledger, config) stays unless you pass --purge.
set -euo pipefail

LABEL="io.github.peberwang.docket"
DOMAIN="gui/$(id -u)"

for job in "$LABEL.app" "$LABEL"; do
  launchctl bootout "$DOMAIN/$job" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$job.plist"
done
rm -rf "$HOME/Applications/Docket.app" "$HOME/.openclaw/workspace/skills/desk-dispatch"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/Library/Application Support/docket" "$HOME/Library/Logs/docket"
  echo "Removed OpenClaw Docket and its data."
else
  echo "Removed OpenClaw Docket. Data kept in ~/Library/Application Support/docket (use --purge to delete it)."
fi
