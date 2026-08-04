#!/usr/bin/env python3
"""
Slack Check Tracker — a tiny macOS menu bar app that helps you notice
how often you check Slack, without blocking you.

It watches which app is in the foreground. Every time Slack becomes the
frontmost app (a non-Slack -> Slack transition), that counts as one "check."
The menu bar shows how long it's been since your last check and how many
times you've checked today.

Menu bar looks like:   💬 12m · 5×
    -> last checked 12 minutes ago, 5 checks so far today.

No Slack login, no network, no data leaves your Mac. State is a small JSON
file in ~/.slack_check_tracker.json.

Requires: macOS, Python 3, and the `rumps` and `pyobjc` packages.
    pip install rumps pyobjc-framework-Cocoa
(See setup.command / README for the recommended virtualenv install.)
"""

import json
import os
import time
from datetime import datetime, date

import rumps

# AppKit / NSWorkspace gives us the current frontmost application.
from AppKit import NSWorkspace


STATE_PATH = os.path.expanduser("~/.slack_check_tracker.json")

# How often (seconds) we poll the frontmost app and refresh the title.
POLL_INTERVAL = 2

# Bundle identifier for the Slack desktop app. We match on this first and
# fall back to the localized app name so it works regardless of language.
SLACK_BUNDLE_ID = "com.tinyspeck.slackmacgap"
SLACK_APP_NAME = "Slack"


def _today_str():
    return date.today().isoformat()


class SlackCheckTracker(rumps.App):
    def __init__(self):
        super().__init__("💬 —", quit_button=None)

        # In-memory state, seeded from disk.
        self.count_today = 0
        self.today = _today_str()
        self.last_check_ts = None  # epoch seconds of the most recent check
        # Tracks whether Slack was frontmost on the previous poll, so we only
        # count the *transition* into Slack rather than every poll while it's open.
        self.slack_was_frontmost = False

        self._load_state()

        # Seed the edge detector with whatever is frontmost right now. Without
        # this, launching while Slack is already the active app would read as a
        # False -> True transition on the first tick and log a phantom check.
        self.slack_was_frontmost = self._slack_is_frontmost()

        # Menu items (the title line is the menu bar; these are the dropdown).
        self.detail_item = rumps.MenuItem("Last checked: —")
        self.count_item = rumps.MenuItem("Checks today: 0")
        self.menu = [
            self.detail_item,
            self.count_item,
            None,  # separator
            rumps.MenuItem("Reset today's count", callback=self.reset_count),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Poll on a timer. rumps.Timer fires on the main thread, which is
        # required for both NSWorkspace reads and menu bar title updates.
        self._timer = rumps.Timer(self.tick, POLL_INTERVAL)
        self._timer.start()

        # Render once immediately so we don't show a placeholder for 2s.
        self._refresh_title()

    # ---- persistence -------------------------------------------------

    def _load_state(self):
        try:
            with open(STATE_PATH, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

        saved_day = data.get("today")
        if saved_day == _today_str():
            # Same calendar day: restore the running count and last-check time.
            self.today = saved_day
            self.count_today = int(data.get("count_today", 0))
            self.last_check_ts = data.get("last_check_ts")
        else:
            # New day: start fresh but keep last_check_ts so "time since"
            # is still meaningful across a day boundary.
            self.today = _today_str()
            self.count_today = 0
            self.last_check_ts = data.get("last_check_ts")

    def _save_state(self):
        data = {
            "today": self.today,
            "count_today": self.count_today,
            "last_check_ts": self.last_check_ts,
        }
        try:
            # Write-then-rename for an atomic update.
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass  # never let a disk hiccup crash the menu bar

    # ---- core loop ---------------------------------------------------

    def _slack_is_frontmost(self):
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if app is None:
            return False
        bundle_id = app.bundleIdentifier()
        if bundle_id == SLACK_BUNDLE_ID:
            return True
        # Fallback for edge cases where the bundle id isn't reported.
        return app.localizedName() == SLACK_APP_NAME

    def _roll_day_if_needed(self):
        now_day = _today_str()
        if now_day != self.today:
            self.today = now_day
            self.count_today = 0

    def tick(self, _timer):
        self._roll_day_if_needed()

        is_front = self._slack_is_frontmost()
        # Count only the rising edge: was NOT Slack, now IS Slack.
        if is_front and not self.slack_was_frontmost:
            self.count_today += 1
            self.last_check_ts = time.time()
            self._save_state()
        self.slack_was_frontmost = is_front

        self._refresh_title()

    # ---- rendering ---------------------------------------------------

    @staticmethod
    def _humanize(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        rem_min = minutes % 60
        if hours < 24:
            return f"{hours}h{rem_min:02d}m" if rem_min else f"{hours}h"
        days = hours // 24
        return f"{days}d"

    def _refresh_title(self):
        if self.last_check_ts is None:
            self.title = f"💬 — · {self.count_today}×"
            self.detail_item.title = "Last checked: not yet today"
        else:
            ago = self._humanize(time.time() - self.last_check_ts)
            self.title = f"💬 {ago} · {self.count_today}×"
            when = datetime.fromtimestamp(self.last_check_ts).strftime("%-I:%M %p")
            self.detail_item.title = f"Last checked: {ago} ago (at {when})"
        self.count_item.title = f"Checks today: {self.count_today}"

    # ---- menu actions ------------------------------------------------

    def reset_count(self, _sender):
        self.count_today = 0
        self._save_state()
        self._refresh_title()

    def quit_app(self, _sender):
        self._save_state()
        rumps.quit_application()


if __name__ == "__main__":
    SlackCheckTracker().run()
