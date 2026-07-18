"""The §6.4 selector ladder.

The property under test throughout: a rung only counts as working if the gate
ACCEPTS it. Falling through to a more expensive rung is always better than
caching a selector that matches the wrong elements — that is the silent-garbage
failure the whole design exists to avoid.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from crawler.ladder import climb
from crawler.llm import LLMClient
from tests.test_extract import make_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
URL = "https://www.regione-esempio.it/bandi"


@pytest.fixture
def html() -> str:
    return (FIXTURES / "regione_list.html").read_text()


def llm_proposing(selector, reasoning="found the list"):
    """An LLM stub that proposes `selector` for Stage A."""
    payload = {"list_selector": selector, "reasoning": reasoning}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    return LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))


class TestRung1CachedSelector:
    def test_working_cached_selector_wins_immediately(self, html):
        llm = llm_proposing("should.not.be.called")
        result = climb(
            html=html, url=URL,
            source={"list_selector": "li.bando-item"},
            strategies=[{"id": "s1", "list_selector": "article.card", "times_worked": 99}],
            llm=llm,
        )

        assert len(result.candidates) == 3
        assert result.selector_used == "li.bando-item"
        assert llm.calls_made == 0, "a working cache must not spend an LLM call"
        assert result.diagnostics["ladder_rungs_tried"] == ["cached_selector"]

    def test_stale_cached_selector_falls_through(self, html):
        """The self-healing case: the portal changed its markup."""
        result = climb(
            html=html, url=URL,
            source={"list_selector": "div.gone-in-the-redesign"},
            strategies=[{"id": "s1", "list_selector": "li.bando-item", "times_worked": 5}],
            llm=None,
        )

        assert len(result.candidates) == 3, "should have healed via the reuse pool"
        assert result.selector_used == "li.bando-item"
        assert result.worked_strategy_id == "s1"


class TestRung2ReusePool:
    def test_reuses_a_selector_proven_elsewhere(self, html):
        llm = llm_proposing("should.not.be.called")
        result = climb(
            html=html, url=URL, source={},
            strategies=[{"id": "s1", "list_selector": "li.bando-item", "times_worked": 3}],
            llm=llm,
        )

        assert len(result.candidates) == 3
        assert result.worked_strategy_id == "s1"
        assert llm.calls_made == 0, "the pool exists to avoid the LLM call"

    def test_same_fingerprint_is_tried_first(self, html):
        """Portals share a selector because they share a CMS (§6.4)."""
        from crawler.fingerprint import fingerprint_page

        matching = fingerprint_page(html)
        result = climb(
            html=html, url=URL, source={},
            strategies=[
                {"id": "wrong", "list_selector": "nav.main-nav", "fingerprint": "other:xxx", "times_worked": 100},
                {"id": "right", "list_selector": "li.bando-item", "fingerprint": matching, "times_worked": 1},
            ],
            llm=None,
        )

        assert result.worked_strategy_id == "right", "fingerprint must outrank times_worked"

    def test_a_pool_selector_that_matches_the_nav_is_rejected(self, html):
        """THE reason the gate exists: reuse can succeed and be wrong."""
        result = climb(
            html=html, url=URL, source={},
            strategies=[{"id": "bad", "list_selector": "nav.main-nav", "times_worked": 50}],
            llm=None,
        )

        assert result.candidates == [], "nav links must not be accepted as grants"
        assert "bad" in result.failed_strategy_ids, "the bad strategy should be blamed"

    def test_failed_pool_attempts_are_recorded_for_blame(self, html):
        result = climb(
            html=html, url=URL, source={},
            strategies=[
                {"id": "bad1", "list_selector": "footer", "times_worked": 9},
                {"id": "bad2", "list_selector": "nav.main-nav", "times_worked": 8},
                {"id": "good", "list_selector": "li.bando-item", "times_worked": 1},
            ],
            llm=None,
        )

        assert result.worked_strategy_id == "good"
        assert set(result.failed_strategy_ids) == {"bad1", "bad2"}


class TestRung3StageA:
    def test_infers_when_the_cheaper_rungs_fail(self, html):
        llm = llm_proposing("li.bando-item")
        result = climb(html=html, url=URL, source={}, strategies=[], llm=llm)

        assert len(result.candidates) == 3
        assert result.selector_used == "li.bando-item"
        assert llm.calls_made == 1

    def test_an_inferred_selector_that_fails_the_gate_is_not_cached(self, html):
        """A model reading a hostile page must not be able to poison the cache."""
        llm = llm_proposing("nav.main-nav")
        result = climb(html=html, url=URL, source={}, strategies=[], llm=llm)

        assert result.candidates == []
        assert result.selector_used is None, "a gated-out selector must not be cached"

    def test_model_reporting_no_list_is_recorded(self, html):
        llm = llm_proposing(None, reasoning="this page is an article")
        result = climb(html=html, url=URL, source={}, strategies=[], llm=llm)

        assert result.candidates == []
        assert result.diagnostics["stage_a"]["selector"] is None
        assert "article" in result.diagnostics["stage_a"]["reasoning"]

    def test_a_malicious_selector_is_rejected_before_use(self, html):
        """Selectors are data, but they still get a shape check (§6.4)."""
        llm = llm_proposing("li[onclick='javascript:alert(1)']")
        result = climb(html=html, url=URL, source={}, strategies=[], llm=llm)

        assert result.candidates == []
        assert "implausible" in result.diagnostics["stage_a"].get("reasoning", "").lower() or \
               result.selector_used is None


class TestRung4GiveUp:
    def test_exhausted_ladder_reports_why(self, html):
        """§6.5: a failure must be debuggable, not mysterious."""
        result = climb(
            html=html, url=URL,
            source={"list_selector": "div.nope"},
            strategies=[{"id": "s1", "list_selector": "span.also-nope", "times_worked": 1}],
            llm=None,
        )

        assert result.candidates == []
        rungs = result.diagnostics["ladder_rungs_tried"]
        assert "cached_selector" in rungs
        assert "reuse_pool" in rungs
        assert result.diagnostics["fingerprint"]

    def test_no_llm_means_stage_a_is_skipped_not_crashed(self, html):
        result = climb(html=html, url=URL, source={}, strategies=[], llm=None)
        assert result.candidates == []
        assert "stage_a" not in result.diagnostics["ladder_rungs_tried"]
