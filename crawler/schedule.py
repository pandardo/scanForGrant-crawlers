"""Frequency gating for the hourly cron (§6.7).

The workflow runs hourly and asks this whether to proceed. That way changing the
frequency in Settings takes effect immediately, with no workflow edit and no
redeploy — the schedule lives in the database, not in YAML.

Drift-proof by design. GitHub Actions cron is best-effort and drifts badly under
load — it can skip a whole hour. An earlier version gated "daily" on the hour
being exactly 06:00, so if GitHub skipped the 06:00 firing, the daily scan never
ran that day. Instead we gate on *elapsed time since the last successful scan*:
the first firing at or after the target runs, later ones skip because the work is
already done. A missed hour just means the scan happens an hour late, never not
at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# The team is Italian; "daily at 06:00" means their morning, not UTC's.
ROME = ZoneInfo("Europe/Rome")

DAILY_HOUR = 6

# Minimum spacing between scans per frequency. Slightly under the nominal period
# so ordinary cron jitter (a firing a few minutes early) does not push a due scan
# into the next cycle.
_MIN_GAP = {
    "hourly": timedelta(minutes=50),
    "six_hourly": timedelta(hours=5, minutes=30),
    "daily": timedelta(hours=23),
}


def should_run(
    frequency: str,
    *,
    now: datetime | None = None,
    last_scan_at: datetime | None = None,
) -> tuple[bool, str]:
    """Should this hourly tick actually scan? Returns (run, reason).

    hourly     — at most once an hour
    six_hourly — at most once every 6 hours, not before 00/06/12/18 Rome
    daily      — at most once a day, not before 06:00 Rome

    `last_scan_at` is when the last successful run finished (any tz; None if never
    scanned). Gating on the gap since then rather than on the exact clock hour is
    what makes this survive cron drift.
    """
    current = (now or datetime.now(ROME)).astimezone(ROME)

    if frequency not in _MIN_GAP:
        # An unknown value must not silently disable scanning: fail loud but keep
        # running, so a bad settings write does not stop the product working.
        log.warning("unknown scan_frequency %r, defaulting to run", frequency)
        return True, f"unknown frequency {frequency!r}: running anyway"

    # Never scanned yet — always run, whatever the frequency.
    if last_scan_at is None:
        return True, f"frequency={frequency}: no previous scan, running"

    elapsed = current - last_scan_at.astimezone(ROME)
    gap = _MIN_GAP[frequency]

    if elapsed < gap:
        hrs = elapsed.total_seconds() / 3600
        return False, (
            f"frequency={frequency}: last scan {hrs:.1f}h ago, "
            f"under the {gap.total_seconds() / 3600:.1f}h minimum"
        )

    # Enough time has passed. For six_hourly/daily, also require we are at or past
    # the intended clock window, so a daily scan lands in the morning rather than
    # drifting to whatever hour the gap happens to clear.
    if frequency == "daily" and current.hour < DAILY_HOUR:
        return False, (
            f"frequency=daily: {elapsed.total_seconds() / 3600:.1f}h since last scan, "
            f"but before {DAILY_HOUR:02d}:00 Europe/Rome"
        )

    if frequency == "six_hourly" and current.hour % 6 != 0 and elapsed < timedelta(hours=7):
        # Prefer the 00/06/12/18 boundaries, but do not wait forever: once the gap
        # exceeds 7h (a boundary was clearly missed), run at the next tick.
        return False, (
            f"frequency=six_hourly: {elapsed.total_seconds() / 3600:.1f}h since last scan, "
            f"waiting for a 6-hour boundary (hour {current.hour:02d})"
        )

    return True, (
        f"frequency={frequency}: {elapsed.total_seconds() / 3600:.1f}h since last scan, due"
    )
