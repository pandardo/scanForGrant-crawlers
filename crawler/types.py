"""Shared types.

`Candidate` lives here, not in adapters/, because both the adapters and the
ladder need it: importing it from adapters/ meant ladder -> adapters/__init__ ->
html -> ladder, a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """A URL that might be a grant detail page.

    `payload` carries structured data the discovery step already has (JSON-API
    sources). When present the pipeline extracts from it instead of fetching the
    detail page — essential for portals whose detail pages are behind a cookie
    wall, where a fetch returns nothing usable.
    """

    url: str
    title: str | None = None
    payload: dict | None = None
