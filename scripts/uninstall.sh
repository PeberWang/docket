#!/bin/bash
# Remove OpenClaw Docket: the two launchd agents, the app, the OpenClaw skill and the settings
# (config.json and the app's preferences). The ledger of handoffs stays unless you pass --purge.
set -euo pipefail

LABEL="io.github.peberwang.docket"
DOMAIN="gui/$(id -u)"
DATA="$HOME/Library/Application Support/docket"
LOGS="$HOME/Library/Logs/docket"

for job in "$LABEL.app" "$LABEL"; do
  launchctl bootout "$DOMAIN/$job" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$job.plist"
done
if command -v openclaw >/dev/null; then
  /usr/bin/python3 -I "$(cd "$(dirname "$0")" && pwd)/openclaw_setup.py" --remove || echo "OpenClaw was not changed; unlink the plugin with: openclaw plugins uninstall docket"
fi
rm -rf "$HOME/Applications/Docket.app" "$HOME/.openclaw/workspace/skills/desk-dispatch" "$DATA/lib"  # pm-workspace stays with the PM agent
rm -f "$DATA/config.json" "$DATA/daemon.json"
defaults delete "$LABEL" 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA" "$LOGS"
  echo "Removed OpenClaw Docket and its data."
else
  echo "Removed OpenClaw Docket and its settings."
  echo "Your handoff history is kept in: $DATA"
  echo "Logs are kept in:                $LOGS"
  echo "Run again with --purge to delete both."
fi
echo "A judge key saved from Settings stays in the keychain; remove it with:"
echo "  security delete-generic-password -s docket.judge -a systemone   (or -a openai)"
