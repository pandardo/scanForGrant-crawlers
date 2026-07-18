"""Runtime configuration, read from the environment.

Nothing here has a default that is a secret, and nothing is committed: the source
list lives in the database and the keys live in GitHub Actions secrets (§2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. See .env.example.")
    return value


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_service_key: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_max_calls_per_run: int

    # Politeness: per-domain rate limit and per-run page budget (§6.1).
    request_delay_seconds: float = 2.0
    request_timeout_seconds: float = 30.0
    user_agent: str = (
        "ScanForGrantsBot/0.1 (+https://github.com/; monitors public grant listings)"
    )

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            supabase_url=_require("SUPABASE_URL"),
            supabase_service_key=_require("SUPABASE_SERVICE_ROLE_KEY"),
            llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").strip(),
            llm_model=os.environ.get("LLM_MODEL", "deepseek-chat").strip(),
            # DEEPSEEK_API_KEY is the documented name; LLM_API_KEY works too, so
            # swapping providers does not mean renaming a secret.
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip()
            or _require("DEEPSEEK_API_KEY"),
            llm_max_calls_per_run=int(os.environ.get("LLM_MAX_CALLS_PER_RUN", "200")),
        )
