#!/bin/bash
# Double-click to make Slack Check Tracker start automatically at login.
#
# Installs a per-user LaunchAgent that runs the app from this project's
# virtualenv. It starts at login, and relaunches if it ever crashes — but
# stays down when you Quit it from the menu (a clean exit is respected).
#
# Re-run this any time you move the project folder; it rewrites the paths.

set -e
cd "$(dirname "$0")"

LABEL="com.stop-checking-slack.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT_DIR="$(pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
APP="$PROJECT_DIR/slack_check_tracker.py"
LOG="$HOME/Library/Logs/slack-check-tracker.log"
UID_NUM="$(id -u)"

if [ ! -x "$VENV_PY" ]; then
  echo "Virtualenv not found. Run setup.command once first." >&2
  exit 1
fi

echo "==> Stopping any running copy so we don't end up with two menu bar icons..."
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# Match on the script filename, not the full path: a copy started with a
# relative path (e.g. from setup.command) won't match an absolute pattern.
pkill -f "slack_check_tracker.py" 2>/dev/null || true

echo "==> Writing LaunchAgent: $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PY</string>
        <string>$APP</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

echo "==> Loading and starting it..."
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart "gui/$UID_NUM/$LABEL"

echo "==> Done. 💬 should be in your menu bar, and it will start at every login."
echo "    Log: $LOG"
echo "    To undo: run uninstall_autostart.command"
