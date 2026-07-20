"""Track 2 drafting (§6.4): the LLM proposes an extraction rule for a hard source,
and the result is opened as a PR for a human to review and merge.

The output is a RULE (inert data), never executable code. The LLM sees the page
structure and proposes selectors / JSON paths; parse_rule validates the result
against the schema before it is written anywhere. A malformed or hostile proposal
is rejected here, not merged.

Nothing this module produces runs until a human merges the PR. That review IS the
safety mechanism — see §6.4 "Why not generated modules".
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from .clean import clean_html
from .llm import LLMClient
from .rules import ExtractRule, parse_rule

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write an extraction RULE (JSON) for an Italian grants page the
automatic selector inference could not handle. You never write code — only a rule
object the crawler interprets.

Return ONLY this JSON shape (omit fields you do not need):
{
  "item_selector": "CSS selector for each repeating grant/bando in the list",
  "link_selector": "optional: the <a> within an item",
  "title_selector": "optional: the title element within an item",
  "next_page_selector": "optional: CSS selector for the 'next page' link",
  "render": true/false,   // true if the list only appears after JavaScript runs
  "note": "one sentence for the human reviewer explaining the choice"
}

For a JSON-API page instead use:
{
  "results_path": "dotted path to the results array in the JSON",
  "item_url_path": "dotted path to each result's URL",
  "item_title_path": "dotted path to each result's title",
  "request": {"method": "POST", "json_body": {...}},
  "note": "..."
}

Rules:
- item_selector must match the REPEATING element wrapping each grant, not the nav,
  footer, sidebar or pagination.
- Use only classes/tags/ids visible in the HTML given.
- If the page has no grant list at all, return {"note": "no grant list found"}."""


class DraftResult:
    def __init__(self, rule: ExtractRule | None, reasoning: str, raw: dict):
        self.rule = rule
        self.reasoning = reasoning
        self.raw = raw

    @property
    def ok(self) -> bool:
        return self.rule is not None and self.rule.is_usable()


def draft_rule(llm: LLMClient, *, url: str, html: str) -> DraftResult:
    """Ask the LLM for an extraction rule. Validates before returning.

    The returned rule is guaranteed to have passed schema validation; an invalid
    proposal comes back as ok=False with the reason, never as an unvalidated dict.
    """
    cleaned = clean_html(html, max_chars=20_000)
    # The model needs structure, so send condensed HTML, not just cleaned text.
    structure = _condense(html)

    response = llm.complete_json(
        system=SYSTEM_PROMPT,
        user=(
            f"URL: {url}\n\n"
            f"VISIBLE TEXT (first 800 chars):\n{cleaned.text[:800]}\n\n"
            f"HTML STRUCTURE:\n{structure}"
        ),
        max_tokens=500,
    )

    note = str(response.get("note", ""))
    try:
        rule = parse_rule(response)
    except (ValidationError, ValueError) as exc:
        log.info("drafted rule failed validation for %s", url)
        return DraftResult(None, f"rule rejected by validation: {exc}", response)

    if not rule.is_usable():
        return DraftResult(None, note or "model found no grant list", response)

    return DraftResult(rule, note, response)


def _condense(html: str, max_chars: int = 16_000) -> str:
    """Structure-only view: tags and classes matter, prose does not."""
    import re

    condensed = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    condensed = re.sub(r">([^<]{40,})<", lambda m: f">{m.group(1)[:40]}…<", condensed)
    condensed = re.sub(r"\s+", " ", condensed)
    return condensed[:max_chars]
