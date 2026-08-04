#!/bin/bash
# Double-click this file in Finder to set up and launch Slack Check Tracker.
# It creates a self-contained virtualenv next to this script, installs the two
# dependencies into it (once), and starts the menu bar app.
#
# A virtualenv is used because recent Python installs (Homebrew, python.org)
# refuse "pip install" into the system interpreter (PEP 668,
# "externally-managed-environment"). The venv sidesteps that and keeps this
# project's packages isolated from the rest of your machine.

set -e

# Run from the folder this script lives in, so it finds the .py next to it.
cd "$(dirname "$0")"

VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtualenv in $(pwd)/$VENV ..."
  python3 -m venv "$VENV"
fi

echo "==> Installing dependencies (rumps, pyobjc-framework-Cocoa)..."
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r requirements.txt

echo "==> Launching Slack Check Tracker..."
echo "    Look for the 💬 icon in your menu bar (top-right of the screen)."
echo "    You can close this Terminal window once the icon appears."

exec "$VENV/bin/python" slack_check_tracker.py
