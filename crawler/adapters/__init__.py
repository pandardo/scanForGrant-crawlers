"""Source adapters (§6.2), selected by `sources.type`."""

from __future__ import annotations

from .base import Adapter, Candidate, DiscoveryResult
from .html import HtmlAdapter
from .html_js import HtmlJsAdapter
from .rss import RssAdapter

ADAPTERS: dict[str, type] = {
    "rss": RssAdapter,
    "html": HtmlAdapter,
    "html_js": HtmlJsAdapter,
}


def adapter_for(source_type: str) -> type:
    """`api` is not built yet; an unknown type falls back to plain html."""
    return ADAPTERS.get(source_type, HtmlAdapter)


def build_adapter(source: dict, *, fetcher, strategies=None, llm=None, renderer=None):
    """Construct the right adapter with only the arguments it accepts.

    rss takes none of the ladder machinery: feed entries are the candidates, so
    there is no selector to infer and no LLM call to spend (§6.2).
    """
    source_type = source.get("type", "html")
    adapter_class = adapter_for(source_type)

    if adapter_class is RssAdapter:
        return adapter_class(fetcher)

    if adapter_class is HtmlJsAdapter:
        return adapter_class(fetcher, strategies=strategies, llm=llm, renderer=renderer)

    return adapter_class(fetcher, strategies=strategies, llm=llm)


__all__ = [
    "Adapter",
    "Candidate",
    "DiscoveryResult",
    "HtmlAdapter",
    "HtmlJsAdapter",
    "RssAdapter",
    "adapter_for",
    "build_adapter",
    "ADAPTERS",
]
