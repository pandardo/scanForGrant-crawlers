"""Frequency gating (§6.7).

The workflow fires hourly; this decides whether to scan. Getting it wrong either
burns budget (scanning every hour when set to daily) or silently stops the
product working (never scanning).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from crawler.schedule import ROME, should_run


def rome(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, tzinfo=ROME)


class TestHourly:
    @pytest.mark.parametrize("hour", [0, 3, 7, 13, 23])
    def test_always_runs(self, hour):
        assert should_run("hourly", now=rome(hour))[0] is True


class TestSixHourly:
    @pytest.mark.parametrize("hour", [0, 6, 12, 18])
    def test_runs_on_multiples_of_six(self, hour):
        assert should_run("six_hourly", now=rome(hour))[0] is True

    @pytest.mark.parametrize("hour", [1, 5, 7, 11, 13, 17, 23])
    def test_skips_other_hours(self, hour):
        assert should_run("six_hourly", now=rome(hour))[0] is False


class TestDaily:
    def test_runs_at_six_rome(self):
        assert should_run("daily", now=rome(6))[0] is True

    @pytest.mark.parametrize("hour", [0, 5, 7, 12, 18, 23])
    def test_skips_every_other_hour(self, hour):
        assert should_run("daily", now=rome(hour))[0] is False

    def test_cron_drift_within_the_hour_still_counts(self):
        """GitHub cron drifts by minutes under load (§6.7): 06:47 is still 06:00."""
        assert should_run("daily", now=rome(6, 47))[0] is True

    def test_uses_rome_not_utc(self):
        """The team is Italian: 'daily at 06:00' means their morning."""
        # 06:00 UTC is 08:00 in Rome in July — must NOT run.
        utc_six = datetime(2026, 7, 16, 6, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        assert should_run("daily", now=utc_six)[0] is False

        # 04:00 UTC IS 06:00 Rome in July — must run.
        utc_four = datetime(2026, 7, 16, 4, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        assert should_run("daily", now=utc_four)[0] is True


class TestUnknownFrequency:
    def test_runs_rather_than_silently_disabling_scans(self):
        """A bad settings write must not quietly stop the product working."""
        run, reason = should_run("every_fortnight", now=rome(3))
        assert run is True
        assert "unknown" in reason.lower()


def test_reason_is_always_human_readable():
    """The reason is logged; it should explain itself to whoever reads the run."""
    for frequency in ("hourly", "six_hourly", "daily"):
        _, reason = should_run(frequency, now=rome(6))
        assert frequency in reason
