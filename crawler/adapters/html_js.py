"""html_js adapter: Playwright renders, then it is the same ladder as `html`.

Rendering is the ONLY difference. Everything after it — the ladder, the gate, the
selectors — is shared, because a rendered DOM is just a DOM. That is why this is
a two-method subclass rather than a parallel implementation.
"""

from __future__ import annotations

import logging

from ..fetch import Fetcher
from ..render import RenderError, Renderer
from .base import DiscoveryResult
from .html import HtmlAdapter

log = logging.getLogger(__name__)


class HtmlJsAdapter(HtmlAdapter):
    def __init__(
        self,
        fetcher: Fetcher,
        *,
        strategies: list[dict] | None = None,
        llm=None,
        renderer: Renderer | None = None,
    ) -> None:
        super().__init__(fetcher, strategies=strategies, llm=llm)
        self._renderer = renderer

    def _get_html(self, url: str) -> tuple[str | None, str | None]:
        if self._renderer is None:
            return None, "html_js source needs a renderer, none was provided"

        # robots.txt still applies: rendering is a fetch with more steps.
        if not self._fetcher.allowed(url):
            return None, f"robots.txt disallows {url}"

        try:
            html = self._renderer.render(url)
        except RenderError as exc:
            return None, str(exc)

        self._fetcher.pages_fetched += 1
        return html, None

    def discover(self, source: dict) -> DiscoveryResult:
        result = super().discover(source)
        result.diagnostics["rendered"] = True
        return result
