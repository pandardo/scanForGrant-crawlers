"""Extraction rules — the constrained, INTERPRETED format for hard sources
(§6.4 Track 2).

A rule is DATA, not code. It describes how to pull candidates from a source that
the selector ladder cannot handle on its own: a paginated listing, a POST-driven
search, a nested field layout. The crawler *interprets* a rule; it never
executes anything the rule contains.

This is the safe middle of §6.4: richer than a bare CSS selector, but with the
same security property — the worst a malicious or wrong rule can do is select the
wrong elements or hit the wrong URL (which the validation gate and same-origin
checks catch), never run arbitrary code. Generated Python (a real adapter) is the
rare fallback for logic no rule can express, and it goes through human PR review.

A rule is validated against this schema before it is ever used. Anything it does
not recognise is rejected, so a generated rule cannot smuggle in an unexpected
field that some future interpreter might honour.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RequestRule(BaseModel):
    """How to fetch the listing. Defaults to a plain GET of the source URL."""

    method: str = "GET"
    # For POST/api-style sources: a JSON body or multipart field map. Values are
    # inert data echoed into the request; they are never evaluated.
    json_body: dict | None = None
    multipart: dict | None = None
    # Override the URL the source row carries, when the listing lives elsewhere.
    url: str | None = None

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        upper = value.upper()
        if upper not in ("GET", "POST"):
            raise ValueError(f"unsupported method {value!r}; only GET and POST")
        return upper


class ExtractRule(BaseModel):
    """The full extraction recipe for one source.

    Every field is a selector or a dotted JSON path — inert. There is deliberately
    no field that names code, a Python expression, or a callable.
    """

    # HTML path: the repeating element, and where the link/title sit within it.
    item_selector: str | None = Field(default=None, max_length=200)
    link_selector: str | None = Field(default=None, max_length=200)
    title_selector: str | None = Field(default=None, max_length=200)

    # JSON path (api-style): dotted paths into the response.
    results_path: str | None = Field(default=None, max_length=100)
    item_url_path: str | None = Field(default=None, max_length=100)
    item_title_path: str | None = Field(default=None, max_length=100)

    # Pagination: a selector for the "next" link (HTML) or a page query param (API).
    next_page_selector: str | None = Field(default=None, max_length=200)
    page_param: str | None = Field(default=None, max_length=60)
    max_pages: int = Field(default=5, ge=1, le=20)

    # Whether the listing needs a rendered browser (JS) or plain fetch.
    render: bool = False

    request: RequestRule = Field(default_factory=RequestRule)

    # Free-text note from whoever/whatever drafted the rule — for the PR reviewer.
    note: str | None = Field(default=None, max_length=500)

    @field_validator(
        "item_selector", "link_selector", "title_selector", "next_page_selector"
    )
    @classmethod
    def _plausible_selector(cls, value: str | None) -> str | None:
        """A selector is inert, but still shape-checked before it is stored and
        used — no angle brackets, no javascript:, nothing exotic."""
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        lowered = candidate.lower()
        if any(bad in lowered for bad in ("javascript:", "<script", "expression(", "url(")):
            raise ValueError("selector contains a forbidden token")
        return candidate

    def is_api_style(self) -> bool:
        """A rule is API-style when it reads JSON paths rather than selectors."""
        return bool(self.results_path or self.item_url_path)

    def is_usable(self) -> bool:
        """A rule must describe at least one way to find items."""
        return bool(self.item_selector or self.results_path)


def parse_rule(raw: object) -> ExtractRule:
    """Validate untrusted rule JSON into an ExtractRule.

    Raises pydantic.ValidationError on anything malformed or containing unknown
    fields — the caller quarantines it rather than trusting it.
    """
    if isinstance(raw, ExtractRule):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("extraction rule must be a JSON object")
    # extra='forbid' via model_config would be stricter, but we want forward-compat
    # on additive fields; the validators above reject the dangerous shapes.
    return ExtractRule.model_validate(raw)
