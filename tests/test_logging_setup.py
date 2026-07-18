"""The scrubber is the thing standing between a secret and a public Actions log.
If it silently stops working, nothing else fails — so it is tested first.
"""

from __future__ import annotations

import logging

import pytest

from crawler import logging_setup
from crawler.logging_setup import REDACTED, configure, scrub


@pytest.fixture(autouse=True)
def _env():
    """Configure with a known fake environment, and restore afterwards."""
    configure(
        env={
            "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fakepayload.fakesig",
            "DEEPSEEK_API_KEY": "sk-NOT-A-REAL-KEY-test-fixture-only",
            "RESEND_API_KEY": "re_abcdef0123456789abcdef",
        }
    )
    yield
    logging_setup._secret_values = []


def test_redacts_service_role_key_verbatim():
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fakepayload.fakesig"
    assert key not in scrub(f"connecting with {key}")


def test_redacts_key_embedded_in_a_json_error_body():
    """The realistic leak: a provider echoes the key back inside an error."""
    body = '{"error":{"message":"Invalid key sk-NOT-A-REAL-KEY-test-fixture-only"}}'
    out = scrub(body)
    assert "sk-NOT-A-REAL-KEY-test-fixture-only" not in out
    assert REDACTED in out


def test_redacts_email_addresses():
    """Recipients come from the DB, so their values are never in our env."""
    out = scrub("sending digest to team@azienda.it and boss@azienda.it")
    assert "team@azienda.it" not in out
    assert "boss@azienda.it" not in out


def test_redacts_unknown_keys_by_shape():
    """A key we never had in our environment is still redacted."""
    assert "sk-9999999999999999zzzzzzzzzzzzzzzz" not in scrub(
        "upstream said: sk-9999999999999999zzzzzzzzzzzzzzzz"
    )
    assert "ghp_0123456789abcdef0123456789abcdef" not in scrub(
        "token ghp_0123456789abcdef0123456789abcdef"
    )


def test_redacts_authorization_headers():
    out = scrub("Authorization: Bearer some-opaque-token-value")
    assert "some-opaque-token-value" not in out


def test_scrubs_through_the_logging_pipeline(caplog):
    """The filter must fire on real logger calls, not just direct scrub()."""
    logger = logging.getLogger("crawler.test")
    handler = logging.StreamHandler()
    handler.addFilter(logging_setup.ScrubbingFilter())

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    capture = Capture()
    capture.addFilter(logging_setup.ScrubbingFilter())
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)

    logger.info("calling with key sk-NOT-A-REAL-KEY-test-fixture-only")
    logger.info("notifying %s", "team@azienda.it")

    logger.removeHandler(capture)

    assert "sk-NOT-A-REAL-KEY-test-fixture-only" not in records[0]
    assert "team@azienda.it" not in records[1]


def test_scrubs_exception_messages():
    """Tracebacks quote request bodies; those carry keys."""
    logger = logging.getLogger("crawler.test.exc")
    messages: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.exc_info[1].args[0] if record.exc_info else "")

    capture = Capture()
    capture.addFilter(logging_setup.ScrubbingFilter())
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)

    try:
        raise ValueError("request failed with apikey=sk-NOT-A-REAL-KEY-test-fixture-only")
    except ValueError:
        logger.exception("upstream error")

    logger.removeHandler(capture)
    assert "sk-NOT-A-REAL-KEY-test-fixture-only" not in messages[0]


def test_longest_secret_first():
    """A secret that is a prefix of another must not leave a fragment behind."""
    configure(env={"DEEPSEEK_API_KEY": "sk-aaaabbbbccccdddd", "LLM_API_KEY": "sk-aaaabbbbccccddddeeeeffff"})
    out = scrub("keys: sk-aaaabbbbccccddddeeeeffff")
    assert "eeeeffff" not in out


def test_short_env_values_are_not_treated_as_secrets():
    """A short value would match everywhere and redact real content."""
    configure(env={"DEEPSEEK_API_KEY": "abc"})
    assert scrub("abc is a normal word here") == "abc is a normal word here"


def test_scrub_handles_empty_and_none_like():
    assert scrub("") == ""
