"""Supabase access with the service-role key.

This key bypasses RLS — it is the trusted writer the policy matrix in §5 refers
to. Nothing here should ever echo it: errors are scrubbed before logging.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from supabase import Client, create_client

from .config import Config
from .logging_setup import scrub

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, config: Config, client: Client | None = None) -> None:
        self._client = client or create_client(
            config.supabase_url, config.supabase_service_key
        )

    # — reads —

    def active_sources(self, source_id: str | None = None) -> list[dict[str, Any]]:
        """Active sources, or one specific source regardless of active state.

        A single-source run is an explicit request, so it overrides the pause.
        """
        query = self._client.table("sources").select("*")
        query = query.eq("id", source_id) if source_id else query.eq("active", True)
        return query.execute().data or []

    def active_topics(self) -> list[dict[str, Any]]:
        return (
            self._client.table("topics")
            .select("id, name, description, keywords")
            .eq("active", True)
            .execute()
            .data
            or []
        )

    def last_successful_scan_at(self) -> "datetime | None":
        """When the last ok run-summary finished — drives frequency gating (§6.7).

        The run-level summary row has source_id NULL; a per-source row would
        double-count. Gating on this makes the schedule survive cron drift.
        """
        rows = (
            self._client.table("scan_runs")
            .select("finished_at")
            .is_("source_id", "null")
            .eq("status", "ok")
            .order("finished_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows or not rows[0].get("finished_at"):
            return None
        return datetime.fromisoformat(rows[0]["finished_at"].replace("Z", "+00:00"))

    def settings(self) -> dict[str, Any]:
        return (
            self._client.table("app_settings").select("*").eq("id", 1).single().execute().data
            or {}
        )

    def known_url_hashes(self, source_id: str) -> set[str]:
        rows = (
            self._client.table("grants")
            .select("url_hash")
            .eq("source_id", source_id)
            .execute()
            .data
            or []
        )
        return {row["url_hash"] for row in rows}

    def extraction_strategies(self, limit: int = 50) -> list[dict[str, Any]]:
        """The reuse pool (§6.4 rung 2), best track record first."""
        return (
            self._client.table("extraction_strategies")
            .select("id, list_selector, fingerprint, times_worked, times_failed")
            .order("times_worked", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def recent_grants_for_dedup(self, limit: int = 500) -> list[dict[str, Any]]:
        """Grants to compare against for cross-portal soft dedup (§6.3)."""
        return (
            self._client.table("grants")
            .select("id, title, deadline, source_id")
            .order("found_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def page_snapshot(self, source_id: str, url_hash: str) -> str | None:
        rows = (
            self._client.table("page_snapshots")
            .select("content_hash")
            .eq("source_id", source_id)
            .eq("url_hash", url_hash)
            .execute()
            .data
            or []
        )
        return rows[0]["content_hash"] if rows else None

    # — writes —

    def save_snapshot(self, source_id: str, url_hash: str, content_hash: str) -> None:
        self._client.table("page_snapshots").upsert(
            {
                "source_id": source_id,
                "url_hash": url_hash,
                "content_hash": content_hash,
                "last_seen_at": _now(),
            },
            on_conflict="source_id,url_hash",
        ).execute()

    def upsert_grant(self, grant: dict[str, Any]) -> bool:
        """Insert or update a grant by url_hash. Returns True if newly inserted.

        found_at is set only on insert: it means "first seen", and the dashboard's
        "New today" depends on it not moving when a page is re-extracted.
        """
        existing = (
            self._client.table("grants")
            .select("id")
            .eq("url_hash", grant["url_hash"])
            .execute()
            .data
            or []
        )
        is_new = not existing

        if is_new:
            grant["found_at"] = _now()

        self._client.table("grants").upsert(grant, on_conflict="url_hash").execute()
        return is_new

    def update_source_status(
        self, source_id: str, *, status: str, error: str | None = None
    ) -> int:
        """Record the scan outcome on the source row.

        Maintains `consecutive_errors` (0024): +1 on error, reset on ok. Returns
        the new count so the caller can decide whether a persistent break has
        crossed the auto-draft threshold (§6.4 Track 2).
        """
        errors = 0
        if status == "error":
            rows = (
                self._client.table("sources")
                .select("consecutive_errors")
                .eq("id", source_id)
                .execute()
                .data
                or []
            )
            errors = (rows[0].get("consecutive_errors") or 0) + 1 if rows else 1

        self._client.table("sources").update(
            {
                "last_scan_at": _now(),
                "last_status": status,
                # Errors can quote fetched content; scrub before storing.
                "last_error": scrub(error)[:1000] if error else None,
                "consecutive_errors": errors,
            }
        ).eq("id", source_id).execute()
        return errors

    def record_scan_run(self, run: dict[str, Any]) -> None:
        if run.get("log"):
            run["log"] = scrub(str(run["log"]))[:5000]
        self._client.table("scan_runs").insert(run).execute()

    def cache_selector(self, source_id: str, selector: str) -> None:
        self._client.table("sources").update({"list_selector": selector}).eq(
            "id", source_id
        ).execute()

    def remember_strategy(self, *, selector: str, fingerprint: str | None, origin: str = "llm") -> None:
        """Add a selector to the reuse pool, or credit it if already there.

        Only ever called for a selector the validation gate ACCEPTED, so
        times_worked counts gated successes, not bare matches (§6.4).
        """
        existing = (
            self._client.table("extraction_strategies")
            .select("id, times_worked")
            .eq("list_selector", selector)
            .eq("fingerprint", fingerprint)
            .execute()
            .data
            or []
        )

        if existing:
            self._client.table("extraction_strategies").update(
                {"times_worked": (existing[0]["times_worked"] or 0) + 1, "last_used_at": _now()}
            ).eq("id", existing[0]["id"]).execute()
            return

        self._client.table("extraction_strategies").insert(
            {
                "list_selector": selector,
                "fingerprint": fingerprint,
                "origin": origin,
                "times_worked": 1,
                "last_used_at": _now(),
            }
        ).execute()

    def credit_strategy(self, strategy_id: str) -> None:
        rows = (
            self._client.table("extraction_strategies")
            .select("times_worked").eq("id", strategy_id).execute().data or []
        )
        if rows:
            self._client.table("extraction_strategies").update(
                {"times_worked": (rows[0]["times_worked"] or 0) + 1, "last_used_at": _now()}
            ).eq("id", strategy_id).execute()

    def blame_strategy(self, strategy_id: str) -> None:
        rows = (
            self._client.table("extraction_strategies")
            .select("times_failed").eq("id", strategy_id).execute().data or []
        )
        if rows:
            self._client.table("extraction_strategies").update(
                {"times_failed": (rows[0]["times_failed"] or 0) + 1}
            ).eq("id", strategy_id).execute()
