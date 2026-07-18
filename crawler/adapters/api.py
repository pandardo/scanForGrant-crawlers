"""JSON API adapter (§6.2 `api`).

Some portals are not scrapeable at all: the EU Funding & Tenders portal serves a
cookie-consent wall to any fetcher, and its calls list is rendered client-side
from a POST search API. No selector can find content that is never in the DOM.

This adapter talks to those APIs directly. Discovery costs no LLM call — the API
returns structured results, so candidates come straight from JSON, the same
economics as the `rss` adapter.

Per-source shape lives in `sources.api_config` (jsonb) rather than in code, so
adding another JSON-API portal is a config change, not a deploy — §6.2's
"registry makes this a config change" principle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..fetch import Fetcher, canonical_url
from ..types import Candidate
from .base import DiscoveryResult

log = logging.getLogger(__name__)

# Cap pages so a source with thousands of results cannot spend an entire run.
MAX_PAGES = 5
DEFAULT_PAGE_SIZE = 50


def _dig(payload: Any, path: str) -> Any:
    """Read a dotted path out of nested JSON, tolerating missing keys.

    API responses nest inconsistently (metadata fields often arrive as
    single-element lists), so this unwraps those rather than forcing every
    config to describe them.
    """
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


class ApiAdapter:
    """Fetches candidates from a JSON API described by sources.api_config.

    api_config keys:
      method        "GET" | "POST"                 (default GET)
      url           override for source.url        (optional)
      multipart     dict of field -> JSON value, sent as multipart/form-data
                    with filename="blob" (what the EU SEDIA API requires)
      json_body     dict sent as a JSON body       (alternative to multipart)
      results_path  dotted path to the result array (default "results")
      title_path    dotted path to a result's title (default "title")
      url_path      dotted path to a result's URL   (default "url")
      page_param    query param for paging          (optional)
      page_size     results per page                (default 50)
    """

    def __init__(self, fetcher: Fetcher, **_ignored) -> None:
        self._fetcher = fetcher

    def discover(self, source: dict) -> DiscoveryResult:
        config: dict = source.get("api_config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                return DiscoveryResult(error="api_config is not valid JSON")

        base_url = config.get("url") or source.get("list_url") or source["url"]
        method = (config.get("method") or "GET").upper()
        results_path = config.get("results_path", "results")
        title_path = config.get("title_path", "title")
        url_path = config.get("url_path", "url")
        page_param = config.get("page_param")
        page_size = int(config.get("page_size", DEFAULT_PAGE_SIZE))

        candidates: list[Candidate] = []
        seen: set[str] = set()
        diagnostics: dict[str, Any] = {"adapter": "api", "method": method, "pages_fetched": 0}
        total_reported: int | None = None

        for page in range(1, MAX_PAGES + 1):
            url = base_url
            if page_param and page > 1:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{page_param}={page}"

            payload = self._request(url, method, config)
            if payload is None:
                if page == 1:
                    diagnostics["error"] = "request failed"
                    return DiscoveryResult(diagnostics=diagnostics, error=f"API request failed: {url}")
                break

            diagnostics["pages_fetched"] = page
            if total_reported is None:
                total_reported = payload.get("totalResults") if isinstance(payload, dict) else None

            rows = _dig(payload, results_path) if results_path else payload
            # _dig unwraps single-element lists; the result array must stay a list.
            if not isinstance(rows, list):
                rows = payload.get(results_path) if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                break

            for row in rows:
                raw_url = _dig(row, url_path) or row.get("url")
                if not raw_url:
                    continue
                absolute = canonical_url(str(raw_url), base=base_url)
                if absolute in seen:
                    continue
                seen.add(absolute)

                title = _dig(row, title_path)
                if not title:
                    metadata = row.get("metadata") if isinstance(row, dict) else None
                    if isinstance(metadata, dict):
                        title = _dig(metadata, "title")
                # Carry the API row itself: the EU portal's detail pages are
                # behind the same cookie wall as its listing, but the search
                # response already holds title, budget, deadline and description.
                candidates.append(
                    Candidate(
                        url=absolute,
                        title=str(title).strip() if title else None,
                        payload=row if isinstance(row, dict) else None,
                    )
                )

            if len(rows) < page_size:
                break  # last page

        diagnostics["total_reported"] = total_reported
        diagnostics["candidates_found"] = len(candidates)

        if not candidates:
            return DiscoveryResult(
                diagnostics=diagnostics,
                error="API returned no usable results — check api_config paths",
            )

        return DiscoveryResult(candidates=candidates, diagnostics=diagnostics)

    def _request(self, url: str, method: str, config: dict) -> dict | None:
        if method == "GET":
            response = self._fetcher.get(url)
            if response is None:
                return None
            try:
                return response.json()
            except ValueError:
                log.warning("API response was not JSON: %s", url)
                return None

        # POST. Multipart parts are sent with filename="blob" and an explicit
        # JSON content type — the EU SEDIA API rejects them otherwise, which is
        # the kind of detail only visible by capturing what the real site sends.
        multipart = config.get("multipart")
        json_body = config.get("json_body")

        try:
            if multipart:
                files = {
                    name: ("blob", json.dumps(value), "application/json")
                    if not isinstance(value, str)
                    else (None, value)
                    for name, value in multipart.items()
                }
                response = self._fetcher.post(url, files=files)
            else:
                response = self._fetcher.post(url, json=json_body or {})
        except Exception as exc:
            log.warning("API POST failed for %s: %s", url, type(exc).__name__)
            return None

        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            log.warning("API response was not JSON: %s", url)
            return None
