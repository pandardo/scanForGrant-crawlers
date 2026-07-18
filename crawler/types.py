"""Shared types.

`Candidate` lives here, not in adapters/, because both the adapters and the
ladder need it: importing it from adapters/ meant ladder -> adapters/__init__ ->
html -> ladder, a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """A URL that might be a grant detail page."""

    url: str
    title: str | None = None
