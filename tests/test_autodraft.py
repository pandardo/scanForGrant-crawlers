"""Auto-draft gating (§6.4 Track 2): WHEN a persistent error turns into a PR.

The drafting/PR mechanics are draft.py + github_pr.py; what is load-bearing here
is the trigger — fire exactly at the threshold (one PR per outage, not one per
scan), only for source types a rule can fix, and never when disabled.
"""

from __future__ import annotations

import pytest

from crawler.autodraft import DRAFTABLE_TYPES, should_autodraft, threshold_from_env

HTML_SOURCE = {"id": "s1", "type": "html", "label": "Regione Esempio"}


def test_fires_exactly_at_the_threshold():
    assert not should_autodraft(HTML_SOURCE, 2, threshold=3)
    assert should_autodraft(HTML_SOURCE, 3, threshold=3)


def test_does_not_refire_above_the_threshold():
    """A week-long outage must produce one PR, not one per hourly scan."""
    assert not should_autodraft(HTML_SOURCE, 4, threshold=3)
    assert not should_autodraft(HTML_SOURCE, 27, threshold=3)


def test_zero_threshold_disables_autodraft():
    assert not should_autodraft(HTML_SOURCE, 3, threshold=0)


@pytest.mark.parametrize("source_type", ["rss", "api", "rule"])
def test_only_html_sources_are_draftable(source_type):
    """An rss/api error is a feed problem, not an extraction problem; a broken
    rule source needs human eyes, not a second rule on top."""
    source = {**HTML_SOURCE, "type": source_type}
    assert not should_autodraft(source, 3, threshold=3)


def test_html_js_is_draftable():
    assert should_autodraft({**HTML_SOURCE, "type": "html_js"}, 3, threshold=3)
    assert set(DRAFTABLE_TYPES) == {"html", "html_js"}


def test_threshold_defaults_to_three(monkeypatch):
    monkeypatch.delenv("AUTODRAFT_AFTER_ERRORS", raising=False)
    assert threshold_from_env() == 3


def test_threshold_reads_the_env(monkeypatch):
    monkeypatch.setenv("AUTODRAFT_AFTER_ERRORS", "5")
    assert threshold_from_env() == 5
    monkeypatch.setenv("AUTODRAFT_AFTER_ERRORS", "0")
    assert threshold_from_env() == 0


def test_draft_failure_never_raises(monkeypatch):
    """An auto-draft failure must not fail the scan run that triggered it."""
    from crawler.autodraft import draft_and_open_pr

    class ExplodingFetcher:
        def get(self, url):
            raise RuntimeError("network down")

    class DeadRenderer:
        def render(self, url):
            from crawler.render import RenderError

            raise RenderError("no browser")

    # fetcher.get raising is outside draft_and_open_pr's contract (the pipeline's
    # Fetcher returns None on failure), so emulate that behaviour.
    class NoneFetcher:
        def get(self, url):
            return None

    url = "https://www.regione-esempio.it/bandi"
    result = draft_and_open_pr(
        {**HTML_SOURCE, "url": url},
        fetcher=NoneFetcher(),
        llm=None,  # never reached: fetch and render both fail first
        renderer=DeadRenderer(),
    )
    assert result is None
