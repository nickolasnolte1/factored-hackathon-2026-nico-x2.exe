"""Service clocks (CONTRACT.md section 9): naive datetimes in dataset local time, second precision."""
import time
from datetime import datetime, timedelta, timezone

DEMO_NOW = "2026-06-19T09:00:00"  # the morning after the data's last event date (2026-06-18)


def parse_dt(value):
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None)
    return datetime.fromisoformat(str(value).strip().replace(" ", "T").rstrip("Z")[:19])


def iso(dt):
    """'YYYY-MM-DDTHH:MM:SS' or None."""
    return None if dt is None else parse_dt(dt).strftime("%Y-%m-%dT%H:%M:%S")


def epoch(dt):
    """Epoch seconds of a naive service datetime, treated as UTC (section 2.3)."""
    return int(parse_dt(dt).replace(tzinfo=timezone.utc).timestamp())


def from_epoch(seconds):
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).replace(tzinfo=None)


def wall_utc():
    """Wall-clock UTC time for recorded_at fields: 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FixedClock:
    """A settable clock for tests, evaluation and the demo."""

    def __init__(self, now=DEMO_NOW):
        self._now = parse_dt(now)

    def now(self):
        return self._now

    def set(self, dt):
        self._now = parse_dt(dt)

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


class SystemClock:
    def now(self):
        return datetime.now().replace(microsecond=0)


def clock_from_config(value):
    """BANK_TOOLS_CLOCK: an ISO time (FixedClock) or 'system'."""
    if str(value or "").strip().lower() == "system":
        return SystemClock()
    return FixedClock(value or DEMO_NOW)


class RealSleeper:
    """Backoff that really sleeps (dev, demo)."""

    def __init__(self):
        self.simulated_s = 0.0

    def sleep(self, seconds):
        time.sleep(seconds)


class RecordingSleeper:
    """No-op backoff that only records the time it would have slept (test, eval)."""

    def __init__(self):
        self.simulated_s = 0.0

    def sleep(self, seconds):
        self.simulated_s += seconds
