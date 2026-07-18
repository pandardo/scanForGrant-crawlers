"""Soft dedup (§6.3): the same bando on two portals.

The risk being managed is a FALSE positive: wrongly linking two distinct grants
is worse than missing a duplicate, because the UI would present them as the same
scheme. Hence the deadline-month scope and the high threshold.
"""

from __future__ import annotations

from datetime import date

from crawler.dedup import find_duplicate, normalise_title

SEPT = date(2026, 9, 30)


def existing(**kw):
    base = {"id": "g1", "title": "Bando Voucher Digitale 4.0", "deadline": SEPT, "source_id": "ministry"}
    return {**base, **kw}


class TestNormalise:
    def test_strips_boilerplate_that_differs_between_portals(self):
        assert normalise_title("Bando Voucher Digitale 4.0 - Anno 2026") == normalise_title(
            "Avviso pubblico Voucher Digitale 4.0"
        )

    def test_keeps_the_distinguishing_words(self):
        assert "digitale" in normalise_title("Bando Voucher Digitale")
        assert "agricoltura" in normalise_title("Contributi Agricoltura 2026")


class TestFindDuplicate:
    def test_same_bando_on_two_portals_is_linked(self):
        candidate = {
            "title": "Avviso pubblico Voucher Digitale 4.0",
            "deadline": SEPT,
            "source_id": "region",
        }
        found, score = find_duplicate(candidate, [existing()])
        assert found == "g1"
        assert score >= 0.9

    def test_different_grants_are_not_linked(self):
        candidate = {"title": "Bando Agricoltura Sostenibile", "deadline": SEPT, "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing()])
        assert found is None

    def test_same_scheme_different_year_is_not_a_duplicate(self):
        """The years live in the deadline, so the month scope separates them."""
        candidate = {
            "title": "Bando Voucher Digitale 4.0",
            "deadline": date(2027, 9, 30),
            "source_id": "region",
        }
        found, _ = find_duplicate(candidate, [existing()])
        assert found is None, "different deadline month must not match"

    def test_the_same_source_is_never_its_own_duplicate(self):
        """One portal listing a bando twice is its own problem, not cross-portal."""
        candidate = {"title": "Bando Voucher Digitale 4.0", "deadline": SEPT, "source_id": "ministry"}
        found, _ = find_duplicate(candidate, [existing(source_id="ministry")])
        assert found is None

    def test_missing_deadlines_never_match(self):
        """Half the archive is 'a sportello'; two nulls are not evidence."""
        candidate = {"title": "Bando Voucher Digitale 4.0", "deadline": None, "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing(deadline=None)])
        assert found is None

    def test_picks_the_best_match_when_several_pass_the_threshold(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0 per le PMI", "deadline": SEPT, "source_id": "region"}
        found, _ = find_duplicate(
            candidate,
            [
                existing(id="g1", title="Bando Voucher Digitale 4.0 per le PMI"),
                existing(id="g2", title="Voucher Digitale 4.0 per le PMI - anno 2026"),
            ],
        )
        assert found in ("g1", "g2"), "both are the same bando; either link is correct"

    def test_a_near_miss_below_threshold_is_not_linked(self):
        """Wrongly linking two distinct grants is worse than missing a duplicate:
        the UI would present them as one scheme. 'Voucher Digitale 4.0 PMI' vs
        'per le PMI' scores 82 — real phrasing drift, but under the §6.3 bar of 90,
        so it stays unlinked rather than guessed."""
        candidate = {"title": "Voucher Digitale 4.0 PMI", "deadline": SEPT, "source_id": "region"}
        found, _ = find_duplicate(
            candidate, [existing(id="g1", title="Voucher Digitale 4.0 per le PMI")]
        )
        assert found is None

    def test_empty_pool_is_not_an_error(self):
        assert find_duplicate({"title": "B", "deadline": SEPT, "source_id": "s"}, []) == (None, 0.0)

    def test_untitled_candidate_matches_nothing(self):
        assert find_duplicate({"title": "", "deadline": SEPT, "source_id": "s"}, [existing()])[0] is None


class TestDeadlineShapes:
    """Deadlines arrive as `date` from Stage B and as ISO STRINGS from Supabase
    (JSON has no date type). Comparing the two crashed on the first real
    multi-source scan — every unit test passed because the fixtures used `date`
    on both sides and never exercised the seam.
    """

    def test_string_deadline_from_the_db_matches_a_date_from_stage_b(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0", "deadline": SEPT, "source_id": "region"}
        # Exactly what postgrest returns:
        pool = [existing(deadline="2026-09-30")]
        found, _ = find_duplicate(candidate, pool)
        assert found == "g1"

    def test_both_sides_as_strings(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0", "deadline": "2026-09-30", "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing(deadline="2026-09-30")])
        assert found == "g1"

    def test_timestamp_string_is_accepted(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0", "deadline": SEPT, "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing(deadline="2026-09-30T00:00:00+00:00")])
        assert found == "g1"

    def test_unparseable_deadline_does_not_crash(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0", "deadline": SEPT, "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing(deadline="a sportello")])
        assert found is None

    def test_different_months_as_strings_do_not_match(self):
        candidate = {"title": "Avviso Voucher Digitale 4.0", "deadline": "2026-10-30", "source_id": "region"}
        found, _ = find_duplicate(candidate, [existing(deadline="2026-09-30")])
        assert found is None
