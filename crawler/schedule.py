"""Frequency gating for the hourly cron (§6.7).

The workflow runs hourly and asks this whether to proceed. That way changing the
frequency in Settings takes effect immediately, with no workflow edit and no
redeploy — the schedule lives in the database, not in YAML.

Anchored to windows, not gaps. Two earlier designs each failed one way:

  * Gating on the clock hour being exactly 06:00 broke under GitHub cron drift —
    a skipped 06:00 firing meant the daily scan never ran that day.
  * Gating on "at least 23h since the last scan" survived drift but let the scan
    time creep: a manual run at 12:20 pushed every following daily scan to
    ~12:30, permanently — it could drift later but never recover to the morning.

So instead each frequency defines windows (daily: one per day starting 06:00
Rome; six-hourly: 00/06/12/18), and a tick runs iff the current window has not
been scanned yet. The first tick at or after the window start runs — an hour of
drift means an hour late, never "not at all" — and the next morning re-anchors
at 06:00 no matter when the previous scan actually happened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# The team is Italian; "daily at 06:00" means their morning, not UTC's.
ROME = ZoneInfo("Europe/Rome")

DAILY_HOUR = 6

# Slightly under an hour so ordinary cron jitter (a firing a few minutes early)
# does not push a due hourly scan into the next tick.
_HOURLY_GAP = timedelta(minutes=50)

_KNOWN = ("hourly", "six_hourly", "daily")


def should_run(
    frequency: str,
    *,
    now: datetime | None = None,
    last_scan_at: datetime | None = None,
) -> tuple[bool, str]:
    """Should this hourly tick actually scan? Returns (run, reason).

    hourly     — at most once an hour
    six_hourly — once per 00/06/12/18 Rome window
    daily      — once per day, in the window starting 06:00 Rome

    `last_scan_at` is when the last completed run finished (any tz; None if never
    scanned). A window counts as done iff that falls inside it, which is what
    keeps the schedule both drift-proof and anchored to the intended hour.
    """
    current = (now or datetime.now(ROME)).astimezone(ROME)

    if frequency not in _KNOWN:
        # An unknown value must not silently disable scanning: fail loud but keep
        # running, so a bad settings write does not stop the product working.
        log.warning("unknown scan_frequency %r, defaulting to run", frequency)
        return True, f"unknown frequency {frequency!r}: running anyway"

    # Never scanned yet — always run, whatever the frequency.
    if last_scan_at is None:
        return True, f"frequency={frequency}: no previous scan, running"

    last = last_scan_at.astimezone(ROME)

    if frequency == "hourly":
        elapsed = current - last
        if elapsed < _HOURLY_GAP:
            return False, (
                f"frequency=hourly: last scan {elapsed.total_seconds() / 60:.0f}m ago, "
                f"under the {_HOURLY_GAP.total_seconds() / 60:.0f}m minimum"
            )
        return True, (
            f"frequency=hourly: {elapsed.total_seconds() / 3600:.1f}h since last scan, due"
        )

    if frequency == "daily":
        if current.hour < DAILY_HOUR:
            return False, (
                f"frequency=daily: before {DAILY_HOUR:02d}:00 Europe/Rome "
                f"(hour {current.hour:02d})"
            )
        window_start = current.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    else:  # six_hourly
        window_start = current.replace(
            hour=(current.hour // 6) * 6, minute=0, second=0, microsecond=0
        )

    if last >= window_start:
        return False, (
            f"frequency={frequency}: already scanned this window "
            f"(last at {last:%H:%M}, window started {window_start:%H:%M} Europe/Rome)"
        )

    return True, (
        f"frequency={frequency}: window started {window_start:%H:%M} Europe/Rome "
        f"and last scan was {last:%Y-%m-%d %H:%M}, due"
    )
