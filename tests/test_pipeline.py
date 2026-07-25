"""Pipeline behaviour: change detection, upsert, and scan_run recording.

The load-bearing property here is that an unchanged page costs ZERO LLM calls
(§6.1 DIFF). Everything else in the design assumes it.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from crawler.fetch import Fetcher, content_hash, url_hash
from crawler.llm import LLMClient
from crawler.pipeline import scan_source
from tests.test_extract import VALID_GRANT, make_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SOURCE = {
    "id": "s1",
    "url": "https://www.regione-esempio.it/bandi",
    "label": "Regione Esempio",
    "type": "html",
    "list_selector": "li.bando-item",
}


class FakeDB:
    """In-memory stand-in for Supabase, recording what the pipeline would write."""

    def __init__(self, known: set[str] | None = None, snapshots: dict | None = None):
        self.known = known or set()
        self.snapshots = snapshots or {}
        self.grants: list[dict] = []
        self.scan_runs: list[dict] = []
        self.source_status: list[dict] = []
        self.consecutive_errors: dict[str, int] = {}
        self.cached_selectors: list[tuple[str, str]] = []
        self.strategies: list[dict] = []
        self.credited: list[str] = []
        self.blamed: list[str] = []

    def known_url_hashes(self, source_id): return self.known
    def page_snapshot(self, source_id, url_hash_): return self.snapshots.get((source_id, url_hash_))
    def save_snapshot(self, source_id, url_hash_, content_hash_): self.snapshots[(source_id, url_hash_)] = content_hash_
    def update_source_status(self, source_id, *, status, error=None):
        # Mirrors db.py: +1 on error, reset on ok, new count returned (0024).
        errors = (self.consecutive_errors.get(source_id, 0) + 1) if status == "error" else 0
        self.consecutive_errors[source_id] = errors
        self.source_status.append({"id": source_id, "status": status, "error": error})
        return errors
    def record_scan_run(self, run): self.scan_runs.append(run)
    def cache_selector(self, source_id, selector): self.cached_selectors.append((source_id, selector))
    def remember_strategy(self, *, selector, fingerprint, origin="llm"): self.strategies.append({"selector": selector, "fingerprint": fingerprint, "origin": origin})
    def credit_strategy(self, strategy_id): self.credited.append(strategy_id)
    def blame_strategy(self, strategy_id): self.blamed.append(strategy_id)
    def extraction_strategies(self, limit=50): return []
    def recent_grants_for_dedup(self, limit=500): return []

    def upsert_grant(self, grant):
        is_new = grant["url_hash"] not in self.known
        self.grants.append(grant)
        self.known.add(grant["url_hash"])
        return is_new


def make_fetcher(pages: dict[str, str]) -> Fetcher:
    """A Fetcher serving canned pages; unknown URLs 404. No network, no sleeping."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        for path, body in pages.items():
            if url == path:
                return httpx.Response(200, text=body)
        return httpx.Response(404)

    config = make_config()
    object.__setattr__(config, "request_delay_seconds", 0.0)
    return Fetcher(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


def make_llm(payload=VALID_GRANT) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    return LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def pages() -> dict[str, str]:
    list_html = (FIXTURES / "regione_list.html").read_text()
    detail_html = (FIXTURES / "regione_detail.html").read_text()
    return {
        "https://www.regione-esempio.it/bandi": list_html,
        "https://www.regione-esempio.it/bandi/voucher-digitale-4-0": detail_html,
        "https://www.regione-esempio.it/bandi/innovazione-sostenibile": detail_html,
        "https://www.regione-esempio.it/bandi/startup-green": detail_html,
    }


def test_first_scan_finds_and_stores_grants(pages):
    db, llm = FakeDB(), make_llm()
    result = scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[])

    assert result.status == "ok"
    assert result.new_count == 3
    assert len(db.grants) == 3
    assert db.grants[0]["title"] == "Voucher Digitale 4.0"
    assert db.grants[0]["url_hash"] == url_hash("https://www.regione-esempio.it/bandi/voucher-digitale-4-0")


def _detail_hash() -> str:
    from crawler.clean import clean_html
    return content_hash(clean_html((FIXTURES / "regione_detail.html").read_text()).text)


def test_unchanged_pages_cost_no_llm_calls(pages):
    """THE property the whole cost model rests on (§6.1)."""
    known = {url_hash(u) for u in pages if u != "https://www.regione-esempio.it/bandi"}
    snapshots = {("s1", h): _detail_hash() for h in known}

    db = FakeDB(known=known, snapshots=snapshots)
    llm = make_llm()

    result = scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[])

    assert llm.calls_made == 0, "unchanged pages must not reach the LLM"
    assert result.new_count == 0
    assert db.grants == []


def test_changed_page_is_re_extracted(pages):
    """A known URL whose content moved must be re-extracted."""
    known = {url_hash("https://www.regione-esempio.it/bandi/voucher-digitale-4-0")}
    snapshots = {("s1", next(iter(known))): "a-stale-hash-from-last-week"}

    db = FakeDB(known=known, snapshots=snapshots)
    llm = make_llm()

    result = scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[])

    assert llm.calls_made > 0
    assert result.updated_count >= 1, "the changed page should update, not insert"


def test_records_a_scan_run_with_cost_metrics(pages):
    db, llm = FakeDB(), make_llm()
    scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[], trigger="cron")

    assert len(db.scan_runs) == 1
    run = db.scan_runs[0]
    assert run["trigger"] == "cron"
    assert run["status"] == "ok"
    assert run["new_count"] == 3
    assert run["llm_calls"] == 3
    assert run["pages_fetched"] >= 4


def test_unfetchable_list_page_marks_the_source_errored():
    db, llm = FakeDB(), make_llm()
    result = scan_source(SOURCE, db=db, fetcher=make_fetcher({}), llm=llm, topics=[])

    assert result.status == "error"
    assert db.source_status[-1]["status"] == "error"
    assert llm.calls_made == 0, "a failed fetch must not spend LLM budget"


def test_error_streak_is_counted_and_surfaced():
    """0024: repeated errors accumulate; the result carries the streak so
    __main__ can trigger an auto-draft (§6.4 Track 2)."""
    db, llm = FakeDB(), make_llm()

    for expected in (1, 2, 3):
        result = scan_source(SOURCE, db=db, fetcher=make_fetcher({}), llm=llm, topics=[])
        assert result.status == "error"
        assert result.consecutive_errors == expected


def test_ok_scan_resets_the_error_streak(pages):
    db, llm = FakeDB(), make_llm()
    scan_source(SOURCE, db=db, fetcher=make_fetcher({}), llm=llm, topics=[])
    assert db.consecutive_errors["s1"] == 1

    scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=make_llm(), topics=[])
    assert db.consecutive_errors["s1"] == 0


def test_zero_candidates_records_diagnostics(pages):
    """§6.5: a zero-candidate run must be debuggable."""
    source = {**SOURCE, "list_selector": "div.does-not-exist"}
    db, llm = FakeDB(), make_llm()

    result = scan_source(source, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[])

    assert result.status == "error"
    assert "cleaned_text_length" in result.diagnostics
    assert "ladder_rungs_tried" in result.diagnostics
    assert result.diagnostics["cached_selector"]["gate_verdict"].startswith("implausible")


def test_dry_run_writes_nothing(pages):
    db, llm = FakeDB(), make_llm()
    result = scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[], dry_run=True)

    assert result.new_count == 3, "dry-run still reports what it would do"
    assert db.grants == [], "but must not write"
    assert db.scan_runs == []
    assert db.source_status == []


def test_non_grant_pages_are_snapshotted_so_they_are_not_re_extracted(pages):
    """A news article costs one LLM call once, not one every run."""
    db, llm = FakeDB(), make_llm(payload={"grant": None})

    scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=llm, topics=[])

    assert db.grants == []
    assert len(db.snapshots) == 3, "each non-grant page should be remembered"


def test_non_grant_pages_are_not_re_extracted_on_the_next_run(pages):
    """Regression, found by running against the live CCIAA feed.

    Non-grant pages (exam notices, news) never enter `grants`, so a DIFF gated on
    "is this already a grant" skipped them and re-sent every one to DeepSeek on
    every run — 5 wasted calls per run, forever. The DIFF must key on the
    snapshot alone.
    """
    db = FakeDB()

    first = make_llm(payload={"grant": None})
    scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=first, topics=[])
    assert first.calls_made == 3, "first run must actually look at each page"
    assert db.grants == [], "none of them are grants"

    # Second run: same pages, nothing changed, no grant rows to gate on.
    second = make_llm(payload={"grant": None})
    scan_source(SOURCE, db=db, fetcher=make_fetcher(pages), llm=second, topics=[])

    assert second.calls_made == 0, "a known non-grant page must not be re-extracted"
