#!/usr/bin/env python3
"""
Slack Check + Work Session Tracker — a tiny macOS menu bar app that helps you
notice how often you check Slack and how much active work time you have spent
in VS Code or cmux, without blocking you.

It watches which app is in the foreground. Every time Slack becomes the
frontmost app (a non-Slack -> Slack transition), that counts as one "check."
The menu bar shows automatically accumulated work time, how long it's been
since your last Slack check, and how many times you've checked today. Work time
counts only while VS Code or cmux is frontmost and keyboard/mouse input was
seen recently.

Menu bar looks like:   🪑1h05m  💬12m·5×
    -> working for 1 hour 5 minutes, last checked Slack 12 minutes ago,
       5 checks so far today.

No Slack login, no network, no data leaves your Mac. State is a small JSON
file in ~/.slack_check_tracker.json.

Requires: macOS, Python 3, and the `rumps` and `pyobjc` packages.
    pip install rumps pyobjc-framework-Cocoa
(See setup.command / README for the recommended virtualenv install.)
"""

import ctypes
import fcntl
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, date, timedelta

import rumps

# AppKit / NSWorkspace gives us the current frontmost application.
from AppKit import NSWorkspace


STATE_PATH = os.path.expanduser("~/.slack_check_tracker.json")
LEDGER_PATH = os.path.expanduser("~/.slack_check_tracker_ledger.sqlite3")
LEDGER_SPOOL_PATH = os.path.expanduser(
    "~/.slack_check_tracker_ledger.pending.json"
)
INSTANCE_LOCK_PATH = os.path.expanduser("~/.slack_check_tracker.lock")
ENABLE_INSTANCE_LOCK = True
INSTANCE_LOCK_WAIT_SECONDS = 5
INSTANCE_LOCK_RETRY_INTERVAL = 0.1
STATUS_ITEM_AUTOSAVE_NAME = "com.stop-checking-slack.tracker.primary"

# How often (seconds) we poll the frontmost app and refresh the title.
POLL_INTERVAL = 2
INPUT_PROBE_INTERVAL = 5
INPUT_PROBE_TIMEOUT = 0.25
INPUT_IDLE_THRESHOLD = 60
STATE_SAVE_INTERVAL = 60
CLOCK_ADJUSTMENT_TOLERANCE = 2

# Work time accumulates automatically only while one of these apps is
# frontmost and keyboard/mouse input was seen recently.
WORK_APP_BUNDLE_IDS = {
    "com.microsoft.VSCode": "VS Code",
    "com.microsoft.VSCodeInsiders": "VS Code",
    "com.cmuxterm.app": "cmux",
}
WORK_APP_NAMES = {
    "visual studio code": "VS Code",
    "visual studio code - insiders": "VS Code",
    "cmux": "cmux",
}

# Bundle identifier for the Slack desktop app. We match on this first and
# fall back to the localized app name so it works regardless of language.
SLACK_BUNDLE_ID = "com.tinyspeck.slackmacgap"
SLACK_APP_NAME = "Slack"


def _today_str():
    return date.today().isoformat()


def _read_input_idle_seconds():
    """Read macOS keyboard/mouse idle time without extra dependencies."""
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-r", "-d", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=INPUT_PROBE_TIMEOUT,
    )
    match = re.search(rb'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if match is None:
        raise ValueError("IOHIDSystem did not report HIDIdleTime")
    return int(match.group(1)) / 1_000_000_000


def _read_continuous_time():
    """Read a monotonic macOS clock that continues advancing during sleep."""
    if sys.platform != "darwin":
        return None

    class MachTimebaseInfo(ctypes.Structure):
        _fields_ = [
            ("numer", ctypes.c_uint32),
            ("denom", ctypes.c_uint32),
        ]

    try:
        system = ctypes.CDLL(None)
        system.mach_continuous_time.restype = ctypes.c_uint64
        info = MachTimebaseInfo()
        if system.mach_timebase_info(ctypes.byref(info)) != 0 or not info.denom:
            return None
        ticks = system.mach_continuous_time()
        return ticks * info.numer / info.denom / 1_000_000_000
    except (AttributeError, OSError):
        return None


class SlackCheckTracker(rumps.App):
    def __init__(self):
        # Keep the original rumps app name so existing Application Support
        # state is reused; the visible status-bar title is set separately.
        super().__init__("💬 —", title="🪑—  💬—·0×", quit_button=None)
        self._instance_lock = None
        if ENABLE_INSTANCE_LOCK:
            self._acquire_instance_lock()

        # In-memory state, seeded from disk.
        self.count_today = 0
        self.today = _today_str()
        self.last_check_ts = None  # epoch seconds of the most recent check
        self.work_elapsed_seconds = 0.0
        self.work_started_ts = None
        self.work_started_monotonic = None
        self.work_status = "waiting for VS Code or cmux"
        self.work_app_label = None
        self._last_input_probe_monotonic = None
        self._cached_input_idle_seconds = None
        self._last_state_save_monotonic = time.monotonic()
        self.state_save_error = False
        self.ledger_status = None
        self.ledger_app = None
        self.ledger_reason = None
        self.ledger_started_ts = None
        self.ledger_started_monotonic = None
        self.ledger_started_continuous = None
        self.ledger_save_error = False
        self.ledger_clock_warning = False
        self._pending_ledger_records = []
        # Tracks whether Slack was frontmost on the previous poll, so we only
        # count the *transition* into Slack rather than every poll while it's open.
        self.slack_was_frontmost = False

        self._load_state()

        # Seed the edge detector with whatever is frontmost right now. Without
        # this, launching while Slack is already the active app would read as a
        # False -> True transition on the first tick and log a phantom check.
        frontmost_bundle, frontmost_name = self._frontmost_app_info()
        self.slack_was_frontmost = self._is_slack_app(
            frontmost_bundle, frontmost_name
        )

        # Menu items (the title line is the menu bar; these are the dropdown).
        self.work_detail_item = rumps.MenuItem("Work tracked: —")
        self.work_rule_item = rumps.MenuItem(
            "Auto: VS Code or cmux + recent keyboard/mouse input"
        )
        self.ledger_item = rumps.MenuItem(
            "Ledger: ~/.slack_check_tracker_ledger.sqlite3"
        )
        self.detail_item = rumps.MenuItem("Last checked: —")
        self.count_item = rumps.MenuItem("Checks today: 0")
        self.menu = [
            self.work_detail_item,
            self.work_rule_item,
            self.ledger_item,
            rumps.MenuItem("Reset work session", callback=self.reset_work_session),
            None,
            self.detail_item,
            self.count_item,
            None,
            rumps.MenuItem("Reset today's count", callback=self.reset_count),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Poll on a timer. rumps.Timer fires on the main thread, which is
        # required for both NSWorkspace reads and menu bar title updates.
        self._timer = rumps.Timer(self.tick, POLL_INTERVAL)
        self._timer.start()

        self._load_ledger_spool()

        # Start counting immediately if launch occurs in an eligible app with
        # recent user input; otherwise wait for the first eligible tick.
        self._sync_work_tracking(frontmost_bundle, frontmost_name)

        # Render once immediately so we don't show a placeholder for 2s.
        self._refresh_title()

    def _acquire_instance_lock(self):
        self._instance_lock = open(INSTANCE_LOCK_PATH, "a+")
        deadline = time.monotonic() + INSTANCE_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(
                    self._instance_lock.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(
                        "Another tracker instance is already running.",
                        file=sys.stderr,
                    )
                    self._instance_lock.close()
                    self._instance_lock = None
                    raise SystemExit(0)
                time.sleep(INSTANCE_LOCK_RETRY_INTERVAL)

    def run(self, **options):
        # rumps creates the NSStatusItem immediately before before_start. Set an
        # autosave name there so macOS remembers where the user Command-drags
        # this item in the menu bar. AppKit does not expose a visibility-priority
        # property, so a compact combined item plus saved placement is the
        # supported way to keep both signals ahead of lower-priority items.
        callbacks = (
            (rumps.events.before_start, self._configure_status_item),
            (rumps.events.on_sleep, self._handle_sleep),
            (rumps.events.on_wake, self._handle_wake),
            (rumps.events.before_quit, self._handle_quit),
        )
        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        for event, callback in callbacks:
            event.register(callback)
        try:
            super().run(**options)
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
            for event, callback in callbacks:
                event.unregister(callback)

    def _configure_status_item(self):
        status_item = self._nsapp.nsstatusitem
        if hasattr(status_item, "setAutosaveName_"):
            status_item.setAutosaveName_(STATUS_ITEM_AUTOSAVE_NAME)

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

        saved_elapsed = data.get("work_elapsed_seconds", 0.0)
        if self._is_finite_number(saved_elapsed):
            self.work_elapsed_seconds = max(0.0, float(saved_elapsed))

    def _save_state(self):
        data = {
            "today": self.today,
            "count_today": self.count_today,
            "last_check_ts": self.last_check_ts,
            "work_elapsed_seconds": self._current_work_elapsed(),
            # Automatic tracking never assumes downtime between processes was
            # productive, so a restart always re-evaluates app and input state.
            "work_started_ts": None,
        }
        try:
            # Write-then-rename for an atomic update.
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_PATH)
            self.state_save_error = False
        except OSError as error:
            self.state_save_error = True
            print(f"Could not save tracker state: {error}", file=sys.stderr)

    # ---- status ledger -----------------------------------------------

    @staticmethod
    def _iso_timestamp(timestamp):
        return datetime.fromtimestamp(timestamp).astimezone().isoformat(
            timespec="seconds"
        )

    def _duration_records(
        self,
        started_ts,
        ended_ts,
        status,
        app_name,
        reason,
        elapsed_seconds=None,
    ):
        if elapsed_seconds is None:
            elapsed_seconds = max(0.0, ended_ts - started_ts)
        elapsed_seconds = max(0.0, elapsed_seconds)
        if elapsed_seconds == 0:
            return []

        clock_adjustment = ended_ts - started_ts - elapsed_seconds
        clock_was_adjusted = (
            abs(clock_adjustment) > CLOCK_ADJUSTMENT_TOLERANCE
        )
        accounting_end_ts = (
            started_ts + elapsed_seconds if clock_was_adjusted else ended_ts
        )
        records = []
        cursor = started_ts
        accounting_seconds = accounting_end_ts - started_ts
        remaining_elapsed = round(elapsed_seconds, 3)
        while cursor < accounting_end_ts:
            segment_day = date.fromtimestamp(cursor)
            next_midnight = datetime.combine(
                segment_day + timedelta(days=1), datetime.min.time()
            ).timestamp()
            segment_end = min(accounting_end_ts, next_midnight)
            if segment_end == accounting_end_ts:
                segment_elapsed = remaining_elapsed
            else:
                wall_fraction = (
                    segment_end - cursor
                ) / accounting_seconds
                segment_elapsed = round(elapsed_seconds * wall_fraction, 3)
                remaining_elapsed = round(
                    max(0.0, remaining_elapsed - segment_elapsed),
                    3,
                )
            records.append(
                {
                    "type": "duration",
                    "date": segment_day.isoformat(),
                    "started_at": self._iso_timestamp(cursor),
                    "ended_at": self._iso_timestamp(segment_end),
                    "observed_started_at": self._iso_timestamp(started_ts),
                    "observed_ended_at": self._iso_timestamp(ended_ts),
                    "clock_adjustment_seconds": (
                        round(clock_adjustment, 3)
                        if clock_was_adjusted
                        else 0.0
                    ),
                    "status": status,
                    "app": app_name,
                    "reason": reason,
                    "duration_seconds": segment_elapsed,
                }
            )
            cursor = segment_end
        return records

    def _load_ledger_spool(self):
        try:
            with open(LEDGER_SPOOL_PATH, "r") as spool:
                records = json.load(spool)
            if isinstance(records, list):
                for record in records:
                    normalized = self._normalize_ledger_record(record)
                    if normalized is None:
                        self.ledger_save_error = True
                        print(
                            "Ignored malformed tracker ledger spool record.",
                            file=sys.stderr,
                        )
                        continue
                    self._pending_ledger_records.append(normalized)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self._flush_ledger_records()

    def _persist_ledger_spool(self):
        tmp = LEDGER_SPOOL_PATH + ".tmp"
        with open(tmp, "w") as spool:
            json.dump(self._pending_ledger_records, spool, sort_keys=True)
            spool.flush()
            os.fsync(spool.fileno())
        os.replace(tmp, LEDGER_SPOOL_PATH)
        self._fsync_parent_directory(LEDGER_SPOOL_PATH)

    @staticmethod
    def _fsync_parent_directory(path):
        parent = os.path.dirname(path) or "."
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _initialize_ledger_database(connection):
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS ledger (
                record_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                date TEXT NOT NULL,
                timestamp TEXT,
                started_at TEXT,
                ended_at TEXT,
                status TEXT NOT NULL,
                app TEXT,
                reason TEXT,
                duration_seconds REAL,
                observed_started_at TEXT,
                observed_ended_at TEXT,
                clock_adjustment_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS ledger_date_status
                ON ledger(date, status, app);
            """
        )
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ledger)")
        }
        migration_columns = {
            "observed_started_at": "TEXT",
            "observed_ended_at": "TEXT",
            "clock_adjustment_seconds": "REAL",
        }
        for column, declaration in migration_columns.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE ledger ADD COLUMN {column} {declaration}"
                )

    def _flush_ledger_records(self):
        if not self._pending_ledger_records:
            return

        try:
            self._persist_ledger_spool()
        except OSError as error:
            self.ledger_save_error = True
            print(f"Could not persist tracker ledger spool: {error}", file=sys.stderr)

        try:
            with sqlite3.connect(LEDGER_PATH, timeout=1) as connection:
                self._initialize_ledger_database(connection)
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO ledger (
                        record_id, type, date, timestamp, started_at, ended_at,
                        status, app, reason, duration_seconds,
                        observed_started_at, observed_ended_at,
                        clock_adjustment_seconds
                    ) VALUES (
                        :record_id, :type, :date, :timestamp, :started_at,
                        :ended_at, :status, :app, :reason, :duration_seconds,
                        :observed_started_at, :observed_ended_at,
                        :clock_adjustment_seconds
                    )
                    """,
                    self._pending_ledger_records,
                )
            self._pending_ledger_records.clear()
            self.ledger_save_error = False
            try:
                os.remove(LEDGER_SPOOL_PATH)
                self._fsync_parent_directory(LEDGER_SPOOL_PATH)
            except FileNotFoundError:
                pass
            except OSError as error:
                # A stale spool is safe: record IDs make replay idempotent.
                print(
                    f"Could not remove tracker ledger spool: {error}",
                    file=sys.stderr,
                )
        except (OSError, sqlite3.Error) as error:
            self.ledger_save_error = True
            print(f"Could not update tracker ledger: {error}", file=sys.stderr)

    @staticmethod
    def _normalize_ledger_record(record):
        if not isinstance(record, dict):
            return None
        record = dict(record)
        if (
            record.get("type") not in {"status", "duration"}
            or not isinstance(record.get("date"), str)
            or not isinstance(record.get("status"), str)
        ):
            return None
        record.setdefault("record_id", uuid.uuid4().hex)
        record.setdefault("timestamp", None)
        record.setdefault("started_at", None)
        record.setdefault("ended_at", None)
        record.setdefault("duration_seconds", None)
        record.setdefault("observed_started_at", None)
        record.setdefault("observed_ended_at", None)
        record.setdefault("clock_adjustment_seconds", None)
        record.setdefault("app", None)
        record.setdefault("reason", None)
        return record

    def _queue_ledger_records(self, records):
        for record in records:
            normalized = self._normalize_ledger_record(record)
            if normalized is None:
                raise ValueError("Invalid tracker ledger record")
            self._pending_ledger_records.append(normalized)
        self._flush_ledger_records()

    def _set_ledger_status(self, status, app_name, reason, now=None):
        current = (self.ledger_status, self.ledger_app)
        new = (status, app_name)
        if current == new:
            self.ledger_reason = reason
            return

        now = time.time() if now is None else now
        now_monotonic = time.monotonic()
        now_continuous = _read_continuous_time()
        if status == "sleeping" and now_continuous is None:
            reason += "; suspend-aware clock unavailable, using wall time"
            self.ledger_clock_warning = True
        records = []
        if (
            self.ledger_status is not None
            and self.ledger_started_ts is not None
            and self.ledger_started_monotonic is not None
        ):
            if self.ledger_status == "sleeping":
                if (
                    now_continuous is not None
                    and self.ledger_started_continuous is not None
                ):
                    elapsed = max(
                        0.0,
                        now_continuous - self.ledger_started_continuous,
                    )
                else:
                    elapsed = max(0.0, now - self.ledger_started_ts)
                    self.ledger_clock_warning = True
            else:
                elapsed = max(
                    0.0, now_monotonic - self.ledger_started_monotonic
                )
            records.extend(
                self._duration_records(
                    self.ledger_started_ts,
                    now,
                    self.ledger_status,
                    self.ledger_app,
                    self.ledger_reason,
                    elapsed,
                )
            )
        records.append(
            {
                "type": "status",
                "timestamp": self._iso_timestamp(now),
                "date": date.fromtimestamp(now).isoformat(),
                "status": status,
                "app": app_name,
                "reason": reason,
            }
        )
        self.ledger_status = status
        self.ledger_app = app_name
        self.ledger_reason = reason
        self.ledger_started_ts = now
        self.ledger_started_monotonic = now_monotonic
        self.ledger_started_continuous = now_continuous
        self._queue_ledger_records(records)

    def _checkpoint_ledger(self, now=None):
        if (
            self.ledger_status is None
            or self.ledger_started_ts is None
            or self.ledger_started_monotonic is None
        ):
            return
        now = time.time() if now is None else now
        now_monotonic = time.monotonic()
        now_continuous = _read_continuous_time()
        if self.ledger_status == "sleeping":
            if (
                now_continuous is not None
                and self.ledger_started_continuous is not None
            ):
                elapsed = max(
                    0.0,
                    now_continuous - self.ledger_started_continuous,
                )
            else:
                elapsed = max(0.0, now - self.ledger_started_ts)
                self.ledger_clock_warning = True
        else:
            elapsed = max(
                0.0, now_monotonic - self.ledger_started_monotonic
            )
        records = self._duration_records(
            self.ledger_started_ts,
            now,
            self.ledger_status,
            self.ledger_app,
            self.ledger_reason,
            elapsed,
        )
        self.ledger_started_ts = now
        self.ledger_started_monotonic = now_monotonic
        self.ledger_started_continuous = now_continuous
        self._queue_ledger_records(records)

    # ---- core loop ---------------------------------------------------

    def _frontmost_app_info(self):
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if app is None:
            return None, None
        return app.bundleIdentifier(), app.localizedName()

    @staticmethod
    def _is_slack_app(bundle_id, app_name):
        if bundle_id == SLACK_BUNDLE_ID:
            return True
        return app_name == SLACK_APP_NAME

    def _slack_is_frontmost(self):
        return self._is_slack_app(*self._frontmost_app_info())

    @staticmethod
    def _work_app_name(bundle_id, app_name):
        if bundle_id in WORK_APP_BUNDLE_IDS:
            return WORK_APP_BUNDLE_IDS[bundle_id]
        if not bundle_id and app_name:
            return WORK_APP_NAMES.get(app_name.casefold())
        return None

    def _input_idle_seconds(self, now_monotonic=None):
        now_monotonic = (
            time.monotonic() if now_monotonic is None else now_monotonic
        )
        should_probe = (
            self._last_input_probe_monotonic is None
            or now_monotonic - self._last_input_probe_monotonic
            >= INPUT_PROBE_INTERVAL
        )
        if should_probe:
            try:
                self._cached_input_idle_seconds = _read_input_idle_seconds()
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ):
                self._cached_input_idle_seconds = None
            self._last_input_probe_monotonic = now_monotonic

        if self._cached_input_idle_seconds is None:
            return None
        return self._cached_input_idle_seconds + max(
            0.0, now_monotonic - self._last_input_probe_monotonic
        )

    def _start_work_tracking(self):
        if self.work_started_monotonic is not None:
            return
        self.work_started_ts = time.time()
        self.work_started_monotonic = time.monotonic()

    def _sync_work_tracking(self, bundle_id, app_name):
        self.work_app_label = self._work_app_name(bundle_id, app_name)
        if self.work_app_label is None:
            display_name = app_name or "No frontmost app"
            self.work_status = f"{display_name} is not counted"
            self._pause_work_session(refresh=False)
            self._set_ledger_status(
                "excluded" if app_name else "no_app",
                app_name,
                self.work_status,
            )
            return

        idle_seconds = self._input_idle_seconds()
        if idle_seconds is None:
            self.work_status = "keyboard/mouse activity unavailable"
            self._pause_work_session(refresh=False)
            self._set_ledger_status(
                "input_unavailable",
                self.work_app_label,
                self.work_status,
            )
        elif idle_seconds >= INPUT_IDLE_THRESHOLD:
            self.work_status = f"idle for {self._humanize(idle_seconds)}"
            self._pause_work_session(refresh=False)
            self._set_ledger_status(
                "idle",
                self.work_app_label,
                self.work_status,
            )
        else:
            self.work_status = f"counting {self.work_app_label}"
            self._start_work_tracking()
            self._set_ledger_status(
                "working",
                self.work_app_label,
                "recent keyboard/mouse input",
            )

    def _roll_day_if_needed(self):
        now_day = _today_str()
        if now_day != self.today:
            self.today = now_day
            self.count_today = 0

    @staticmethod
    def _is_finite_number(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    def _current_work_elapsed(self, now_monotonic=None):
        elapsed = self.work_elapsed_seconds
        if self.work_started_monotonic is not None:
            now_monotonic = (
                time.monotonic() if now_monotonic is None else now_monotonic
            )
            elapsed += max(0.0, now_monotonic - self.work_started_monotonic)
        return elapsed

    def tick(self, _timer):
        self._roll_day_if_needed()

        frontmost_bundle, frontmost_name = self._frontmost_app_info()
        is_front = self._is_slack_app(frontmost_bundle, frontmost_name)
        # Count only the rising edge: was NOT Slack, now IS Slack.
        if is_front and not self.slack_was_frontmost:
            self.count_today += 1
            self.last_check_ts = time.time()
            self._save_state()
        self.slack_was_frontmost = is_front

        self._sync_work_tracking(frontmost_bundle, frontmost_name)
        now_monotonic = time.monotonic()
        if (
            now_monotonic - self._last_state_save_monotonic
            >= STATE_SAVE_INTERVAL
        ):
            if self.work_started_monotonic is not None:
                self._save_state()
            self._checkpoint_ledger()
            self._last_state_save_monotonic = now_monotonic

        self._refresh_title()

    # ---- rendering ---------------------------------------------------

    @staticmethod
    def _humanize(seconds, show_seconds=True):
        seconds = int(seconds)
        if show_seconds and seconds < 60:
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
        now = time.time()
        work_elapsed = self._current_work_elapsed()
        work_compact = (
            self._humanize(work_elapsed, show_seconds=False)
            if work_elapsed or self.work_started_monotonic is not None
            else "—"
        )

        if self.last_check_ts is None:
            slack_compact = "—"
            self.detail_item.title = "Last checked: not yet today"
        else:
            ago = self._humanize(max(0.0, now - self.last_check_ts))
            slack_compact = ago
            when = datetime.fromtimestamp(self.last_check_ts).strftime("%-I:%M %p")
            self.detail_item.title = f"Last checked: {ago} ago (at {when})"

        self.title = f"🪑{work_compact}  💬{slack_compact}·{self.count_today}×"
        self.count_item.title = f"Checks today: {self.count_today}"

        if self.work_started_monotonic is not None:
            self.work_detail_item.title = (
                f"Work tracked: {self._humanize(work_elapsed)} "
                f"({self.work_status})"
            )
        else:
            elapsed = self._humanize(work_elapsed) if work_elapsed else "0s"
            self.work_detail_item.title = (
                f"Work tracked: {elapsed} (paused: {self.work_status})"
            )
        if self.state_save_error:
            self.work_detail_item.title += " · state save failed; retrying"
        self.ledger_item.title = "Ledger: ~/.slack_check_tracker_ledger.sqlite3"
        if self.ledger_save_error:
            self.ledger_item.title += " · write pending"
        if self.ledger_clock_warning:
            self.ledger_item.title += " · sleep timing used wall-clock fallback"

    # ---- menu actions ------------------------------------------------

    def _pause_work_session(self, _sender=None, refresh=True):
        if self.work_started_monotonic is None:
            return
        now_monotonic = time.monotonic()
        self.work_elapsed_seconds += max(
            0.0, now_monotonic - self.work_started_monotonic
        )
        self.work_started_ts = None
        self.work_started_monotonic = None
        self._save_state()
        self._last_state_save_monotonic = now_monotonic
        if refresh:
            self._refresh_title()

    def reset_work_session(self, _sender):
        self.work_elapsed_seconds = 0.0
        if self.work_started_monotonic is not None:
            self.work_started_ts = time.time()
            self.work_started_monotonic = time.monotonic()
        self._save_state()
        self._refresh_title()

    def _handle_sleep(self):
        self.work_status = "Mac is sleeping"
        self._pause_work_session()
        self._set_ledger_status("sleeping", None, self.work_status)

    def _handle_wake(self):
        self._cached_input_idle_seconds = None
        self._last_input_probe_monotonic = None
        frontmost_bundle, frontmost_name = self._frontmost_app_info()
        self._sync_work_tracking(frontmost_bundle, frontmost_name)
        self._refresh_title()

    def _handle_quit(self):
        self.work_status = "Tracker stopped"
        self._pause_work_session()
        self._set_ledger_status("stopped", None, self.work_status)

    def _handle_sigterm(self, _signum, _frame):
        # The setup scripts stop older copies with SIGTERM. Checkpoint first so
        # relaunching later cannot count the stopped interval as seated work.
        self._handle_quit()
        rumps.quit_application()

    def reset_count(self, _sender):
        self.count_today = 0
        self._save_state()
        self._refresh_title()

    def quit_app(self, _sender):
        self._handle_quit()
        rumps.quit_application()


if __name__ == "__main__":
    SlackCheckTracker().run()
