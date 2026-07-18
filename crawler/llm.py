"""Thin LLM provider abstraction.

OpenAI-compatible chat completions with JSON mode. DeepSeek is the default, but
base URL and model come from config, so swapping providers is an env change
rather than a code change (ARCHITECTURE.md §3).

Deliberately thin: no SDK, just the HTTP shape every OpenAI-compatible endpoint
implements. That is one fewer dependency and one fewer thing to break when a
provider's client library moves.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import Config
from .logging_setup import scrub

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any failure talking to the provider."""


class CallCapExceeded(LLMError):
    """The per-run call budget is spent (§6.3 cost guardrail)."""


class LLMClient:
    """Counts its own calls so a runaway loop cannot spend the budget."""

    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self.calls_made = 0

    @property
    def calls_remaining(self) -> int:
        return max(0, self._config.llm_max_calls_per_run - self.calls_made)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """One JSON-mode completion. Returns the parsed object.

        Raises CallCapExceeded when the run's budget is spent, LLMError on
        transport, HTTP, or JSON-parse failure.
        """
        if self.calls_remaining == 0:
            raise CallCapExceeded(
                f"per-run LLM call cap reached ({self._config.llm_max_calls_per_run})"
            )

        self.calls_made += 1

        # DeepSeek (and OpenAI) reject response_format=json_object unless the
        # prompt itself mentions "json" — a 400, not a soft failure. Enforcing it
        # here rather than trusting every prompt to remember means a new prompt
        # cannot reintroduce the bug.
        if "json" not in f"{system}\n{user}".lower():
            system = f"{system}\n\nRespond with a single valid JSON object."

        payload = {
            "model": self._config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = self._client.post(
                f"{self._config.llm_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._config.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {scrub(str(exc))}") from None

        if response.status_code != 200:
            # Error bodies routinely quote the key back; scrub before it is seen.
            raise LLMError(
                f"LLM returned {response.status_code}: {scrub(response.text[:500])}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unexpected LLM response shape: {scrub(str(exc))}") from None

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            # JSON mode still occasionally returns prose; the caller retries once.
            raise LLMError(f"LLM did not return valid JSON: {exc}") from None

        if not isinstance(parsed, dict):
            raise LLMError(f"LLM returned {type(parsed).__name__}, expected an object")

        return parsed

    def close(self) -> None:
        self._client.close()
