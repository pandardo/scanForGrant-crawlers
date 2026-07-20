"""Rule adapter — interprets an extraction rule (§6.4 Track 2).

Selected when `sources.type = 'rule'`. Reads `sources.extraction_rules`, validates
it, and follows it: fetch or render the listing, walk pagination, pull candidates
by the rule's selectors or JSON paths. The rule is interpreted step by step —
nothing in it is ever executed.

Everything the rule produces still passes the §6.4 validation gate and the
same-origin pagination checks, so a wrong or hostile rule surfaces as rejected
candidates, never as arbitrary behaviour.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from selectolax.parser import HTMLParser

from ..fetch import Fetcher, canonical_url
from ..gate import extract_candidates, validate_candidates
from ..paginate import find_next_page, page_urls
from ..rules import ExtractRule, parse_rule
from ..types import Candidate
from .base import DiscoveryResult

log = logging.getLogger(__name__)


def _dig(payload, path: str):
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if isinstance(current, list):
        current = current[0] if current else None
    return current


class RuleAdapter:
    def __init__(self, fetcher: Fetcher, *, renderer=None, **_ignored) -> None:
        self._fetcher = fetcher
        self._renderer = renderer

    def discover(self, source: dict) -> DiscoveryResult:
        raw = source.get("extraction_rules")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return DiscoveryResult(error="extraction_rules is not valid JSON")
        if not raw:
            return DiscoveryResult(error="type=rule source has no extraction_rules")

        try:
            rule = parse_rule(raw)
        except (ValidationError, ValueError) as exc:
            return DiscoveryResult(error=f"invalid extraction rule: {str(exc)[:200]}")

        if not rule.is_usable():
            return DiscoveryResult(error="extraction rule describes no way to find items")

        base_url = rule.request.url or source.get("list_url") or source["url"]
        diagnostics: dict = {"adapter": "rule", "api_style": rule.is_api_style()}

        if rule.is_api_style():
            return self._discover_api(base_url, rule, diagnostics)
        return self._discover_html(base_url, rule, diagnostics)

    # — HTML rules —

    def _fetch_html(self, url: str, rule: ExtractRule) -> str | None:
        if rule.render:
            if self._renderer is None:
                return None
            from ..render import RenderError

            if not self._fetcher.allowed(url):
                return None
            try:
                html = self._renderer.render(url)
            except RenderError:
                return None
            self._fetcher.pages_fetched += 1
            return html
        response = self._fetcher.get(url)
        return response.text if response else None

    def _extract_page(self, html: str, url: str, rule: ExtractRule) -> list[Candidate]:
        if not rule.item_selector:
            return []
        found = extract_candidates(html, rule.item_selector, url)
        accepted, _ = validate_candidates(found, url)
        return accepted

    def _discover_html(self, base_url, rule, diagnostics) -> DiscoveryResult:
        html = self._fetch_html(base_url, rule)
        if html is None:
            return DiscoveryResult(
                diagnostics=diagnostics, error=f"could not fetch listing: {base_url}"
            )

        candidates: list[Candidate] = []
        seen: set[str] = set()

        # Page 1, plus the rule's pagination if it declares any.
        if rule.next_page_selector:
            urls = _rule_page_urls(
                html, base_url, rule,
                fetch=lambda u: self._fetch_html(u, rule),
            )
        else:
            urls = [base_url]

        for i, page_url in enumerate(urls[: rule.max_pages]):
            page_html = html if i == 0 else self._fetch_html(page_url, rule)
            if not page_html:
                continue
            for c in self._extract_page(page_html, page_url, rule):
                if c.url not in seen:
                    seen.add(c.url)
                    candidates.append(c)

        diagnostics["pages_scanned"] = min(len(urls), rule.max_pages)
        diagnostics["candidates_found"] = len(candidates)

        if not candidates:
            return DiscoveryResult(diagnostics=diagnostics, error="rule found no candidates")
        return DiscoveryResult(
            candidates=candidates, selector_used=rule.item_selector, diagnostics=diagnostics
        )

    # — API rules —

    def _discover_api(self, base_url, rule, diagnostics) -> DiscoveryResult:
        req = rule.request
        if req.method == "POST":
            files = None
            if req.multipart:
                files = {
                    name: ("blob", json.dumps(value), "application/json")
                    if not isinstance(value, str)
                    else (None, value)
                    for name, value in req.multipart.items()
                }
            response = (
                self._fetcher.post(base_url, files=files)
                if files
                else self._fetcher.post(base_url, json=req.json_body or {})
            )
        else:
            response = self._fetcher.get(base_url)

        if response is None:
            return DiscoveryResult(diagnostics=diagnostics, error="API request failed")
        try:
            payload = response.json()
        except ValueError:
            return DiscoveryResult(diagnostics=diagnostics, error="API response was not JSON")

        rows = _dig(payload, rule.results_path) if rule.results_path else payload
        if not isinstance(rows, list):
            rows = payload.get(rule.results_path) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return DiscoveryResult(diagnostics=diagnostics, error="results_path did not resolve to a list")

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for row in rows:
            raw_url = _dig(row, rule.item_url_path) if rule.item_url_path else row.get("url")
            if not raw_url:
                continue
            absolute = canonical_url(str(raw_url), base=base_url)
            if absolute in seen:
                continue
            seen.add(absolute)
            title = _dig(row, rule.item_title_path) if rule.item_title_path else row.get("title")
            candidates.append(
                Candidate(
                    url=absolute,
                    title=str(title).strip() if title else None,
                    payload=row if isinstance(row, dict) else None,
                )
            )

        diagnostics["candidates_found"] = len(candidates)
        if not candidates:
            return DiscoveryResult(diagnostics=diagnostics, error="API rule found no candidates")
        return DiscoveryResult(candidates=candidates, diagnostics=diagnostics)


def _rule_page_urls(first_html, first_url, rule, *, fetch):
    """Follow the rule's next_page_selector, bounded by max_pages and a cycle
    guard — the same navigation-only discipline as the built-in pager (§6.4)."""
    urls = [canonical_url(first_url)]
    seen = {urls[0]}
    html, url = first_html, first_url
    while len(urls) < rule.max_pages:
        nxt = _next_by_selector(html, url, rule.next_page_selector)
        if nxt is None or nxt in seen:
            break
        html = fetch(nxt)
        if html is None:
            break
        urls.append(nxt)
        seen.add(nxt)
        url = nxt
    return urls


def _next_by_selector(html: str, current_url: str, selector: str) -> str | None:
    """The 'next page' href from a rule-supplied selector, same-origin only."""
    from urllib.parse import urlparse

    tree = HTMLParser(html)
    node = tree.css_first(selector)
    if node is None:
        return None
    anchor = node if node.tag == "a" else node.css_first("a")
    href = anchor.attributes.get("href") if anchor else None
    if not href or href.startswith(("#", "javascript:")):
        return None
    candidate = canonical_url(href, base=current_url)
    if urlparse(candidate).netloc != urlparse(current_url).netloc:
        return None
    return candidate if candidate != canonical_url(current_url) else None
