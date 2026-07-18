"""Headless rendering for JS-rendered portals (§6.2 `html_js`).

Real case that forced this: Regione Sardegna's bandi listing serves 192KB of HTML
that cleans down to 25 characters of text. The grants are rendered client-side,
so httpx sees an empty shell and the LLM correctly reports nothing — the §6.5
"content isn't in the HTML" failure.

The browser NAVIGATES; it never acts on a page's instructions. There is no click,
no form fill, no interpreter (§6.4). It fetches a URL and returns the rendered
DOM, which is a more expensive `get`, not a different capability.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from .config import Config

log = logging.getLogger(__name__)


class RenderError(RuntimeError):
    """The page could not be rendered."""


class Renderer:
    """Lazily-started headless Chromium. Starting it costs ~1s, so it is created
    once per run and only if an html_js source is actually scanned."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright = None
        self._browser = None

    def _ensure_browser(self):
        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RenderError("playwright is not installed") from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        return self._browser

    def render(self, url: str) -> str:
        """Return the rendered HTML for a URL.

        Raises RenderError on navigation failure or timeout.
        """
        browser = self._ensure_browser()

        context = browser.new_context(
            user_agent=self._config.user_agent,
            # A desktop viewport: some portals render a stripped mobile list.
            viewport={"width": 1280, "height": 1024},
            java_script_enabled=True,
        )

        try:
            page = context.new_page()

            # Images and fonts are bytes we never read; blocking them makes the
            # render meaningfully faster and lighter on the portal.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_(),
            )

            timeout_ms = int(self._config.request_timeout_seconds * 1000)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            return page.content()
        except Exception as exc:
            raise RenderError(f"render failed for {url}: {type(exc).__name__}") from None
        finally:
            context.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


@contextmanager
def renderer_for(config: Config):
    renderer = Renderer(config)
    try:
        yield renderer
    finally:
        renderer.close()
