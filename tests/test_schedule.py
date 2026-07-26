"""Frequency gating (§6.7), drift-proof AND anchored.

Two bugs this guards against. GitHub cron drift can skip a whole hour, and a
gate that demands the clock be exactly 06:00 then never runs the daily scan that
day. And a pure time-since-last-scan gate lets the scan time creep ever later —
one midday manual run and every following daily scan lands at midday. Window
anchoring fixes both: the first tick in an unscanned window runs, and the next
window re-anchors at its intended start regardless of when the last scan was.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from crawler.schedule import ROME, should_run


def rome(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=ROME)


class TestNeverScanned:
    @pytest.mark.parametrize("freq", ["hourly", "six_hourly", "daily"])
    def test_always_runs_with_no_history(self, freq):
        assert should_run(freq, now=rome(3), last_scan_at=None)[0] is True


class TestHourly:
    def test_runs_when_an_hour_has_passed(self):
        assert should_run("hourly", now=rome(10), last_scan_at=rome(9))[0] is True

    def test_skips_when_under_an_hour(self):
        assert should_run("hourly", now=rome(10, 20), last_scan_at=rome(10))[0] is False


class TestDaily:
    def test_runs_the_first_tick_after_6am(self):
        """A day has passed and it's past 06:00 — run."""
        run, _ = should_run("daily", now=rome(6, 5, day=20), last_scan_at=rome(6, 2, day=19))
        assert run is True

    def test_skips_the_rest_of_the_day(self):
        """Already scanned this morning — every later tick skips."""
        assert should_run("daily", now=rome(14), last_scan_at=rome(6, 5))[0] is False

    def test_survives_a_missed_6am_hour(self):
        """THE bug: GitHub skipped the 06:00 firing (5:13 then 7:20 in the wild).
        The 07:20 tick must still run, because a day has passed and it is past 6am."""
        run, reason = should_run("daily", now=rome(7, 20, day=20), last_scan_at=rome(6, 10, day=19))
        assert run is True, reason

    def test_does_not_run_before_6am_even_if_a_day_passed(self):
        """A daily scan should land in the morning, not at 02:00."""
        run, _ = should_run("daily", now=rome(2, day=20), last_scan_at=rome(6, day=19))
        assert run is False

    def test_before_6am_but_over_a_day_still_waits_for_morning(self):
        run, _ = should_run("daily", now=rome(5, 30, day=21), last_scan_at=rome(6, day=19))
        assert run is False

    def test_reanchors_to_morning_after_a_midday_manual_run(self):
        """THE drift bug: a manual run at 12:20 must not push the next daily
        scan to midday forever. The 06:05 tick the next morning is a new
        window, so it runs — under the old 23h-gap rule it skipped until 12+."""
        run, reason = should_run("daily", now=rome(6, 5, day=21), last_scan_at=rome(12, 20, day=20))
        assert run is True, reason

    def test_manual_run_before_the_window_does_not_count_for_it(self):
        """A 03:00 manual scan belongs to yesterday's window; the morning tick
        still runs today's scan."""
        run, _ = should_run("daily", now=rome(6, 5, day=20), last_scan_at=rome(3, day=20))
        assert run is True


class TestSixHourly:
    def test_runs_after_six_hours_on_a_boundary(self):
        run, _ = should_run("six_hourly", now=rome(12, 5), last_scan_at=rome(6, 3))
        assert run is True

    def test_skips_within_six_hours(self):
        assert should_run("six_hourly", now=rome(9), last_scan_at=rome(6))[0] is False

    def test_runs_eventually_if_all_boundaries_are_missed(self):
        """If drift skips every 00/06/12/18 boundary, don't wait forever."""
        run, _ = should_run("six_hourly", now=rome(13, 30), last_scan_at=rome(6))
        assert run is True


class TestUnknownFrequency:
    def test_runs_rather_than_silently_disabling(self):
        run, reason = should_run("every_fortnight", now=rome(3), last_scan_at=rome(2))
        assert run is True
        assert "unknown" in reason.lower()


class TestTimezone:
    def test_daily_uses_rome_not_utc(self):
        from zoneinfo import ZoneInfo

        # 04:00 UTC is 06:00 Rome in July; a day has passed -> should run.
        utc = ZoneInfo("UTC")
        now = datetime(2026, 7, 20, 4, 5, tzinfo=utc)
        last = datetime(2026, 7, 19, 4, 5, tzinfo=utc)
        assert should_run("daily", now=now, last_scan_at=last)[0] is True
