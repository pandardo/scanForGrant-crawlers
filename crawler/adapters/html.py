"""HTML adapter: fetch, then walk the §6.4 selector ladder."""

from __future__ import annotations

import logging

from ..clean import clean_html
from ..fetch import Fetcher
from ..gate import (  # re-exported: these were part of this module's API
    CHROME_TITLES,
    MAX_PLAUSIBLE_CANDIDATES,
    MIN_PLAUSIBLE_CANDIDATES,
    extract_candidates,
    validate_candidates,
)
from ..ladder import climb
from .base import DiscoveryResult

log = logging.getLogger(__name__)

__all__ = [
    "HtmlAdapter",
    "extract_candidates",
    "validate_candidates",
    "CHROME_TITLES",
    "MAX_PLAUSIBLE_CANDIDATES",
    "MIN_PLAUSIBLE_CANDIDATES",
]


class HtmlAdapter:
    """Plain HTML via httpx. `strategies` and `llm` enable ladder rungs 2 and 3."""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        strategies: list[dict] | None = None,
        llm=None,
    ) -> None:
        self._fetcher = fetcher
        self._strategies = strategies or []
        self._llm = llm

    def _get_html(self, url: str) -> tuple[str | None, str | None]:
        """Returns (html, error)."""
        response = self._fetcher.get(url)
        if response is None:
            return None, f"could not fetch list page: {url}"
        return response.text, None

    # A real grant list is short — a handful to a few dozen. Hundreds of
    # candidates means the "list" is an albo pretorio or news archive, where each
    # page costs a DeepSeek call to reject as a non-grant. Seen live: Quartu's
    # albo paginated to 241 candidates across 246 pages, burning the whole
    # per-source budget on legal notices. Cap discovery well below that; anything
    # beyond is almost certainly not a grant.
    MAX_CANDIDATES_PER_SOURCE = 60

    def _paginate(self, *, first_html, first_url, selector, seed):
        """Apply a working selector across the list's pages, up to a candidate cap.

        seed is page 1's already-gated candidates. Each further page is fetched,
        its candidates extracted with the same selector and gated, merged and
        de-duplicated. Stops at MAX_PAGES (page_urls) or MAX_CANDIDATES_PER_SOURCE,
        whichever comes first — and says so, never a silent truncation (§6).
        """
        from ..gate import extract_candidates, validate_candidates
        from ..paginate import page_urls

        def fetch(u):
            html, _ = self._get_html(u)
            return html

        urls = page_urls(first_html, first_url, fetch)

        seen = {c.url for c in seed}
        candidates = list(seed)
        truncated = False

        for page_url in urls[1:]:  # page 1 is the seed
            if len(candidates) >= self.MAX_CANDIDATES_PER_SOURCE:
                truncated = True
                break
            html, _ = self._get_html(page_url)
            if html is None:
                continue
            found = extract_candidates(html, selector, page_url)
            accepted, _ = validate_candidates(found, page_url)
            for c in accepted:
                if c.url not in seen:
                    seen.add(c.url)
                    candidates.append(c)

        if truncated or len(candidates) > self.MAX_CANDIDATES_PER_SOURCE:
            candidates = candidates[: self.MAX_CANDIDATES_PER_SOURCE]
            log.warning(
                "%s: capped at %d candidates — the list is unusually large, likely "
                "an albo pretorio rather than a grant listing",
                first_url,
                self.MAX_CANDIDATES_PER_SOURCE,
            )

        return candidates, {
            "pages_scanned": len(urls),
            "candidates_after_pagination": len(candidates),
            "candidates_capped": truncated,
        }

    def discover(self, source: dict) -> DiscoveryResult:
        url = source.get("list_url") or source["url"]

        html, error = self._get_html(url)
        if html is None:
            return DiscoveryResult(error=error)

        cleaned = clean_html(html)
        result = climb(
            html=html, url=url, source=source, strategies=self._strategies, llm=self._llm
        )

        diagnostics = {**cleaned.as_diagnostics(), **result.diagnostics}

        if result.candidates:
            # A selector that works on page 1 works on every page (§6.4). Follow
            # the pager and apply it across all pages, so grants deeper than page
            # 1 are not silently missed — the Sardegna Ricerche case.
            candidates = result.candidates
            if result.selector_used:
                candidates, page_diag = self._paginate(
                    first_html=html, first_url=url, selector=result.selector_used,
                    seed=result.candidates,
                )
                diagnostics.update(page_diag)

            return DiscoveryResult(
                candidates=candidates,
                selector_used=result.selector_used,
                strategy_id=result.strategy_id,
                fingerprint=result.fingerprint,
                worked_strategy_id=result.worked_strategy_id,
                failed_strategy_ids=result.failed_strategy_ids,
                diagnostics=diagnostics,
            )

        # Say WHY, precisely. "Every rung was exhausted" reads as "this site has
        # no grants", but the commonest cause on a large run is that the per-run
        # LLM budget ran out before Stage A could even be attempted — the source
        # was never actually tried. Reporting those identically sends you
        # debugging a site that is probably fine.
        stage_a = diagnostics.get("stage_a") or {}
        if isinstance(stage_a, dict) and stage_a.get("skipped"):
            error = (
                f"not attempted: {stage_a['skipped']}. "
                "Raise LLM_MAX_CALLS_PER_RUN or scan this source on its own."
            )
        elif not diagnostics.get("ladder_rungs_tried"):
            error = "no candidates found: no selector to try and Stage A was unavailable"
        else:
            error = "no candidates found: every ladder rung was tried and gated out"

        return DiscoveryResult(
            fingerprint=result.fingerprint,
            failed_strategy_ids=result.failed_strategy_ids,
            diagnostics=diagnostics,
            error=error,
        )
