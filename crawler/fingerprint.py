"""CMS/DOM fingerprinting for scoped selector reuse (§6.4).

Two portals share a selector because they share a CMS, not because they are both
regions. Without a fingerprint, "try every selector we have ever seen" gets slow
and raises the false-match rate — and a false match that passes the gate is the
silent-garbage failure §6.4 exists to prevent.

The fingerprint is a coarse bucket, not an identity: it narrows what to try
first. The validation gate still decides whether a selector actually worked.
"""

from __future__ import annotations

import hashlib
import re

from selectolax.parser import HTMLParser

# CMS platforms common on Italian public portals.
_GENERATOR_HINTS = (
    ("drupal", re.compile(r"drupal", re.I)),
    ("wordpress", re.compile(r"wordpress|wp-content", re.I)),
    ("joomla", re.compile(r"joomla", re.I)),
    ("liferay", re.compile(r"liferay", re.I)),
    ("typo3", re.compile(r"typo3", re.I)),
    ("plone", re.compile(r"plone", re.I)),
    # Bootstrap-italia is the design system mandated for Italian PA sites, so it
    # is a strong signal that two portals share markup conventions.
    ("bootstrap-italia", re.compile(r"bootstrap-italia", re.I)),
)


# CSS-module / build-hashed class names (style_data_title__ZN8de, css-1a2b3c)
# change on every site redeploy, so including them makes the fingerprint churn
# and the reuse pool accumulate a fresh copy of every strategy after each deploy.
# Match names carrying a hash segment.
_HASHED_CLASS = re.compile(r"__[A-Za-z0-9]{5,}$|^css-[a-z0-9]{5,}$|_[a-f0-9]{6,}$")


def _is_stable_class(cls: str) -> bool:
    """A class worth fingerprinting: structural, not state/utility/build-hashed."""
    if len(cls) <= 2:
        return False
    if cls.startswith(("js-", "is-", "has-")):
        return False
    return not _HASHED_CLASS.search(cls)


def _detect_platform(html: str, tree: HTMLParser) -> str:
    for node in tree.css("meta[name='generator']"):
        content = node.attributes.get("content", "") or ""
        for name, pattern in _GENERATOR_HINTS:
            if pattern.search(content):
                return name

    # No generator meta: fall back to markers in the markup itself.
    head = html[:20_000]
    for name, pattern in _GENERATOR_HINTS:
        if pattern.search(head):
            return name

    return "unknown"


def fingerprint_page(html: str) -> str:
    """A coarse signature of the page's platform and DOM shape.

    Deliberately coarse: it buckets pages that likely share markup conventions.
    Too specific and nothing ever matches; too loose and reuse tries selectors
    from unrelated sites.
    """
    tree = HTMLParser(html)
    platform = _detect_platform(html, tree)

    # The most common class names describe the page's structural vocabulary.
    # Two pages from the same CMS theme share most of them.
    counts: dict[str, int] = {}
    for node in tree.css("div, li, article, section"):
        classes = (node.attributes.get("class") or "").split()
        for cls in classes:
            if _is_stable_class(cls):
                counts[cls] = counts.get(cls, 0) + 1

    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    shape = ",".join(name for name, _ in top)

    digest = hashlib.sha256(shape.encode("utf-8")).hexdigest()[:12]
    return f"{platform}:{digest}"
