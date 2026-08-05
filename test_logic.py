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
    on_wake=_EventEmitter(),
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
    for path in (
        _state_file,
        _ledger_file,
        _ledger_file + "-wal",
        _ledger_file + "-shm",
        _ledger_spool_file,
        _ledger_spool_file + ".tmp",
        _lock_file,
    ):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ---- import the module under test (uses a temp state file) -----------
_tmp = tempfile.mkdtemp()
_state_file = os.path.join(_tmp, "state.json")
_ledger_file = os.path.join(_tmp, "ledger.sqlite3")
_ledger_spool_file = os.path.join(_tmp, "ledger.pending.json")
_lock_file = os.path.join(_tmp, "tracker.lock")

import slack_check_tracker as sct  # noqa: E402

sct.STATE_PATH = _state_file
sct.LEDGER_PATH = _ledger_file
sct.LEDGER_SPOOL_PATH = _ledger_spool_file
sct.INSTANCE_LOCK_PATH = _lock_file
sct.ENABLE_INSTANCE_LOCK = False

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def read_ledger_records():
    with sct.sqlite3.connect(_ledger_file) as connection:
        connection.row_factory = sct.sqlite3.Row
        rows = connection.execute(
            """
            SELECT record_id, type, date, timestamp, started_at, ended_at,
                   status, app, reason, duration_seconds,
                   observed_started_at, observed_ended_at,
                   clock_adjustment_seconds
            FROM ledger
            ORDER BY rowid
            """
        )
        return [dict(row) for row in rows]


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
    check("activity probe failure is ledgered",
          work_app.ledger_status == "input_unavailable")

    set_front(None)
    work_app.tick(None)
    check("missing frontmost app is ledgered",
          work_app.ledger_status == "no_app")

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

# --- Test 12: rumps lifecycle events configure placement and sleep/wake state
reset_state()
set_front("vscode")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
real_continuous_reader = sct._read_continuous_time
wall_clock = [3000.0]
monotonic_clock = [3000.0]
continuous_clock = [3000.0]
idle = [0.0]
idle_probe_calls = []
sct.time.time = lambda: wall_clock[0]
sct.time.monotonic = lambda: monotonic_clock[0]
sct._read_continuous_time = lambda: continuous_clock[0]


def lifecycle_idle_reader():
    idle_probe_calls.append(True)
    return idle[0]


sct._read_input_idle_seconds = lifecycle_idle_reader
lifecycle_app = sct.SlackCheckTracker()
wall_clock[0] += 45
monotonic_clock[0] += 45
lifecycle_status_item = None
previous_sigterm_handler = sct.signal.getsignal(sct.signal.SIGTERM)


def run_hook(app):
    global lifecycle_status_item
    check("run registers all native lifecycle callbacks",
          len(rumps.events.before_start.callbacks) == 1
          and len(rumps.events.on_sleep.callbacks) == 1
          and len(rumps.events.on_wake.callbacks) == 1
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
    wall_clock[0] += 300
    continuous_clock[0] += 600
    idle[0] = 600
    rumps.events.on_wake.emit()
    rumps.events.before_quit.emit()


_RUN_HOOK = run_hook
try:
    lifecycle_app.run()
finally:
    _RUN_HOOK = None
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader
    sct._read_continuous_time = real_continuous_reader

check("sleep event pauses and persists automatic work",
      lifecycle_app.work_started_monotonic is None
      and lifecycle_app.work_elapsed_seconds == 45
      and json.load(open(_state_file))["work_started_ts"] is None)
check("sleep, wake, and shutdown transitions are ledgered",
      [record["status"] for record in read_ledger_records()
       if record["type"] == "status"][-3:]
      == ["sleeping", "idle", "stopped"])
check("sleep duration uses continuous time across a wall-clock adjustment",
      [record["duration_seconds"] for record in read_ledger_records()
       if record["type"] == "duration"
       and record["status"] == "sleeping"] == [600.0])
check("wake forces a fresh activity probe before resuming work",
      len(idle_probe_calls) == 2
      and lifecycle_app.work_elapsed_seconds == 45)
check("before-start event sets placement autosave name",
      lifecycle_status_item.autosave_name == sct.STATUS_ITEM_AUTOSAVE_NAME)
check("run unregisters all native lifecycle callbacks",
      not rumps.events.before_start.callbacks
      and not rumps.events.on_sleep.callbacks
      and not rumps.events.on_wake.callbacks
      and not rumps.events.before_quit.callbacks)
check("run restores previous SIGTERM handler",
      sct.signal.getsignal(sct.signal.SIGTERM) == previous_sigterm_handler)

# --- Test 13: sleep timing fallback is explicit when continuous time fails
reset_state()
set_front("vscode")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
real_continuous_reader = sct._read_continuous_time
clock = [3500.0]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
sct._read_input_idle_seconds = lambda: 0
sct._read_continuous_time = lambda: None
try:
    fallback_app = sct.SlackCheckTracker()
    clock[0] += 10
    fallback_app._handle_sleep()
    clock[0] += 20
    fallback_app._handle_wake()
    fallback_sleep_records = [
        record for record in read_ledger_records()
        if record["type"] == "duration"
        and record["status"] == "sleeping"
    ]
    check("sleep fallback uses wall time and surfaces a warning",
          fallback_sleep_records[-1]["duration_seconds"] == 20
          and "suspend-aware clock unavailable"
          in fallback_sleep_records[-1]["reason"]
          and "wall-clock fallback" in fallback_app.ledger_item.title)
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader
    sct._read_continuous_time = real_continuous_reader

# --- Test 14: SIGTERM checkpoints automatic work before termination
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

# --- Test 15: wall-clock changes do not alter automatic elapsed time
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
    set_front("chrome")
    rollback_app.tick(None)
    rollback_durations = [
        record for record in read_ledger_records()
        if record["type"] == "duration"
        and record["status"] == "working"
    ]
    check("ledger duration uses monotonic time across clock rollback",
          rollback_durations[-1]["duration_seconds"] == 30)
    check("ledger preserves observed wall-clock endpoints across rollback",
          rollback_durations[-1]["observed_started_at"]
          == rollback_app._iso_timestamp(500)
          and rollback_durations[-1]["observed_ended_at"]
          == rollback_app._iso_timestamp(400)
          and rollback_durations[-1]["ended_at"]
          == rollback_app._iso_timestamp(530)
          and rollback_durations[-1]["clock_adjustment_seconds"] == -130)
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

# --- Test 16: ledger records transitions and daily durations
reset_state()
set_front("chrome")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
real_idle_reader = sct._read_input_idle_seconds
real_connect = sct.sqlite3.connect
clock = [1000.0]
idle = [0.0]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
sct._read_input_idle_seconds = lambda: idle[0]
try:
    ledger_app = sct.SlackCheckTracker()
    clock[0] += 10
    set_front("vscode")
    ledger_app.tick(None)
    clock[0] += 20
    set_front("cmux")
    ledger_app.tick(None)
    clock[0] += 30
    idle[0] = 61
    ledger_app.tick(None)
    clock[0] += 15
    ledger_app._checkpoint_ledger()

    ledger_records = read_ledger_records()
    status_records = [
        record for record in ledger_records if record["type"] == "status"
    ]
    duration_records = [
        record for record in ledger_records if record["type"] == "duration"
    ]
    check("ledger records timestamped status transitions",
          [(record["status"], record["app"]) for record in status_records]
          == [
              ("excluded", "Google Chrome"),
              ("working", "VS Code"),
              ("working", "cmux"),
              ("idle", "cmux"),
          ])
    check("ledger records per-status durations",
          [(record["status"], record["duration_seconds"])
           for record in duration_records]
          == [
              ("excluded", 10.0),
              ("working", 20.0),
              ("working", 30.0),
              ("idle", 15.0),
          ])
    check("duration records are tagged for daily totals",
          all(record["date"] and record["started_at"] and record["ended_at"]
              for record in duration_records))

    midnight_start = sct.datetime(2026, 8, 4, 23, 59, 50).timestamp()
    split_records = ledger_app._duration_records(
        midnight_start,
        midnight_start + 20,
        "working",
        "VS Code",
        "test",
    )
    check("ledger splits durations across local midnight",
          [record["duration_seconds"] for record in split_records] == [10.0, 10.0]
          and [record["date"] for record in split_records]
          == ["2026-08-04", "2026-08-05"])
    jump_start = midnight_start + 5
    adjusted_split_records = ledger_app._duration_records(
        jump_start,
        jump_start + 3610,
        "working",
        "VS Code",
        "clock adjusted",
        elapsed_seconds=10,
    )
    check("forward jump allocates time on the monotonic accounting timeline",
          [record["duration_seconds"] for record in adjusted_split_records]
          == [5.0, 5.0]
          and [record["date"] for record in adjusted_split_records]
          == ["2026-08-04", "2026-08-05"]
          and adjusted_split_records[-1]["observed_ended_at"]
          == ledger_app._iso_timestamp(jump_start + 3610)
          and adjusted_split_records[-1]["clock_adjustment_seconds"] == 3600)

    def fail_ledger_connect(*_args, **_kwargs):
        raise sct.sqlite3.OperationalError("database unavailable")

    sct.sqlite3.connect = fail_ledger_connect
    with redirect_stderr(io.StringIO()) as ledger_error:
        ledger_app._set_ledger_status(
            "excluded", "Safari", "Safari is not counted"
        )
    pending_records = list(ledger_app._pending_ledger_records)
    check("ledger failure is durably spooled and visible",
          ledger_app.ledger_save_error
          and pending_records
          and os.path.exists(_ledger_spool_file)
          and "Could not update tracker ledger" in ledger_error.getvalue())
    sct.sqlite3.connect = real_connect
    ledger_app._flush_ledger_records()
    check("queued ledger records flush after recovery",
          not ledger_app.ledger_save_error
          and not ledger_app._pending_ledger_records
          and not os.path.exists(_ledger_spool_file))

    records_after_recovery = read_ledger_records()
    legacy_pending_records = [
        {
            key: value for key, value in record.items()
            if key not in {
                "observed_started_at",
                "observed_ended_at",
                "clock_adjustment_seconds",
            }
        }
        for record in pending_records
    ]
    with open(_ledger_spool_file, "w") as spool:
        json.dump(legacy_pending_records, spool)
    ledger_app._load_ledger_spool()
    check("replaying a legacy stale spool is normalized and idempotent",
          len(read_ledger_records()) == len(records_after_recovery)
          and not ledger_app._pending_ledger_records)
finally:
    sct.sqlite3.connect = real_connect
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic
    sct._read_input_idle_seconds = real_idle_reader

# --- Test 17: excluded statuses are checkpointed without a transition
reset_state()
set_front("chrome")
real_time = sct.time.time
real_monotonic = sct.time.monotonic
clock = [5000.0]
sct.time.time = lambda: clock[0]
sct.time.monotonic = lambda: clock[0]
try:
    checkpoint_app = sct.SlackCheckTracker()
    clock[0] += sct.STATE_SAVE_INTERVAL
    checkpoint_app.tick(None)
    checkpoint_records = read_ledger_records()
    check("excluded status receives periodic duration checkpoints",
          any(record["type"] == "duration"
              and record["status"] == "excluded"
              and record["duration_seconds"] == sct.STATE_SAVE_INTERVAL
              for record in checkpoint_records))
finally:
    sct.time.time = real_time
    sct.time.monotonic = real_monotonic

# --- Test 18: only one tracker instance can own the ledger spool
reset_state()
sct.ENABLE_INSTANCE_LOCK = True
sct.INSTANCE_LOCK_WAIT_SECONDS = 0
set_front("chrome")
lock_owner = sct.SlackCheckTracker()
with redirect_stderr(io.StringIO()) as lock_error:
    try:
        sct.SlackCheckTracker()
    except SystemExit as error:
        duplicate_exit_code = error.code
    else:
        duplicate_exit_code = None
check("instance lock rejects a duplicate tracker",
      duplicate_exit_code == 0
      and "already running" in lock_error.getvalue())
lock_owner._instance_lock.close()
lock_owner._instance_lock = None
replacement_owner = sct.SlackCheckTracker()
check("instance lock is released when the owner exits",
      replacement_owner._instance_lock is not None)
replacement_owner._instance_lock.close()
replacement_owner._instance_lock = None
sct.ENABLE_INSTANCE_LOCK = False
sct.INSTANCE_LOCK_WAIT_SECONDS = 5

# --- Test 19: native status item gets a stable autosave name
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
