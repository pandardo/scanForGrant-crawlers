"""Source adapters (§6.2), selected by `sources.type`."""

from __future__ import annotations

from .api import ApiAdapter
from .base import Adapter, Candidate, DiscoveryResult
from .html import HtmlAdapter
from .html_js import HtmlJsAdapter
from .rss import RssAdapter
from .rule import RuleAdapter

ADAPTERS: dict[str, type] = {
    "rss": RssAdapter,
    "api": ApiAdapter,
    "html": HtmlAdapter,
    "html_js": HtmlJsAdapter,
    "rule": RuleAdapter,
}


def adapter_for(source_type: str) -> type:
    """An unknown type falls back to plain html."""
    return ADAPTERS.get(source_type, HtmlAdapter)


def build_adapter(source: dict, *, fetcher, strategies=None, llm=None, renderer=None):
    """Construct the right adapter with only the arguments it accepts.

    rss takes none of the ladder machinery: feed entries are the candidates, so
    there is no selector to infer and no LLM call to spend (§6.2).
    """
    source_type = source.get("type", "html")
    adapter_class = adapter_for(source_type)

    # rss and api need none of the ladder machinery: their responses are already
    # structured, so there is no selector to infer and no LLM call to spend (§6.2).
    if adapter_class in (RssAdapter, ApiAdapter):
        return adapter_class(fetcher)

    if adapter_class is HtmlJsAdapter:
        return adapter_class(fetcher, strategies=strategies, llm=llm, renderer=renderer)

    if adapter_class is RuleAdapter:
        return adapter_class(fetcher, renderer=renderer)

    return adapter_class(fetcher, strategies=strategies, llm=llm)


__all__ = [
    "Adapter",
    "ApiAdapter",
    "Candidate",
    "DiscoveryResult",
    "HtmlAdapter",
    "HtmlJsAdapter",
    "RssAdapter",
    "RuleAdapter",
    "adapter_for",
    "build_adapter",
    "ADAPTERS",
]
