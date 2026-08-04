#!/bin/bash
# Double-click to stop Slack Check Tracker from starting at login and quit it now.

set -e
cd "$(dirname "$0")"

LABEL="com.lingzhang.slack-check-tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

echo "==> Unloading LaunchAgent..."
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# Match on the script filename so copies started with a relative path
# (e.g. from setup.command) are also stopped.
pkill -f "slack_check_tracker.py" 2>/dev/null || true

if [ -f "$PLIST" ]; then
  rm "$PLIST"
  echo "==> Removed $PLIST"
fi

echo "==> Done. The tracker will no longer start at login."
