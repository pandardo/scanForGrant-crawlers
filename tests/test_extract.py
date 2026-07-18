"""Stage B: schema validation, the single retry, and the call cap (§6.3).

The LLM is stubbed. These test our contract with the provider, not the provider.
"""

from __future__ import annotations

import json

import httpx
import pytest

from crawler.clean import CleanedPage
from crawler.config import Config
from crawler.extract import NotAGrant, extract_grant
from crawler.llm import CallCapExceeded, LLMClient, LLMError

PAGE = CleanedPage(text="Bando Voucher Digitale 4.0 …", original_bytes=100, text_length=28, truncated=False)
TOPICS = [{"id": "t1", "name": "Digitale", "description": "Digital transformation"}]

VALID_GRANT = {
    "grant": {
        "title": "Voucher Digitale 4.0",
        "issuer": "MIMIT",
        "category": "Digital & Innovation",
        "description": "Contributo a fondo perduto.",
        "requirements": ["PMI italiana"],
        "funding_text": "€10.000 - €50.000",
        "funding_min": 10000,
        "funding_max": 50000,
        "deadline": "2026-09-30",
    },
    "relevance": [{"topic_id": "t1", "score": 0.9}],
}


def make_config(max_calls: int = 200) -> Config:
    return Config(
        supabase_url="https://fake.supabase.co",
        supabase_service_key="fake-service-key-value",
        llm_base_url="https://api.fake-llm.test/v1",
        llm_model="fake-model",
        llm_api_key="sk-fake000000000000000000",
        llm_max_calls_per_run=max_calls,
    )


def llm_returning(*payloads: object, config: Config | None = None) -> LLMClient:
    """An LLMClient whose transport replays the given payloads in order."""
    responses = iter(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = next(responses)
        if isinstance(payload, int):  # an HTTP status to simulate
            return httpx.Response(payload, text='{"error":{"message":"upstream boom"}}')
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    transport = httpx.MockTransport(handler)
    return LLMClient(config or make_config(), client=httpx.Client(transport=transport))


def test_extracts_a_valid_grant():
    llm = llm_returning(VALID_GRANT)
    result = extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)
    assert result.grant.title == "Voucher Digitale 4.0"
    assert result.grant.funding_min == 10000.0
    assert llm.calls_made == 1


def test_non_grant_page_raises_rather_than_inventing_a_row():
    llm = llm_returning({"grant": None})
    with pytest.raises(NotAGrant):
        extract_grant(llm, url="https://e.it/news", cleaned=PAGE, topics=TOPICS)


def test_invalid_response_is_retried_once_and_can_succeed():
    """§6.3: retry once with the validation error in-prompt."""
    invalid = {"grant": {"issuer": "MIMIT"}}  # no title
    llm = llm_returning(invalid, VALID_GRANT)

    result = extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)

    assert result.grant.title == "Voucher Digitale 4.0"
    assert llm.calls_made == 2, "should have retried exactly once"


def test_retry_prompt_carries_the_validation_error():
    """The retry must tell the model what was wrong, not just resample."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["messages"][1]["content"])
        payload = VALID_GRANT if len(sent) > 1 else {"grant": {"issuer": "X"}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    llm = LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)

    assert len(sent) == 2
    assert "failed schema validation" in sent[1]
    assert "title" in sent[1], "the retry should name the offending field"


def test_two_invalid_responses_are_quarantined_not_stored():
    invalid = {"grant": {"issuer": "MIMIT"}}
    llm = llm_returning(invalid, invalid)

    with pytest.raises(LLMError, match="failed validation twice"):
        extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)

    assert llm.calls_made == 2, "must not retry more than once"


def test_non_json_response_raises():
    llm = llm_returning("I'm sorry, I cannot help with that.")
    with pytest.raises(LLMError, match="valid JSON"):
        extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)


def test_http_error_is_surfaced_with_the_key_scrubbed():
    """An error body quoting our key must not reach a log un-redacted."""
    llm = llm_returning(500)
    with pytest.raises(LLMError) as exc:
        extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)
    assert "sk-fake000000000000000000" not in str(exc.value)


def test_call_cap_stops_the_run():
    """§6.3 cost guardrail."""
    llm = llm_returning(VALID_GRANT, VALID_GRANT, config=make_config(max_calls=1))

    extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)
    assert llm.calls_remaining == 0

    with pytest.raises(CallCapExceeded):
        extract_grant(llm, url="https://e.it/b/2", cleaned=PAGE, topics=TOPICS)


def test_topics_are_passed_to_the_model():
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID_GRANT)}}]})

    llm = LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)

    assert "id=t1" in sent[0]
    assert "Digital transformation" in sent[0]


def test_json_mode_is_requested():
    """JSON mode is what keeps the response parseable (§6.3)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(VALID_GRANT)}}]})

    llm = LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    extract_grant(llm, url="https://e.it/b/1", cleaned=PAGE, topics=TOPICS)

    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["model"] == "fake-model"


class TestJsonModeGuard:
    """DeepSeek rejects response_format=json_object with a 400 unless the prompt
    mentions "json". Found when Stage A's prompt (which never said the word)
    failed against the live API while Stage B worked — Stage B's prompt happens
    to contain "JSON". The client enforces it so a new prompt cannot regress."""

    def _sent(self, system: str, user: str) -> dict:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        llm = LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
        llm.complete_json(system=system, user=user)
        return captured[0]

    def test_word_is_injected_when_a_prompt_omits_it(self):
        sent = self._sent("Find the selector.", "Here is some HTML.")
        prompt_text = " ".join(m["content"] for m in sent["messages"]).lower()
        assert "json" in prompt_text

    def test_an_existing_mention_is_not_duplicated(self):
        sent = self._sent("Return ONLY a JSON object.", "html here")
        assert sent["messages"][0]["content"] == "Return ONLY a JSON object."

    def test_a_mention_in_the_user_message_counts(self):
        sent = self._sent("Find the selector.", "Return json please.")
        assert sent["messages"][0]["content"] == "Find the selector."


def test_stage_a_prompt_satisfies_json_mode():
    """The specific prompt that broke: guard it directly."""
    from crawler.infer import SYSTEM_PROMPT

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"list_selector":null}'}}]})

    llm = LLMClient(make_config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm.complete_json(system=SYSTEM_PROMPT, user="URL: x\n\nHTML STRUCTURE: <div/>")

    prompt_text = " ".join(m["content"] for m in captured[0]["messages"]).lower()
    assert "json" in prompt_text, "stage A would 400 against DeepSeek"


class TestPerSourceBudget:
    """One paginated source must not spend the whole run's budget.

    Seen live: a comune consumed 149 of 200 calls across 188 pages, and the
    eleven councils after it were skipped with 0 calls — then reported as
    'no candidates found', which reads as 'this site has no grants' rather than
    'we never tried'.
    """

    def test_source_budget_caps_a_greedy_source(self):
        llm = llm_returning(*[VALID_GRANT] * 10, config=make_config(max_calls=100))
        llm.begin_source(3)

        for _ in range(3):
            extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])

        with pytest.raises(CallCapExceeded, match="per-source"):
            extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])

    def test_the_next_source_gets_a_fresh_allowance(self):
        """The whole point: exhausting one source must not starve the next."""
        llm = llm_returning(*[VALID_GRANT] * 10, config=make_config(max_calls=100))

        llm.begin_source(2)
        extract_grant(llm, url="https://e.it/a", cleaned=PAGE, topics=[])
        extract_grant(llm, url="https://e.it/a", cleaned=PAGE, topics=[])
        assert llm.calls_remaining == 0, "first source is spent"

        llm.begin_source(2)
        assert llm.calls_remaining == 2, "second source starts fresh"
        extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])
        assert llm.source_calls_made == 1

    def test_the_run_cap_still_wins_over_a_generous_source_budget(self):
        llm = llm_returning(*[VALID_GRANT] * 10, config=make_config(max_calls=2))
        llm.begin_source(50)  # more than the run allows

        extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])
        extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])

        with pytest.raises(CallCapExceeded, match="per-run"):
            extract_grant(llm, url="https://e.it/b", cleaned=PAGE, topics=[])

    def test_no_source_budget_means_only_the_run_cap_applies(self):
        """Single-source runs (--source <id>) should use the full budget."""
        llm = llm_returning(*[VALID_GRANT] * 10, config=make_config(max_calls=5))
        llm.begin_source(None)
        assert llm.calls_remaining == 5
