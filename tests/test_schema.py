"""LLM output is untrusted input. These pin how it is coerced and rejected."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from crawler.schema import GrantExtraction, StageBResponse


class TestCategory:
    def test_known_category_passes(self):
        assert GrantExtraction(title="B", category="Sustainability").category == "Sustainability"

    def test_is_case_insensitive(self):
        assert GrantExtraction(title="B", category="sustainability").category == "Sustainability"

    def test_invented_category_falls_back_to_other(self):
        """A surprising label must not lose the grant."""
        assert GrantExtraction(title="B", category="Fintech & Crypto").category == "Other"

    def test_non_string_category_falls_back(self):
        assert GrantExtraction(title="B", category=None).category == "Other"


class TestDeadline:
    def test_iso_date_parses(self):
        assert GrantExtraction(title="B", deadline="2026-09-30").deadline == date(2026, 9, 30)

    def test_missing_deadline_is_none(self):
        """Many bandi are 'a sportello' — no deadline is normal, not an error."""
        assert GrantExtraction(title="B", deadline=None).deadline is None

    @pytest.mark.parametrize("value", ["a sportello", "fino a esaurimento", "N/A", "", "30/09/2026"])
    def test_non_iso_values_become_none_rather_than_failing(self, value):
        assert GrantExtraction(title="B", deadline=value).deadline is None


class TestFundingAmounts:
    def test_plain_numbers(self):
        g = GrantExtraction(title="B", funding_min=10000, funding_max=50000.5)
        assert (g.funding_min, g.funding_max) == (10000.0, 50000.5)

    def test_italian_formatted_string(self):
        """'€10.000' is ten thousand: '.' groups thousands in Italian."""
        assert GrantExtraction(title="B", funding_min="€10.000").funding_min == 10000.0

    def test_italian_decimal_comma(self):
        assert GrantExtraction(title="B", funding_min="1.234,56").funding_min == 1234.56

    def test_unparseable_amount_is_none(self):
        assert GrantExtraction(title="B", funding_min="da definire").funding_min is None


class TestRequirements:
    def test_list_of_strings_passes(self):
        assert GrantExtraction(title="B", requirements=["a", "b"]).requirements == ["a", "b"]

    def test_non_list_becomes_empty(self):
        assert GrantExtraction(title="B", requirements="a, b").requirements == []

    def test_blanks_are_dropped(self):
        assert GrantExtraction(title="B", requirements=["a", "", "  "]).requirements == ["a"]

    def test_is_capped(self):
        assert len(GrantExtraction(title="B", requirements=[f"r{i}" for i in range(50)]).requirements) == 20


class TestTitle:
    def test_title_is_required(self):
        with pytest.raises(ValidationError):
            GrantExtraction()

    def test_empty_title_is_rejected(self):
        """A grant with no title is not a grant."""
        with pytest.raises(ValidationError):
            GrantExtraction(title="")


class TestStageBResponse:
    def test_full_valid_payload(self):
        parsed = StageBResponse.model_validate(
            {
                "grant": {
                    "title": "Voucher Digitale 4.0",
                    "issuer": "MIMIT",
                    "category": "Digital & Innovation",
                    "requirements": ["PMI italiana"],
                    "funding_text": "€10.000 - €50.000",
                    "funding_min": "€10.000",
                    "funding_max": "€50.000",
                    "deadline": "2026-09-30",
                },
                "relevance": [{"topic_id": "t1", "score": 0.9}],
            }
        )
        assert parsed.grant.title == "Voucher Digitale 4.0"
        assert parsed.grant.funding_min == 10000.0
        assert parsed.relevance[0].score == 0.9

    def test_relevance_defaults_to_empty(self):
        assert StageBResponse.model_validate({"grant": {"title": "B"}}).relevance == []

    def test_out_of_range_score_is_rejected(self):
        with pytest.raises(ValidationError):
            StageBResponse.model_validate(
                {"grant": {"title": "B"}, "relevance": [{"topic_id": "t", "score": 1.5}]}
            )
