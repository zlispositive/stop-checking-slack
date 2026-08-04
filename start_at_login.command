#!/bin/bash
# Add THIS file to System Settings -> General -> Login Items to launch the
# tracker automatically when you log in. It runs the app from the project's
# virtualenv (created by setup.command), so it never depends on which python3
# happens to be on your PATH.

set -e
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Virtualenv not found. Run setup.command once first." >&2
  exit 1
fi

exec "$VENV/bin/python" slack_check_tracker.py
