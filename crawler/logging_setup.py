"""Log scrubbing.

Every log line in this repo goes through here. The repo is public, so its GitHub
Actions logs are a public URL: a secret that reaches a log line is a published
secret.

GitHub masks registered secrets in Actions output, but that only catches exact
matches of values you registered. It will not catch a key embedded in a JSON
error body, a URL-encoded copy, or a token echoed back inside a provider's error
message — which is exactly how these leak. So we scrub at the source as well.

Two layers:

* Value-based — anything we read from the environment that is secret-shaped is
  redacted wherever it appears, including inside a larger string.
* Pattern-based — recipient emails and known key formats are redacted even when
  we never had the value in our environment (e.g. an address read from the DB, or
  a key quoted back to us by an API).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable

REDACTED = "[REDACTED]"

# Env vars whose values must never appear in output.
SECRET_ENV_VARS = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_API_KEY",
    "RESEND_API_KEY",
    "GITHUB_TOKEN",
)

# Shapes worth redacting even when the value never passed through our env.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Email addresses: notification recipients are personal data (§2).
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), REDACTED),
    # JWTs — Supabase anon/service keys are JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"), REDACTED),
    # Supabase publishable/secret keys.
    (re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]+"), REDACTED),
    # OpenAI-compatible provider keys (DeepSeek included).
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    # Resend.
    (re.compile(r"\bre_[A-Za-z0-9_-]{16,}"), REDACTED),
    # GitHub tokens, fine-grained and classic.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # Auth headers, whatever the token shape. Consumes the rest of the line, not
    # just one token: `Authorization: Bearer <token>` would otherwise redact only
    # the word "Bearer" and leave the token itself in the log.
    (
        re.compile(r"(?i)\b(authorization|apikey|api[-_]?key)\s*[:=]\s*\S.*?(?=[,;}\]\"'\n]|$)"),
        r"\1: " + REDACTED,
    ),
)

# Populated at configure() time from the environment.
_secret_values: list[str] = []


def _collect_secret_values(env: dict[str, str] | None = None) -> list[str]:
    """Secret-shaped values from the environment, longest first.

    Longest-first matters: if one secret is a substring of another, redacting the
    shorter one first would leave a fragment of the longer one visible.
    """
    source = env if env is not None else os.environ
    values = {source.get(name, "").strip() for name in SECRET_ENV_VARS}
    # A short value would match far too much text; 8 is well below any real key.
    return sorted((v for v in values if len(v) >= 8), key=len, reverse=True)


def scrub(text: str, extra_values: Iterable[str] = ()) -> str:
    """Redact secrets from a string. Safe to call on anything log-bound."""
    if not text:
        return text

    for value in list(_secret_values) + [v for v in extra_values if v and len(v) >= 8]:
        text = text.replace(value, REDACTED)

    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)

    return text


class ScrubbingFilter(logging.Filter):
    """Scrubs the message and args of every record passing through a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_arg(a) for a in record.args)

        # Tracebacks quote request bodies and headers; scrub those too.
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            scrubbed = scrub(str(exc))
            if scrubbed != str(exc):
                record.exc_info = (record.exc_info[0], type(exc)(scrubbed), record.exc_info[2])

        return True


def _scrub_arg(value: object) -> object:
    return scrub(value) if isinstance(value, str) else value


def configure(level: int = logging.INFO, env: dict[str, str] | None = None) -> None:
    """Install the scrubbing filter on the root logger. Call once, at startup."""
    global _secret_values
    _secret_values = _collect_secret_values(env)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(ScrubbingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx logs full request URLs, which carry keys in query strings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
