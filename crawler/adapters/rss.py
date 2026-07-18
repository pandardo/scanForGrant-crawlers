"""RSS/Atom adapter.

The cheapest source type: feed entries *are* the candidates, so discovery costs
no LLM call at all (§6.2). Prefer moving sources here whenever a portal offers a
feed.
"""

from __future__ import annotations

import logging

import feedparser

from ..fetch import Fetcher, canonical_url
from ..types import Candidate
from .base import DiscoveryResult

log = logging.getLogger(__name__)


class RssAdapter:
    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def discover(self, source: dict) -> DiscoveryResult:
        url = source.get("list_url") or source["url"]
        response = self._fetcher.get(url)

        if response is None:
            return DiscoveryResult(error=f"could not fetch feed: {url}")

        feed = feedparser.parse(response.text)

        # bozo means malformed XML. Feeds are often slightly malformed but still
        # parse usefully, so only fail when nothing came out.
        if feed.bozo and not feed.entries:
            return DiscoveryResult(
                error=f"feed did not parse: {type(feed.bozo_exception).__name__}",
                diagnostics={
                    "fetched_bytes": len(response.content),
                    "feed_entries": 0,
                    "bozo": True,
                },
            )

        candidates: list[Candidate] = []
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            candidates.append(
                Candidate(
                    url=canonical_url(link, base=url),
                    title=(entry.get("title") or "").strip() or None,
                )
            )

        return DiscoveryResult(
            candidates=candidates,
            diagnostics={
                "fetched_bytes": len(response.content),
                "feed_entries": len(feed.entries),
                "candidates_found": len(candidates),
            },
        )
