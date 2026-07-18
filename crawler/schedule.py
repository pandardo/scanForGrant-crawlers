"""Frequency gating for the hourly cron (§6.7).

The workflow runs hourly and asks this whether to proceed. That way changing the
frequency in Settings takes effect immediately, with no workflow edit and no
redeploy — the schedule lives in the database, not in YAML.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# The team is Italian; "daily at 06:00" means their morning, not UTC's.
ROME = ZoneInfo("Europe/Rome")

DAILY_HOUR = 6


def should_run(frequency: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """Should this hourly tick actually scan? Returns (run, reason).

    hourly     — always
    six_hourly — when the Rome hour is divisible by 6 (00, 06, 12, 18)
    daily      — at 06:00 Rome

    GitHub cron drifts by minutes under load (§6.7), so this keys on the hour and
    never on the minute; a tick that lands at 06:07 must still count as 06:00.
    """
    current = (now or datetime.now(ROME)).astimezone(ROME)

    if frequency == "hourly":
        return True, "frequency=hourly: every tick runs"

    if frequency == "six_hourly":
        if current.hour % 6 == 0:
            return True, f"frequency=six_hourly: hour {current.hour:02d} is a scan hour"
        return False, f"frequency=six_hourly: hour {current.hour:02d} is not a multiple of 6"

    if frequency == "daily":
        if current.hour == DAILY_HOUR:
            return True, f"frequency=daily: {DAILY_HOUR:02d}:00 Europe/Rome"
        return False, (
            f"frequency=daily: hour {current.hour:02d} is not {DAILY_HOUR:02d}:00 Europe/Rome"
        )

    # An unknown value must not silently disable scanning: fail loud but keep
    # running, so a bad settings write does not stop the product working.
    log.warning("unknown scan_frequency %r, defaulting to run", frequency)
    return True, f"unknown frequency {frequency!r}: running anyway"
