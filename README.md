# Slack Check + Work Session Tracker

A tiny macOS **menu bar** app that shows how long it's been since you last
checked Slack, how many times you've checked today, and how much active work
time you have accumulated. No blocking — just a gentle mirror of the habits.

Menu bar reads: `🪑1h05m  💬12m·5×` → working for 1 hour 5 minutes, last
checked Slack 12 minutes ago, 5 checks today.

## Automatic work tracking

No start or pause button is needed. Work time counts automatically when:

- **Visual Studio Code** or **cmux** is the frontmost app, and
- keyboard or mouse input occurred within the last 60 seconds.

Chrome and every other app are excluded, even while you are actively typing or
moving the mouse. Switching back to VS Code or cmux resumes counting. Being
idle for 60 seconds, sleeping the Mac, or quitting the app pauses counting.
Click the menu-bar item to see why tracking is paused or to reset accumulated
work time.

The timer reads macOS's local `IOHIDSystem` idle time and the frontmost app.
It does not capture keystrokes, mouse positions, window contents, or network
data, and it does not require Accessibility permission.

Both trackers share one compact menu-bar item. This uses less space and
prevents one tracker from being hidden while the other remains.

macOS does not provide apps with a public API for forcing one status item ahead
of others. To give this tracker precedence, hold **Command (⌘)** and drag the
combined item to the far right of the menu bar. Its position is saved and
restored on future launches.

## How it detects a "check"

It watches which app is in the foreground. Each time **Slack becomes the
frontmost app**, that's one check. Holding Slack open doesn't inflate the
count — only switching *into* it does. Nothing touches your Slack account,
nothing goes over the network, and state lives in `~/.slack_check_tracker.json`.

## Quick start

Double-click **`setup.command`** in Finder. It creates a virtualenv, installs
the two dependencies, and launches the app. Look for the combined 🪑/💬 item
in your menu bar. That's it.

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

Click the combined 🪑/💬 menu bar item for details and controls.

> Reading the frontmost app name via NSWorkspace does not require Accessibility
> permission, so first launch should not prompt.

## Start automatically at login

Double-click **`install_autostart.command`**. It installs a per-user
LaunchAgent (`~/Library/LaunchAgents/com.stop-checking-slack.tracker.plist`)
that:

- starts the tracker at every login,
- relaunches it if it ever crashes,
- but stays down when you Quit it from the menu (a clean exit is respected).

Re-running the installer is safe — it stops any existing copy first, so you
never end up with two menu bar icons. Re-run it if you move the project folder;
it rewrites the paths.

To turn it off, double-click **`uninstall_autostart.command`** (unloads the
agent, removes the plist, and quits the running app).

Logs go to `~/Library/Logs/slack-check-tracker.log`.

### Lightweight alternative (Login Items)

If you'd rather not use a LaunchAgent: run `setup.command` once, then add
**`start_at_login.command`** under System Settings → General → Login Items.
It runs the app from the project's virtualenv, so it doesn't depend on which
`python3` is on your PATH. (No crash-relaunch with this method.)

## Tuning

Open `slack_check_tracker.py` and edit near the top:

- `POLL_INTERVAL` — how often it checks the foreground app (default 2s).
- `INPUT_IDLE_THRESHOLD` — how long to keep counting without keyboard or mouse
  input (default 60s).
- `WORK_APP_BUNDLE_IDS` — apps whose frontmost time can count as work.
- The compact combined menu bar format lives in `_refresh_title()` if you want
  a different look.
