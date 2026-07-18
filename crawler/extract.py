"""Stage B: detail page → structured grant (§6.3).

The LLM sees only public page content — never user data (§11). Its output is
validated against the schema; a failure is retried once with the validation error
in-prompt, then quarantined to scan_runs.log.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from .clean import CleanedPage
from .llm import LLMClient, LLMError
from .schema import CATEGORIES, StageBResponse

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract Italian public grant ("bando") data from web pages.

You return ONLY a JSON object of this exact shape:
{
  "grant": {
    "title": "string, the official grant name",
    "issuer": "string or null, the body publishing it (the 'ente')",
    "category": "one of: %s",
    "description": "string or null, 1-3 sentences on what it funds",
    "requirements": ["string", ...],
    "funding_text": "string or null, the amount AS DISPLAYED e.g. '€10.000 - €50.000'",
    "funding_min": number or null,
    "funding_max": number or null,
    "deadline": "YYYY-MM-DD or null"
  },
  "relevance": [{"topic_id": "string", "score": 0.0}]
}

Rules:
- The page is in Italian. Keep title, description, requirements and funding_text
  in Italian, exactly as the source words them. Only `category` is English.
- Never invent a value. If the page does not state something, use null.
- Many bandi are "a sportello" (rolling) and have no deadline: use null.
- funding_min/max are numbers in euro, parsed from the text; null if unclear.
- Score each supplied topic 0.0-1.0 for how well this grant matches it.
- If the page is NOT a grant (a news article, an index, an error page), return
  {"grant": null} and nothing else.""" % ", ".join(CATEGORIES)


class NotAGrant(Exception):
    """The page was fetched but is not a grant detail page."""


def _build_user_prompt(url: str, cleaned: CleanedPage, topics: list[dict]) -> str:
    topic_lines = (
        "\n".join(
            f"- id={t['id']} name={t['name']}: {t.get('description') or 'no description'}"
            for t in topics
        )
        or "(no topics defined — return an empty relevance array)"
    )

    return (
        f"URL: {url}\n\n"
        f"TOPICS TO SCORE:\n{topic_lines}\n\n"
        f"PAGE CONTENT:\n{cleaned.text}"
    )


def extract_grant(
    llm: LLMClient,
    *,
    url: str,
    cleaned: CleanedPage,
    topics: list[dict],
) -> StageBResponse:
    """Extract one grant. Retries once on schema failure, per §6.3.

    Raises NotAGrant when the page is not a grant, LLMError when the provider
    fails or the response is still invalid after the retry.
    """
    user_prompt = _build_user_prompt(url, cleaned, topics)
    response = llm.complete_json(system=SYSTEM_PROMPT, user=user_prompt)

    if response.get("grant") is None:
        raise NotAGrant(f"model reports {url} is not a grant page")

    try:
        return StageBResponse.model_validate(response)
    except ValidationError as first_error:
        log.info("stage B validation failed for %s, retrying once", url)

        # The retry carries the validation error, so the model can correct itself
        # rather than resample blindly.
        retry_prompt = (
            f"{user_prompt}\n\n"
            f"Your previous response failed schema validation with:\n"
            f"{first_error}\n\n"
            f"Return corrected JSON matching the schema exactly."
        )

        try:
            retried = llm.complete_json(system=SYSTEM_PROMPT, user=retry_prompt)
        except LLMError:
            raise

        if retried.get("grant") is None:
            raise NotAGrant(f"model reports {url} is not a grant page")

        try:
            return StageBResponse.model_validate(retried)
        except ValidationError as second_error:
            # Quarantine: the caller records this to scan_runs.log (§6.3).
            raise LLMError(
                f"stage B failed validation twice for {url}: "
                f"{json.dumps(second_error.errors()[:3], default=str)}"
            ) from None
