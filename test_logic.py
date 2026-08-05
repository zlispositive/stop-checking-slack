#!/usr/bin/env python3
"""Headless logic tests for slack_check_tracker.

Stubs out `rumps` and `AppKit` so the Slack and work-session logic can be
driven without a real menu bar or foreground app.
Run: .venv/bin/python test_logic.py
"""
import sys
import types
import os
import json
import io
import tempfile
from contextlib import redirect_stderr

# ---- stub rumps -------------------------------------------------------
rumps = types.ModuleType("rumps")
_RUN_HOOK = None


class _EventEmitter:
    def __init__(self):
        self.callbacks = set()

    def register(self, callback):
        self.callbacks.add(callback)

    def unregister(self, callback):
        self.callbacks.discard(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class _App:
    def __init__(self, *a, **k):
        self.title = None
        self.menu = []

    def run(self, **_options):
        if _RUN_HOOK is not None:
            _RUN_HOOK(self)


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
rumps.events = types.SimpleNamespace(
    before_start=_EventEmitter(),
    on_sleep=_EventEmitter(),
    before_quit=_EventEmitter(),
)
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
    """Set the fake frontmost app for Slack and work-tracking scenarios."""
    if app == "slack":
        _STATE["bundle"] = "com.tinyspeck.slackmacgap"
        _STATE["name"] = "Slack"
    elif app == "vscode":
        _STATE["bundle"] = "com.microsoft.VSCode"
        _STATE["name"] = "Visual Studio Code"
    elif app == "cmux":
        _STATE["bundle"] = "com.cmuxterm.app"
        _STATE["name"] = "cmux"
    elif app == "chrome":
        _STATE["bundle"] = "com.google.Chrome"
        _STATE["name"] = "Google Chrome"
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

# --- Test 7: legacy manual-session state migrates to paused auto time
reset_state()
with open(_state_file, "w") as f:
    json.dump({
        "today": sct._today_str(),
        "count_today": 3,
        "last_check_ts": 123,
        "work_elapsed_seconds": 25,
        "work_started_ts": 100,
    }, f)
set_front("chrome")
legacy_app = sct.SlackCheckTracker()
check("legacy state restores Slack count", legacy_app.count_today == 3)
check("legacy elapsed work restores without counting downtime",
      legacy_app.work_elapsed_seconds == 25
      and legacy_app.work_started_monotonic is None)

# --- Test 8: malformed work-session values are ignored safely
with open(_state_file, "w") as f:
    json.dump({
        "today": sct._today_str(),
        "count_today": 3,
        "work_elapsed_seconds": float("inf"),
        "work_started_ts": True,
    }, f)
malformed_app = sct.SlackCheckTracker()
check("non-finite work state is rejected",
      malformed_app.work_elapsed_seconds == 0)

with open(_state_file, "w") as f:
    json.dump({
        "today": sct._today_str(),
        "count_today": 3,
        "work_elapsed_seconds": 10 ** 1000,
    }, f)
huge_value_app = sct.SlackCheckTracker()
check("overflow-sized work state is rejected",
      huge_value_app.work_elapsed_seconds == 0)

# --- Test 9: IOHID idle output is parsed as seconds
real_subprocess_run = sct.subprocess.run
captured_command = []


def fake_subprocess_run(command, **_kwargs):
    captured_command.extend(command)
    return types.SimpleNamespace(stdout=b'    "HIDIdleTime" = 2500000000\n')


sct.subprocess.run = fake_subprocess_run
try:
    check("HID idle nanoseconds convert to seconds",
          sct._read_input_idle_seconds() == 2.5)
    check("activity probe uses absolute ioreg path",
          captured_command[0] == "/usr/sbin/ioreg")
finally:
    sct.subprocess.run = real_subprocess_run

# --- Test 10: automatic work tracking follows app and recent input
reset_state()
set_front("chrome")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
clock = [1000.0]
idle = [1.0]
probe_error = [False]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]


def fake_idle_reader():
    if probe_error[0]:
        raise OSError("probe failed")
    return idle[0]


sct._read_input_idle_seconds = fake_idle_reader
try:
    work_app = sct.SlackCheckTracker()
    check("Chrome does not start work tracking",
          work_app.work_started_monotonic is None
          and work_app.title == "🪑—  💬—·0×")
    check("Chrome pause reason is visible",
          "Google Chrome is not counted" in work_app.work_detail_item.title)

    set_front("vscode")
    work_app.tick(None)
    check("VS Code with recent input starts automatically",
          work_app.work_started_monotonic == clock[0]
          and work_app.title == "🪑0m  💬—·0×")
    clock[0] += 65
    work_app.tick(None)
    check("VS Code work time advances",
          work_app._current_work_elapsed() == 65
          and work_app.title == "🪑1m  💬—·0×")

    set_front("chrome")
    work_app.tick(None)
    clock[0] += 300
    work_app.tick(None)
    check("Chrome time is excluded",
          work_app.work_started_monotonic is None
          and work_app._current_work_elapsed() == 65)

    set_front("cmux")
    work_app.tick(None)
    clock[0] += 30
    work_app.tick(None)
    check("cmux resumes automatic work tracking",
          work_app._current_work_elapsed() == 95
          and "counting cmux" in work_app.work_detail_item.title)

    idle[0] = 61
    clock[0] += sct.INPUT_PROBE_INTERVAL
    work_app.tick(None)
    check("input idle threshold pauses work tracking",
          work_app.work_started_monotonic is None
          and work_app._current_work_elapsed() == 100
          and "idle for 1m" in work_app.work_detail_item.title)

    idle[0] = 0
    clock[0] += sct.INPUT_PROBE_INTERVAL
    work_app.tick(None)
    check("new keyboard or mouse activity resumes tracking",
          work_app.work_started_monotonic == clock[0])

    probe_error[0] = True
    clock[0] += sct.INPUT_PROBE_INTERVAL
    work_app.tick(None)
    check("activity probe failure pauses instead of overcounting",
          work_app.work_started_monotonic is None
          and "activity unavailable" in work_app.work_detail_item.title)

    work_app.reset_work_session(None)
    check("reset clears accumulated automatic work time",
          work_app._current_work_elapsed() == 0)

    real_state_path = sct.STATE_PATH
    sct.STATE_PATH = os.path.join(_tmp, "missing", "state.json")
    with redirect_stderr(io.StringIO()) as error_output:
        work_app._save_state()
    work_app._refresh_title()
    check("state save failure is logged and shown",
          work_app.state_save_error
          and "Could not save tracker state" in error_output.getvalue()
          and "state save failed" in work_app.work_detail_item.title)
    sct.STATE_PATH = real_state_path
    work_app._save_state()
    check("successful retry clears state save warning",
          not work_app.state_save_error)
finally:
    sct.STATE_PATH = _state_file
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

# --- Test 11: restart restores saved work but does not count downtime
reset_state()
set_front("vscode")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
clock = [2000.0]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
sct._read_input_idle_seconds = lambda: 0
try:
    running_app = sct.SlackCheckTracker()
    clock[0] += 65
    running_app.tick(None)  # periodic checkpoint
    set_front("chrome")
    clock[0] += 600
    restarted_app = sct.SlackCheckTracker()
    check("restart restores checkpoint without downtime",
          restarted_app.work_elapsed_seconds == 65
          and restarted_app.work_started_monotonic is None)
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

# --- Test 12: rumps lifecycle events configure placement and pause on sleep
reset_state()
set_front("vscode")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
clock = [3000.0]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
sct._read_input_idle_seconds = lambda: 0
lifecycle_app = sct.SlackCheckTracker()
clock[0] += 45
lifecycle_status_item = None
previous_sigterm_handler = sct.signal.getsignal(sct.signal.SIGTERM)


def run_hook(app):
    global lifecycle_status_item
    check("run registers all native lifecycle callbacks",
          len(rumps.events.before_start.callbacks) == 1
          and len(rumps.events.on_sleep.callbacks) == 1
          and len(rumps.events.before_quit.callbacks) == 1)
    check("run installs SIGTERM checkpoint handler",
          sct.signal.getsignal(sct.signal.SIGTERM)
          == lifecycle_app._handle_sigterm)

    class _LifecycleStatusItem:
        autosave_name = None

        def setAutosaveName_(self, name):
            self.autosave_name = name

    lifecycle_status_item = _LifecycleStatusItem()
    app._nsapp = types.SimpleNamespace(nsstatusitem=lifecycle_status_item)
    rumps.events.before_start.emit()
    rumps.events.on_sleep.emit()


_RUN_HOOK = run_hook
try:
    lifecycle_app.run()
finally:
    _RUN_HOOK = None
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

check("sleep event pauses and persists automatic work",
      lifecycle_app.work_started_monotonic is None
      and lifecycle_app.work_elapsed_seconds == 45
      and json.load(open(_state_file))["work_started_ts"] is None)
check("before-start event sets placement autosave name",
      lifecycle_status_item.autosave_name == sct.STATUS_ITEM_AUTOSAVE_NAME)
check("run unregisters all native lifecycle callbacks",
      not rumps.events.before_start.callbacks
      and not rumps.events.on_sleep.callbacks
      and not rumps.events.before_quit.callbacks)
check("run restores previous SIGTERM handler",
      sct.signal.getsignal(sct.signal.SIGTERM) == previous_sigterm_handler)

# --- Test 13: SIGTERM checkpoints automatic work before termination
reset_state()
set_front("cmux")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
real_quit = rumps.quit_application
clock = [4000.0]
quit_calls = []
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
sct._read_input_idle_seconds = lambda: 0
rumps.quit_application = lambda: quit_calls.append(True)
try:
    quit_app = sct.SlackCheckTracker()
    clock[0] += 30
    quit_app._handle_sigterm(sct.signal.SIGTERM, None)
    check("SIGTERM pauses and checkpoints automatic work",
          quit_app.work_started_monotonic is None
          and quit_app.work_elapsed_seconds == 30)
    check("SIGTERM requests application termination", quit_calls == [True])
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader
    rumps.quit_application = real_quit

# --- Test 14: wall-clock changes do not alter automatic elapsed time
reset_state()
set_front("vscode")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
wall_clock = [500.0]
monotonic_clock = [500.0]
sct.time.time = lambda: wall_clock[0]
sct.time.monotonic = lambda: monotonic_clock[0]
sct._read_input_idle_seconds = lambda: 0
try:
    rollback_app = sct.SlackCheckTracker()
    wall_clock[0] = 400.0
    monotonic_clock[0] += 30
    check("wall-clock rollback does not erase automatic work",
          rollback_app._current_work_elapsed() == 30)
    rollback_app._pause_work_session()
    check("wall-clock rollback checkpoints monotonic time",
          rollback_app.work_elapsed_seconds == 30)
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

# --- Test 15: native status item gets a stable autosave name
class _FakeStatusItem:
    autosave_name = None

    def setAutosaveName_(self, name):
        self.autosave_name = name


fake_status_item = _FakeStatusItem()
rollback_app._nsapp = types.SimpleNamespace(nsstatusitem=fake_status_item)
rollback_app._configure_status_item()
check("status item placement is autosaved",
      fake_status_item.autosave_name == sct.STATUS_ITEM_AUTOSAVE_NAME)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
