"""Pagination discovery (§6.4).

A grant list often spans several pages. Sardegna Ricerche's bandi list is ~10
pages of plain <a href> links, and real grants sit on page 2+ — invisible to a
crawler that only reads page 1. That was a real miss.

This is navigation, not interaction: a "next page" link is an href we fetch, the
same as any other. No clicking, no JS driving (§6.4). Each page is run through the
same selector + gate, and the loop is bounded so a malformed pager cannot spin
forever.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from .fetch import canonical_url

log = logging.getLogger(__name__)

# Text that marks a "go to the next page" control, across the phrasings Italian
# public portals actually use. Matched case-insensitively against link text.
_NEXT_TEXTS = (
    "avanti", "successiva", "successivo", "prossima", "pagina successiva",
    "next", "»", "›", "→",
)

# rel="next" is the unambiguous signal when present.
_NEXT_REL = "next"

# Hard cap: no real bandi list runs longer than this, and it stops a broken or
# hostile pager from looping (§6.4 bounded depth).
MAX_PAGES = 20


def find_next_page(html: str, current_url: str) -> str | None:
    """The URL of the next list page, or None if this is the last one.

    Prefers rel="next", then a link whose text is a known "next" marker. Returns
    an absolute, canonicalised URL, and never the page we are already on (some
    pagers point "next" back at the current page on the final page).
    """
    tree = HTMLParser(html)
    current = canonical_url(current_url)

    # 1. rel="next" — the explicit, unambiguous signal.
    for node in tree.css('a[rel~="next"], link[rel~="next"]'):
        href = node.attributes.get("href")
        if href:
            candidate = canonical_url(href, base=current_url)
            if candidate != current and _same_site(candidate, current_url):
                return candidate

    # 2. A link whose text reads as "next".
    for node in tree.css("a"):
        text = node.text(strip=True).lower()
        if not text:
            # Some "next" controls are an icon with an aria-label instead.
            text = (node.attributes.get("aria-label") or node.attributes.get("title") or "").lower()
        if not text:
            continue

        if any(marker in text for marker in _NEXT_TEXTS):
            href = node.attributes.get("href")
            if not href or href.startswith(("#", "javascript:")):
                continue
            candidate = canonical_url(href, base=current_url)
            if candidate != current and _same_site(candidate, current_url) and _is_page_link(candidate):
                return candidate

    return None


# A "next" control must lead to another list page, not a document. On Sardegna
# Ricerche a link with a "→" glyph pointed at a .pdf, which the renderer cannot
# open — following it crashed the whole scan.
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rtf", ".odt")


def _is_page_link(url: str) -> bool:
    return not urlparse(url).path.lower().endswith(_DOCUMENT_SUFFIXES)


def _same_site(url: str, base: str) -> bool:
    """Pagination must stay on the same host — a hop off-site is not page 2."""
    return urlparse(url).netloc == urlparse(base).netloc


def page_urls(first_html: str, first_url: str, fetch) -> list[str]:
    """Walk the pager from the first page, yielding each page URL in order.

    `fetch(url) -> html | None` renders/fetches a page. Bounded by MAX_PAGES and
    by a seen-set, so a pager that cycles cannot loop. The first page is always
    included; subsequent pages are appended as the "next" chain is followed.

    Returns the list of page URLs actually reachable (including the first).
    """
    urls = [canonical_url(first_url)]
    seen = {urls[0]}

    html = first_html
    url = first_url

    while len(urls) < MAX_PAGES:
        nxt = find_next_page(html, url)
        if nxt is None or nxt in seen:
            break

        next_html = fetch(nxt)
        if next_html is None:
            log.info("pagination stopped: could not fetch %s", nxt)
            break

        urls.append(nxt)
        seen.add(nxt)
        html = next_html
        url = nxt

    if len(urls) >= MAX_PAGES:
        # Never truncate silently (§6 no-silent-caps): say so.
        log.warning("pagination hit the %d-page cap for %s", MAX_PAGES, first_url)

    return urls
