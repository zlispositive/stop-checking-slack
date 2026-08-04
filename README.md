# Slack Check Tracker

A tiny macOS **menu bar** app that shows how long it's been since you last
checked Slack, and how many times you've checked today. No blocking — just
a gentle mirror of the habit.

Menu bar reads:  `💬 12m · 5×`  → last checked 12 min ago, 5 checks today.

## How it detects a "check"

It watches which app is in the foreground. Each time **Slack becomes the
frontmost app**, that's one check. Holding Slack open doesn't inflate the
count — only switching *into* it does. Nothing touches your Slack account,
nothing goes over the network, and state lives in `~/.slack_check_tracker.json`.

## Quick start

Double-click **`setup.command`** in Finder. It creates a virtualenv, installs
the two dependencies, and launches the app. Look for the 💬 icon in your menu
bar. That's it.

> The first time you double-click a `.command` file, macOS Gatekeeper may
> refuse to run it. If so: right-click the file → **Open** → **Open**, or
> allow it under System Settings → Privacy & Security.

## Manual setup (equivalent to the script)

Recent Python installs refuse `pip install` into the system interpreter
(PEP 668, `externally-managed-environment`), so this project uses a
virtualenv:

```bash
cd slack-check-tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python slack_check_tracker.py
```

Click the 💬 menu bar item for details, to reset today's count, or to quit.

> Reading the frontmost app name via NSWorkspace does not require Accessibility
> permission, so first launch should not prompt. If macOS ever does, grant it
> under System Settings → Privacy & Security.

## Start automatically at login (optional)

1. Run `setup.command` once (creates the `.venv`).
2. System Settings → General → Login Items → **+** → add
   **`start_at_login.command`** from this folder.

`start_at_login.command` runs the app from the project's virtualenv, so it
does not depend on which `python3` is on your PATH.

## Tuning

Open `slack_check_tracker.py` and edit near the top:

- `POLL_INTERVAL` — how often it checks the foreground app (default 2s).
- The menu bar format lives in `_refresh_title()` if you want a different look.
