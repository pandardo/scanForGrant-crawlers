"""Discovery, and the §6.4 validation gate.

The gate is the interesting part: "non-empty" must NOT read as success, because a
selector that matches the nav returns links too. These tests pin that behaviour —
it is what stops the ladder caching a wrong selector and reporting ok.
"""

from __future__ import annotations

import pathlib

import pytest

from crawler.adapters.base import Candidate
from crawler.adapters.html import extract_candidates, validate_candidates

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BASE = "https://www.regione-esempio.it/bandi"


@pytest.fixture
def list_html() -> str:
    return (FIXTURES / "regione_list.html").read_text()


def test_extracts_links_from_list_items(list_html):
    found = extract_candidates(list_html, "li.bando-item", BASE)
    assert len(found) == 3
    assert "Voucher Digitale 4.0 per le PMI" in [c.title for c in found]


def test_extracts_when_selector_points_at_the_anchor(list_html):
    """The selector may match the <a> itself rather than a wrapper."""
    found = extract_candidates(list_html, "li.bando-item a", BASE)
    assert len(found) == 3


def test_canonicalises_and_strips_tracking_params(list_html):
    found = extract_candidates(list_html, "li.bando-item", BASE)
    urls = [c.url for c in found]
    assert "https://www.regione-esempio.it/bandi/startup-green" in urls
    assert not any("utm_source" in u for u in urls)


def test_gate_accepts_a_real_grant_list(list_html):
    found = extract_candidates(list_html, "li.bando-item", BASE)
    accepted, diagnostics = validate_candidates(found, BASE)
    assert len(accepted) == 3
    assert diagnostics["gate_verdict"] == "accepted"


def test_gate_rejects_a_selector_that_matched_the_nav(list_html):
    """THE case §6.4 exists for: nav links are non-empty but are not grants."""
    found = extract_candidates(list_html, "nav.main-nav", BASE)
    assert found, "precondition: the nav selector does match links"

    accepted, diagnostics = validate_candidates(found, BASE)

    assert accepted == [], "nav links must not be accepted as grants"
    assert diagnostics["rejected_chrome"] >= 3
    assert diagnostics["gate_verdict"] == "implausible_count_low"


def test_gate_rejects_off_domain_links():
    candidates = [
        Candidate(url="https://www.partner-esterno.it/qualcosa/altro", title="Partner"),
        Candidate(url="https://www.regione-esempio.it/bandi/uno", title="Bando Uno"),
        Candidate(url="https://www.regione-esempio.it/bandi/due", title="Bando Due"),
    ]
    accepted, diagnostics = validate_candidates(candidates, BASE)
    assert len(accepted) == 2
    assert diagnostics["rejected_url_shape"] == 1


def test_gate_rejects_implausibly_many_candidates():
    """A selector matching hundreds of links caught the page, not a list."""
    many = [
        Candidate(url=f"https://www.regione-esempio.it/bandi/{i}/dettaglio", title=f"B {i}")
        for i in range(250)
    ]
    accepted, diagnostics = validate_candidates(many, BASE)
    assert accepted == []
    assert diagnostics["gate_verdict"] == "implausible_count_high"


def test_gate_rejects_a_single_link():
    """One match is far likelier to be a stray anchor than a grant list."""
    accepted, diagnostics = validate_candidates(
        [Candidate(url="https://www.regione-esempio.it/bandi/uno", title="Solo")], BASE
    )
    assert accepted == []
    assert diagnostics["gate_verdict"] == "implausible_count_low"


def test_gate_deduplicates():
    same = [
        Candidate(url="https://www.regione-esempio.it/bandi/uno", title="Bando Uno"),
        Candidate(url="https://www.regione-esempio.it/bandi/uno", title="Bando Uno (again)"),
        Candidate(url="https://www.regione-esempio.it/bandi/due", title="Bando Due"),
        Candidate(url="https://www.regione-esempio.it/bandi/tre", title="Bando Tre"),
    ]
    accepted, diagnostics = validate_candidates(same, BASE)
    assert len(accepted) == 3
    assert diagnostics["rejected_duplicate"] == 1


def test_gate_reports_what_it_rejected(list_html):
    """A failure must be debuggable, not mysterious (§6.5)."""
    found = extract_candidates(list_html, "a", BASE)
    _, diagnostics = validate_candidates(found, BASE)
    assert "candidates_before_gate" in diagnostics
    assert "rejected_chrome" in diagnostics
    assert "gate_verdict" in diagnostics


class TestCohesionCheck:
    """Found on live Regione Sardegna: Stage A proposed a selector that matched
    18 real bandi AND 20 department pages. Every stray was on-domain with a
    plausible path, so the earlier checks accepted all 38 — a selector that
    "succeeds and is wrong", which §6.4 exists to prevent.

    A grant list points at sibling pages, so its links share a path prefix.
    """

    def _mk(self, urls):
        return [Candidate(url=u, title=f"Item {i}") for i, u in enumerate(urls)]

    def test_strays_outside_the_dominant_section_are_dropped(self):
        candidates = self._mk(
            [f"https://www.regione-esempio.it/bandi/atti/{i}" for i in range(8)]
            + [
                "https://www.regione-esempio.it/presidenza/direzione-generale",
                "https://www.regione-esempio.it/assessorato/agricoltura",
            ]
        )
        accepted, gate = validate_candidates(candidates, BASE)

        assert gate["rejected_incohesive"] == 2
        assert len(accepted) == 8
        assert all("/bandi/atti/" in c.url for c in accepted)

    def test_plurality_is_enough_when_strays_are_fragmented(self):
        """The real shape: bandi were 24 of 48, so a >50% majority rule never
        fired even though the leading group was obvious."""
        candidates = self._mk(
            [f"https://www.regione-esempio.it/bandi/atti/{i}" for i in range(6)]
            + [f"https://www.regione-esempio.it/dept-{i}/page" for i in range(5)]
        )
        accepted, gate = validate_candidates(candidates, BASE)

        assert gate["rejected_incohesive"] == 5
        assert len(accepted) == 6

    def test_a_uniform_list_is_untouched(self):
        candidates = self._mk([f"https://www.regione-esempio.it/bandi/{i}" for i in range(5)])
        accepted, gate = validate_candidates(candidates, BASE)

        assert gate["rejected_incohesive"] == 0
        assert len(accepted) == 5

    def test_an_evenly_split_page_is_not_pruned(self):
        """No dominant group means this heuristic cannot tell which is the list;
        pruning would be a guess, so leave it to the count checks."""
        candidates = self._mk(
            [f"https://www.regione-esempio.it/bandi/{i}" for i in range(3)]
            + [f"https://www.regione-esempio.it/news/{i}" for i in range(3)]
        )
        accepted, gate = validate_candidates(candidates, BASE)

        assert gate["rejected_incohesive"] == 0, "a genuine tie must not be guessed at"
        assert len(accepted) == 6

    def test_small_lists_are_not_pruned(self):
        """Under 4 candidates there is not enough signal to call a plurality."""
        candidates = self._mk(
            ["https://www.regione-esempio.it/bandi/1", "https://www.regione-esempio.it/other/2"]
        )
        accepted, gate = validate_candidates(candidates, BASE)
        assert gate["rejected_incohesive"] == 0
        assert len(accepted) == 2


class TestFingerprintStability:
    """A fingerprint that includes build-hashed CSS-module classes
    (style_data_title__ZN8de, css-1a2b3c) churns on every site redeploy, so the
    reuse pool grows a fresh copy of every strategy after each deploy — seen as
    duplicate 'div.search-body' rows on Regione Sardegna."""

    def test_hashed_classes_are_excluded(self):
        from crawler.fingerprint import _is_stable_class
        assert not _is_stable_class("style_data_title__ZN8de")
        assert not _is_stable_class("css-1a2b3c")
        assert not _is_stable_class("Component_wrapper_a1b2c3")

    def test_structural_classes_are_kept(self):
        from crawler.fingerprint import _is_stable_class
        assert _is_stable_class("bando-item")
        assert _is_stable_class("search-body")
        assert _is_stable_class("card")

    def test_state_and_utility_classes_are_excluded(self):
        from crawler.fingerprint import _is_stable_class
        assert not _is_stable_class("js-toggle")
        assert not _is_stable_class("is-active")
        assert not _is_stable_class("x")

    def test_fingerprint_ignores_hashed_noise(self):
        """Two pages identical but for build hashes fingerprint the same."""
        from crawler.fingerprint import fingerprint_page
        a = '<html><body>' + '<div class="card bando-item style_x__AAAAA">x</div>' * 5 + '</body></html>'
        b = '<html><body>' + '<div class="card bando-item style_x__BBBBB">x</div>' * 5 + '</body></html>'
        assert fingerprint_page(a) == fingerprint_page(b)
