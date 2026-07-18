"""Candidate extraction and the §6.4 validation gate.

Lives beside the ladder rather than in the html adapter: the gate is what decides
whether a *rung* worked, so it is ladder machinery. Keeping it in the adapter
made html -> ladder -> html a circular import.
"""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from .types import Candidate
from .fetch import canonical_url

log = logging.getLogger(__name__)


# Link text that is chrome, not a grant. Matched case-insensitively, exact.
CHROME_TITLES = frozenset(
    {
        "home", "contatti", "privacy", "cookie", "cookie policy", "accedi", "login",
        "registrati", "cerca", "menu", "torna su", "leggi tutto", "continua",
        "vai al contenuto", "mappa del sito", "note legali", "amministrazione trasparente",
        "seguici", "newsletter", "faq", "aiuto", "english", "italiano",
    }
)

# A list page with hundreds of matches is matching the nav, not the grants.
MAX_PLAUSIBLE_CANDIDATES = 200
MIN_PLAUSIBLE_CANDIDATES = 2


def _path_prefix(url: str, depth: int = 2) -> str:
    """The first `depth` path segments — the "section" a URL belongs to.

    /atti-bandi-archivi/atti-amministrativi/bandi/123 -> atti-bandi-archivi/atti-amministrativi
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    return "/".join(parts[:depth])


def _looks_like_detail_url(url: str, base_domain: str) -> bool:
    parsed = urlparse(url)

    # Off-domain links on a list page are partners, socials, or the CMS vendor.
    if parsed.netloc and parsed.netloc != base_domain:
        return False

    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return False

    # A detail page has a path with substance, not just /bandi.
    return len([p for p in path.split("/") if p]) >= 2 or bool(parsed.query)


def validate_candidates(candidates: list[Candidate], base_url: str) -> tuple[list[Candidate], dict]:
    """The §6.4 validation gate.

    Non-empty is NOT success: a selector matching the nav returns links too. This
    filters to candidates that plausibly are grant detail pages, and reports what
    it rejected so a failure is debuggable rather than mysterious.

    Returns (accepted, diagnostics).
    """
    base_domain = urlparse(base_url).netloc.lower()

    seen: set[str] = set()
    accepted: list[Candidate] = []
    rejected_chrome = 0
    rejected_shape = 0
    rejected_duplicate = 0

    for candidate in candidates:
        title = (candidate.title or "").strip().lower()
        if title and title in CHROME_TITLES:
            rejected_chrome += 1
            continue

        if not _looks_like_detail_url(candidate.url, base_domain):
            rejected_shape += 1
            continue

        if candidate.url in seen:
            rejected_duplicate += 1
            continue

        seen.add(candidate.url)
        accepted.append(candidate)

    # Cohesion check. A grant list points at sibling detail pages, so its links
    # share a path prefix (/bandi/x, /bandi/y). A selector that is too broad
    # sweeps in unrelated sections — on Regione Sardegna, Stage A proposed
    # "div.search-body > div > div", which matched 18 real bandi AND 20
    # department pages. Every one was on-domain with a plausible path, so the
    # checks above accepted them all. Keeping only the dominant prefix drops the
    # strays without needing to know what a "bando" URL looks like per portal.
    rejected_incohesive = 0
    if len(accepted) >= 4:
        prefixes = Counter(_path_prefix(c.url) for c in accepted)
        ranked = prefixes.most_common()
        dominant, count = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0

        # Plurality, not majority. On Regione Sardegna the real bandi were 24 of
        # 48 — a clear leading group, but the strays fragmented across many small
        # ones, so a >50% rule never fired. What identifies the list is that its
        # prefix dwarfs the next one, not that it owns the page.
        if count >= 3 and count >= runner_up * 2:
            cohesive = [c for c in accepted if _path_prefix(c.url) == dominant]
            rejected_incohesive = len(accepted) - len(cohesive)
            accepted = cohesive

    diagnostics = {
        "candidates_before_gate": len(candidates),
        "candidates_accepted": len(accepted),
        "rejected_chrome": rejected_chrome,
        "rejected_url_shape": rejected_shape,
        "rejected_duplicate": rejected_duplicate,
        "rejected_incohesive": rejected_incohesive,
    }

    # Too many matches means the selector caught the whole page, not a list.
    if len(accepted) > MAX_PLAUSIBLE_CANDIDATES:
        diagnostics["gate_verdict"] = "implausible_count_high"
        return [], diagnostics

    if len(accepted) < MIN_PLAUSIBLE_CANDIDATES:
        diagnostics["gate_verdict"] = "implausible_count_low"
        return [], diagnostics

    diagnostics["gate_verdict"] = "accepted"
    return accepted, diagnostics


def extract_candidates(html: str, selector: str, base_url: str) -> list[Candidate]:
    """Links matching `selector`, or inside elements matching it."""
    tree = HTMLParser(html)
    candidates: list[Candidate] = []

    for node in tree.css(selector):
        # The selector may point at the <a> itself or at a wrapping list item.
        anchors = [node] if node.tag == "a" else node.css("a")
        for anchor in anchors:
            href = anchor.attributes.get("href")
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            candidates.append(
                Candidate(
                    url=canonical_url(href, base=base_url),
                    title=anchor.text(strip=True) or None,
                )
            )

    return candidates


