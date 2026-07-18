"""HTTP fetching: rate limits, robots.txt, and URL canonicalisation.

These are public-interest government pages, but we still respect robots.txt and
rate-limit per domain (§11).
"""

from __future__ import annotations

import hashlib
import re
import logging
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from .config import Config

log = logging.getLogger(__name__)

# Query params that identify a campaign, not a document: stripping them keeps the
# same grant from being stored twice under two URLs.
TRACKING_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
)


# Real feeds ship malformed URLs. Sardegna Ricerche's notizie feed publishes
# every link as "http://http://www.sardegnaricerche.it/..." — the scheme twice.
# Left alone, "http" parses as the hostname and every fetch fails, burning a
# request per item. Repairing this is not politeness, it is the difference
# between that source working and not.
_DOUBLE_SCHEME = re.compile(r"^(https?://)(?:https?://)+", re.IGNORECASE)


def repair_url(url: str) -> str:
    """Fix malformed URLs that real feeds actually publish."""
    return _DOUBLE_SCHEME.sub(r"\1", url.strip())


def canonical_url(url: str, base: str | None = None) -> str:
    """Absolute, fragment-free, tracking-free URL. Feeds url_hash."""
    url = repair_url(url)

    if base:
        url = urljoin(base, url)

    parsed = urlparse(url.strip())

    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and part.split("=")[0].lower() not in TRACKING_PARAMS
    )

    # Drop the fragment: same document.
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            query,
            "",
        )
    )


def url_hash(url: str) -> str:
    """sha256 of the canonical URL — the hard dedup key (§5)."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """Hash of page content, for change detection.

    Whitespace-normalised so cosmetic reflows do not read as a change and trigger
    a needless LLM call.
    """
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


class Fetcher:
    """Rate-limited HTTP client that honours robots.txt per domain."""

    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(
            timeout=config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent},
        )
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.pages_fetched = 0

    def _wait_for_domain(self, domain: str) -> None:
        last = self._last_request_at.get(domain)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self._config.request_delay_seconds:
                time.sleep(self._config.request_delay_seconds - elapsed)
        self._last_request_at[domain] = time.monotonic()

    def _robots_for(self, domain: str, scheme: str) -> urllib.robotparser.RobotFileParser | None:
        if domain in self._robots:
            return self._robots[domain]

        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self._client.get(f"{scheme}://{domain}/robots.txt", timeout=10.0)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                # No robots.txt is permission, not denial.
                parser = None
        except httpx.HTTPError:
            # An unreachable robots.txt should not block the run; the rate limit
            # still applies.
            parser = None

        self._robots[domain] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        robots = self._robots_for(parsed.netloc, parsed.scheme or "https")
        if robots is None:
            return True
        return robots.can_fetch(self._config.user_agent, url)

    def get(self, url: str) -> httpx.Response | None:
        """Fetch a URL. Returns None when robots.txt disallows it or it errors."""
        parsed = urlparse(url)

        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None

        self._wait_for_domain(parsed.netloc)

        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            log.warning("fetch failed for %s: %s", url, exc)
            return None

        self.pages_fetched += 1

        if response.status_code != 200:
            log.warning("fetch returned %s for %s", response.status_code, url)
            return None

        return response

    def post(self, url: str, **kwargs) -> httpx.Response | None:
        """POST for JSON-API sources (§6.2 `api`). Returns None on failure.

        Same politeness as get(): robots.txt is honoured and the per-domain rate
        limit applies. A query API is still someone else's server.
        """
        parsed = urlparse(url)

        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None

        self._wait_for_domain(parsed.netloc)

        try:
            response = self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("POST failed for %s: %s", url, exc)
            return None

        self.pages_fetched += 1

        if response.status_code != 200:
            log.warning("POST returned %s for %s", response.status_code, url)
            return None

        return response

    def close(self) -> None:
        self._client.close()
