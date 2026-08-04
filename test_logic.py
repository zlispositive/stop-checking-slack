#!/usr/bin/env python3
"""Headless logic tests for slack_check_tracker.

Stubs out `rumps` and `AppKit` so the counting logic can be driven without a
real menu bar or a real foreground app. Run: .venv/bin/python test_logic.py
"""
import sys
import types
import os
import json
import tempfile

# ---- stub rumps -------------------------------------------------------
rumps = types.ModuleType("rumps")


class _App:
    def __init__(self, *a, **k):
        self.title = None
        self.menu = []

    def run(self):
        pass


class _MenuItem:
    def __init__(self, title="", callback=None):
        self.title = title
        self.callback = callback


class _Timer:
    def __init__(self, cb, interval):
        self.cb = cb
        self.interval = interval

    def start(self):
        pass


rumps.App = _App
rumps.MenuItem = _MenuItem
rumps.Timer = _Timer
rumps.quit_application = lambda: None
sys.modules["rumps"] = rumps

# ---- stub AppKit.NSWorkspace -----------------------------------------
appkit = types.ModuleType("AppKit")

# The test controls what the "frontmost app" is by setting this.
_STATE = {"bundle": None, "name": None}


class _FakeApp:
    def bundleIdentifier(self):
        return _STATE["bundle"]

    def localizedName(self):
        return _STATE["name"]


class _FakeWorkspace:
    def frontmostApplication(self):
        if _STATE["bundle"] is None and _STATE["name"] is None:
            return None
        return _FakeApp()


class NSWorkspace:
    @staticmethod
    def sharedWorkspace():
        return _FakeWorkspace()


appkit.NSWorkspace = NSWorkspace
appkit.NSWorkspaceDidActivateApplicationNotification = "stub"
sys.modules["AppKit"] = appkit


def set_front(app):
    """app: 'slack', 'other', or None (no frontmost app)."""
    if app == "slack":
        _STATE["bundle"] = "com.tinyspeck.slackmacgap"
        _STATE["name"] = "Slack"
    elif app == "other":
        _STATE["bundle"] = "com.apple.Safari"
        _STATE["name"] = "Safari"
    else:
        _STATE["bundle"] = None
        _STATE["name"] = None


def reset_state():
    """Start a scenario from a clean slate (no persisted state on disk)."""
    try:
        os.remove(_state_file)
    except FileNotFoundError:
        pass


# ---- import the module under test (uses a temp state file) -----------
_tmp = tempfile.mkdtemp()
_state_file = os.path.join(_tmp, "state.json")

import slack_check_tracker as sct  # noqa: E402

sct.STATE_PATH = _state_file

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# --- Test 1: launching while a non-Slack app is frontmost, then switching in
reset_state()
set_front("other")
app = sct.SlackCheckTracker()
check("starts at zero", app.count_today == 0)

# switch INTO slack -> one check
set_front("slack")
app.tick(None)
check("switching into Slack counts one", app.count_today == 1)

# staying in slack across ticks does NOT recount
app.tick(None)
app.tick(None)
check("holding Slack open does not recount", app.count_today == 1)

# leave and come back -> second check
set_front("other")
app.tick(None)
set_front("slack")
app.tick(None)
check("leave and re-enter counts again", app.count_today == 2)

# --- Test 2: the phantom-count fix — launch while Slack IS frontmost
reset_state()
set_front("slack")
app2 = sct.SlackCheckTracker()
app2.tick(None)
check("launching with Slack already frontmost does not phantom-count",
      app2.count_today == 0)

# --- Test 3: persistence round-trips within the same day
reset_state()
set_front("other")
app3 = sct.SlackCheckTracker()
set_front("slack")
app3.tick(None)          # count -> 1, writes state
saved = json.load(open(_state_file))
check("state file written on a check", saved["count_today"] >= 1)

app4 = sct.SlackCheckTracker()   # fresh instance re-reads state
check("count restored from disk same day", app4.count_today == saved["count_today"])

# --- Test 4: day rollover resets the count but keeps last_check_ts
app4.today = "1999-01-01"
prev_ts = app4.last_check_ts
set_front("other")
app4.tick(None)          # rolls the day (no rising edge, so no new check)
check("count resets on new day", app4.count_today == 0)
check("last_check_ts survives day rollover", app4.last_check_ts == prev_ts)

# --- Test 5: no frontmost app is treated as "not Slack", never crashes
set_front(None)
app4.tick(None)
check("None frontmost app is safe", app4.count_today == 0)

# --- Test 6: humanize formatting
h = sct.SlackCheckTracker._humanize
check("humanize 30s", h(30) == "30s")
check("humanize 90s", h(90) == "1m")
check("humanize 1h05m", h(3900) == "1h05m")
check("humanize exact hour", h(7200) == "2h")
check("humanize days", h(90000) == "1d")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
