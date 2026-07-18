"""Schema for Stage B extraction output.

The LLM's response is untrusted input: it is coerced and validated here before
anything reaches the database. A response that fails validation is retried once
with the error in-prompt, then quarantined (§6.3).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

# §6.6. Stage B classifies Italian source text into this English taxonomy.
CATEGORIES = (
    "Digital & Innovation",
    "Sustainability",
    "Startups & SMEs",
    "Research & Development",
    "Culture & Tourism",
    "Internationalization",
    "Training",
    "Agriculture",
    "Other",
)


class GrantExtraction(BaseModel):
    """One grant, as extracted from a detail page."""

    title: str = Field(min_length=1, max_length=500)
    issuer: str | None = Field(default=None, max_length=300)
    category: str = "Other"
    description: str | None = Field(default=None, max_length=4000)
    requirements: list[str] = Field(default_factory=list)
    funding_text: str | None = Field(default=None, max_length=200)
    funding_min: float | None = None
    funding_max: float | None = None
    deadline: date | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _known_category(cls, value: Any) -> str:
        """An unrecognised category becomes 'Other' rather than failing the row.

        The taxonomy exists to keep the archive filter meaningful; a grant with a
        surprising label is still worth having.
        """
        if not isinstance(value, str):
            return "Other"
        for known in CATEGORIES:
            if value.strip().lower() == known.lower():
                return known
        return "Other"

    @field_validator("deadline", mode="before")
    @classmethod
    def _parse_deadline(cls, value: Any) -> Any:
        """Accept ISO dates; treat 'a sportello'/rolling and junk as absent.

        Many Italian grants have no deadline at all (§5), so None is a normal
        outcome here, not an error.
        """
        if value in (None, "", "null", "N/A", "n/a"):
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return text
            return None
        return None

    @field_validator("requirements", mode="before")
    @classmethod
    def _clean_requirements(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()][:20]

    @field_validator("funding_min", "funding_max", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        """Amounts arrive as numbers or as '€10.000' / '10000,50' strings."""
        if value in (None, "", "null"):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            # Italian formatting: '.' groups thousands, ',' is the decimal mark.
            cleaned = re.sub(r"[^\d,.]", "", value).replace(".", "").replace(",", ".")
            try:
                return float(cleaned) if cleaned else None
            except ValueError:
                return None
        return None


class RelevanceScore(BaseModel):
    topic_id: str
    score: float = Field(ge=0.0, le=1.0)


class StageBResponse(BaseModel):
    """The full Stage B payload: the grant plus its topic scores."""

    grant: GrantExtraction
    relevance: list[RelevanceScore] = Field(default_factory=list)
