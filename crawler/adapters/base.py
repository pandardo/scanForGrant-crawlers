"""The adapter contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..types import Candidate

__all__ = ["Adapter", "Candidate", "DiscoveryResult"]


@dataclass
class DiscoveryResult:
    """What an adapter found, plus why it found nothing (§6.5)."""

    candidates: list[Candidate] = field(default_factory=list)
    # Selector that produced these candidates, cached to sources.list_selector.
    selector_used: str | None = None
    # The reuse-pool strategy that produced them, if it came from rung 2 (§6.4).
    strategy_id: str | None = None
    # CMS/DOM signature of the page, for scoping reuse.
    fingerprint: str | None = None
    # Track record to credit/blame once the outcome is known.
    worked_strategy_id: str | None = None
    failed_strategy_ids: list[str] = field(default_factory=list)
    diagnostics: dict[str, object] = field(default_factory=dict)
    error: str | None = None


class Adapter(Protocol):
    """Fetches a source's list page and discovers candidate detail URLs."""

    def discover(self, source: dict) -> DiscoveryResult:
        """Find candidate grant URLs for a source."""
        ...
