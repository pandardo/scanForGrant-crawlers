"""The scan pipeline (§6.1).

    load active sources
    └─ per source:
       1. FETCH list page      — adapter by source.type
       2. DISCOVER candidates  — the §6.4 ladder
       3. DIFF                 — content_hash vs page_snapshots; unchanged → skip
       4. EXTRACT              — Stage B, for new or changed pages only
       5. UPSERT by url_hash
       6. record scan_run

The LLM is the last resort, not the first tool: change detection and the selector
cache exist so DeepSeek only ever sees new or changed content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .adapters import build_adapter
from .clean import CleanedPage, clean_html
from .dedup import find_duplicate
from .db import Database
from .extract import NotAGrant, extract_grant
from .fetch import Fetcher, content_hash, url_hash
from .llm import CallCapExceeded, LLMClient, LLMError

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    source_id: str
    label: str
    status: str  # ok | error | skipped
    new_count: int = 0
    updated_count: int = 0
    pages_fetched: int = 0
    llm_calls: int = 0
    error: str | None = None
    diagnostics: dict = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    # Scans in a row this source has errored (0024) — 0 when ok or dry-run.
    # __main__ compares it to the auto-draft threshold (§6.4 Track 2).
    consecutive_errors: int = 0


def scan_source(
    source: dict,
    *,
    db: Database,
    fetcher: Fetcher,
    llm: LLMClient,
    topics: list[dict],
    trigger: str = "manual",
    dry_run: bool = False,
    strategies: list[dict] | None = None,
    renderer=None,
    dedup_pool: list[dict] | None = None,
) -> SourceResult:
    """Scan one source end to end."""
    result = SourceResult(source_id=source["id"], label=source["label"], status="ok")
    started_at = datetime.now(timezone.utc)
    pages_before = fetcher.pages_fetched
    calls_before = llm.calls_made

    # 1-2. Fetch and discover, walking the §6.4 ladder for html/html_js.
    adapter = build_adapter(
        source, fetcher=fetcher, strategies=strategies, llm=llm, renderer=renderer
    )
    discovery = adapter.discover(source)
    result.diagnostics = discovery.diagnostics

    if discovery.error:
        result.status = "error"
        result.error = discovery.error
        result.pages_fetched = fetcher.pages_fetched - pages_before
        if not dry_run:
            result.consecutive_errors = db.update_source_status(
                source["id"], status="error", error=discovery.error
            )
        return result

    if not discovery.candidates:
        # Not an error: a list page can legitimately have nothing new.
        result.status = "ok"
        result.log_lines.append("no candidates discovered")
        result.pages_fetched = fetcher.pages_fetched - pages_before
        if not dry_run:
            db.update_source_status(source["id"], status="ok")
        return result

    # Persist what the ladder learned (§6.4). Only gated successes are credited,
    # so times_worked means "the gate accepted this", not "it matched something".
    if not dry_run:
        if discovery.selector_used and discovery.selector_used != source.get("list_selector"):
            db.cache_selector(source["id"], discovery.selector_used)

        if discovery.worked_strategy_id:
            db.credit_strategy(discovery.worked_strategy_id)
        elif discovery.selector_used:
            db.remember_strategy(
                selector=discovery.selector_used, fingerprint=discovery.fingerprint
            )

        for failed_id in discovery.failed_strategy_ids:
            db.blame_strategy(failed_id)

    log.info("%s: %d candidates discovered", source["label"], len(discovery.candidates))

    for candidate in discovery.candidates:
        candidate_hash = url_hash(candidate.url)

        try:
            processed = _process_candidate(
                candidate,
                candidate_hash=candidate_hash,
                source=source,
                db=db,
                fetcher=fetcher,
                llm=llm,
                topics=topics,
                dry_run=dry_run,
                result=result,
                dedup_pool=dedup_pool,
            )
        except CallCapExceeded as exc:
            # Budget spent: stop cleanly rather than erroring the whole source.
            result.log_lines.append(str(exc))
            log.warning("%s: %s", source["label"], exc)
            break
        except LLMError as exc:
            # Quarantine the failure and carry on with the next candidate (§6.3).
            result.log_lines.append(f"extraction failed for {candidate.url}: {exc}")
            log.warning("extraction failed for %s: %s", candidate.url, exc)
            continue

        if processed == "new":
            result.new_count += 1
        elif processed == "updated":
            result.updated_count += 1

    result.pages_fetched = fetcher.pages_fetched - pages_before
    result.llm_calls = llm.calls_made - calls_before

    if not dry_run:
        db.update_source_status(source["id"], status="ok")
        db.record_scan_run(
            {
                "source_id": source["id"],
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "trigger": trigger,
                "status": result.status,
                "new_count": result.new_count,
                "pages_fetched": result.pages_fetched,
                "llm_calls": result.llm_calls,
                "log": "\n".join(result.log_lines) or None,
                "diagnostics": result.diagnostics or None,
            }
        )

    return result


def _payload_as_page(candidate) -> CleanedPage:
    """Render an API row as text for Stage B.

    Flattened to readable "key: value" lines rather than raw JSON: the extraction
    prompt is written for prose, and the model handles labelled fields far more
    reliably than nested objects.
    """
    import json as _json

    payload = candidate.payload or {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    lines: list[str] = []
    if candidate.title:
        lines.append(f"Title: {candidate.title}")

    # Only fields that carry grant meaning; skip the search engine's bookkeeping.
    interesting = (
        "callTitle", "description", "destinationDetails", "budget",
        "deadlineDate", "closingDate", "deadlineModel", "duration",
        "programmeDivision", "typesOfAction", "callccm2Id",
    )
    for key in interesting:
        value = metadata.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value in (None, "", []):
            continue
        lines.append(f"{key}: {value}")

    summary = payload.get("summary")
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append(f"URL: {candidate.url}")

    text = "\n".join(str(line) for line in lines)
    raw = _json.dumps(payload, default=str)
    return CleanedPage(
        text=text,
        original_bytes=len(raw.encode("utf-8", errors="ignore")),
        text_length=len(text),
        truncated=False,
    )


def _process_candidate(
    candidate,
    *,
    candidate_hash: str,
    source: dict,
    db: Database,
    fetcher: Fetcher,
    llm: LLMClient,
    topics: list[dict],
    dry_run: bool,
    result: SourceResult,
    dedup_pool: list[dict] | None = None,
) -> str | None:
    """Returns 'new', 'updated', or None when skipped."""
    if candidate.payload is not None:
        # JSON-API source: the discovery response already carries the grant's
        # fields, so skip the detail fetch. The EU portal's detail pages sit
        # behind the same cookie wall as its listing and yield zero text — a
        # fetch there costs a request and returns nothing extractable.
        cleaned = _payload_as_page(candidate)
    else:
        response = fetcher.get(candidate.url)
        if response is None:
            result.log_lines.append(f"could not fetch {candidate.url}")
            return None
        cleaned = clean_html(response.text)

    page_hash = content_hash(cleaned.text)

    # 3. DIFF. An unchanged page costs no LLM call — this is the check the whole
    # cost model rests on (§6.1).
    #
    # Keyed on the snapshot alone, NOT on "is this already a grant". Non-grant
    # pages (exam notices, news) never enter `grants`, so a `known`-gated check
    # would re-extract every one of them on every run, forever. On a real CCIAA
    # feed that was 5 wasted DeepSeek calls per run, permanently.
    if not dry_run:
        previous = db.page_snapshot(source["id"], candidate_hash)
        if previous == page_hash:
            return None

    if not cleaned.text.strip():
        # Empty after cleaning is the §6.5 "cleaner ate it" / JS-rendered case.
        result.log_lines.append(f"no text after cleaning: {candidate.url}")
        return None

    # 4. EXTRACT.
    try:
        extraction = extract_grant(llm, url=candidate.url, cleaned=cleaned, topics=topics)
    except NotAGrant:
        result.log_lines.append(f"not a grant page: {candidate.url}")
        # Remember it anyway, so we do not re-extract this page every run.
        if not dry_run:
            db.save_snapshot(source["id"], candidate_hash, page_hash)
        return None

    grant = extraction.grant
    matched = [r.topic_id for r in extraction.relevance if r.score >= 0.5]
    best_score = max((r.score for r in extraction.relevance), default=None)

    row = {
        "source_id": source["id"],
        "url": candidate.url,
        "url_hash": candidate_hash,
        "title": grant.title,
        "issuer": grant.issuer,
        "category": grant.category,
        "description": grant.description,
        "requirements": grant.requirements,
        "funding_text": grant.funding_text,
        "funding_min": grant.funding_min,
        "funding_max": grant.funding_max,
        "deadline": grant.deadline.isoformat() if grant.deadline else None,
        "relevance_score": best_score,
        "matched_topics": matched or None,
        "content_hash": page_hash,
        "raw": extraction.model_dump(mode="json"),
    }

    # Soft dedup (§6.3): the same bando on a second portal. Linked, not dropped —
    # each page may carry detail the other lacks, and picking the canonical one
    # is a human call.
    if dedup_pool:
        duplicate_id, score = find_duplicate(
            {"title": grant.title, "deadline": grant.deadline, "source_id": source["id"]},
            dedup_pool,
        )
        if duplicate_id:
            row["duplicate_of"] = duplicate_id
            row["duplicate_score"] = score
            result.log_lines.append(f"probable duplicate of {duplicate_id}: {grant.title}")

    if dry_run:
        result.log_lines.append(f"[dry-run] would upsert: {grant.title}")
        return "new"

    # 5. UPSERT.
    is_new = db.upsert_grant(row)
    db.save_snapshot(source["id"], candidate_hash, page_hash)
    return "new" if is_new else "updated"
