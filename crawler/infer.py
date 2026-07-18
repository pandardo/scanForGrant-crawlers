"""Stage A: infer a list selector from a page (§6.3, §6.4 rung 3).

The LLM reads HTML from a site we do not control and proposes a CSS selector.
That output is DATA, not code: we never execute it, we only pass it to
selectolax's `css()`. The worst a hostile page can achieve is a selector that
matches the wrong elements — which the validation gate then rejects. This is the
whole reason §6.4 caches selectors rather than generated modules.

Stage A is the expensive rung and runs only when the cheaper ones fail.
"""

from __future__ import annotations

import logging
import re

from .llm import LLMClient, LLMError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You find the CSS selector for a list of grant ("bandi") links on an Italian public-sector page.

Return ONLY:
{"list_selector": "css selector or null", "reasoning": "one short sentence"}

Rules:
- The selector must match the REPEATING element wrapping each grant in the list
  (e.g. "li.bando-item", "article.card", "div.result-row", "h3.title").
- Prefer a selector on the list container's children over a bare tag name.
- It must NOT match navigation, footer, sidebar, breadcrumb or pagination links.
- If the page has no list of grants (it is an article, an error page, or the
  content is not present), return {"list_selector": null}.
- Use only class names, tag names, ids and attributes visible in the HTML given."""

# A selector is inert data, but it still gets a shape check before it is stored
# and reused: no pseudo-elements, no scripts, nothing exotic. This is belt and
# braces on top of "we never execute it".
_SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9\s.,#\[\]='\"_:()>+~*-]{1,200}$")
_FORBIDDEN = ("javascript:", "expression(", "<", ">alert", "url(")


def is_plausible_selector(selector: str) -> bool:
    """Shape check on an LLM-proposed selector before caching it."""
    if not selector or not selector.strip():
        return False
    candidate = selector.strip()
    if len(candidate) > 200:
        return False
    if any(bad in candidate.lower() for bad in _FORBIDDEN):
        return False
    return bool(_SAFE_SELECTOR.match(candidate))


def _condense_html(html: str, max_chars: int = 18_000) -> str:
    """Structure-only view of the page: attributes matter, prose does not.

    Stage A needs to see tags and classes, not paragraphs. Stripping text keeps
    far more of the DOM inside the token budget than raw HTML would.
    """
    condensed = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Collapse long text nodes; keep a stub so the shape stays readable.
    condensed = re.sub(r">([^<]{40,})<", lambda m: f">{m.group(1)[:40]}…<", condensed)
    condensed = re.sub(r"\s+", " ", condensed)
    return condensed[:max_chars]


def infer_list_selector(llm: LLMClient, *, url: str, html: str) -> tuple[str | None, str]:
    """Ask the LLM for a list selector. Returns (selector, reasoning).

    Returns (None, reason) when the model finds no list or proposes something
    implausible. Raises LLMError on provider failure.
    """
    response = llm.complete_json(
        system=SYSTEM_PROMPT,
        user=f"URL: {url}\n\nHTML STRUCTURE:\n{_condense_html(html)}",
        max_tokens=300,
    )

    selector = response.get("list_selector")
    reasoning = str(response.get("reasoning", ""))[:200]

    if selector is None:
        return None, reasoning or "model found no grant list on the page"

    if not isinstance(selector, str) or not is_plausible_selector(selector):
        log.warning("stage A proposed an implausible selector for %s", url)
        return None, f"implausible selector rejected: {str(selector)[:60]}"

    return selector.strip(), reasoning
