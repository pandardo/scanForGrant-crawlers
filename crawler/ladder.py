"""The selector ladder (§6.4).

    1. sources.list_selector    — the selector that last worked here
    2. extraction_strategies    — selectors proven on OTHER sources,
                                  same fingerprint first, then by times_worked
    3. Stage A                  — the LLM infers a new selector from the HTML
    4. give up                  — error + diagnostics (§6.5)

Every rung is gated. Non-empty is NOT success: a selector matching the nav
returns links too, and accepting it would cache a wrong selector, report `ok`,
and produce plausible garbage — strictly worse than failing. When the gate
rejects, the ladder falls through to the next rung rather than accepting.
Spending a DeepSeek call beats caching a wrong selector.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .types import Candidate
from .gate import extract_candidates, validate_candidates
from .fingerprint import fingerprint_page
from .infer import infer_list_selector
from .llm import CallCapExceeded, LLMClient, LLMError

log = logging.getLogger(__name__)


@dataclass
class LadderResult:
    candidates: list[Candidate] = field(default_factory=list)
    selector_used: str | None = None
    strategy_id: str | None = None
    fingerprint: str | None = None
    # Which strategies to credit/blame once the caller knows the outcome.
    worked_strategy_id: str | None = None
    failed_strategy_ids: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


def _try_selector(html: str, selector: str, url: str) -> tuple[list[Candidate], dict]:
    """One rung: extract, then gate. Returns (accepted, gate diagnostics)."""
    try:
        found = extract_candidates(html, selector, url)
    except Exception as exc:
        # A malformed selector is a rejected rung, not a crashed run — the
        # selector may have come from an LLM reading a hostile page.
        return [], {"selector": selector, "gate_verdict": "invalid_selector",
                    "error": type(exc).__name__}

    accepted, gate = validate_candidates(found, url)
    return accepted, {"selector": selector, **gate}


def climb(
    *,
    html: str,
    url: str,
    source: dict,
    strategies: list[dict],
    llm: LLMClient | None,
) -> LadderResult:
    """Walk the ladder until a rung's candidates pass the gate."""
    result = LadderResult()
    diagnostics: dict = {"ladder_rungs_tried": []}

    page_fingerprint = fingerprint_page(html)
    result.fingerprint = page_fingerprint
    diagnostics["fingerprint"] = page_fingerprint

    # ── Rung 1: the selector that last worked here ──────────────────────────
    cached = source.get("list_selector")
    if cached:
        diagnostics["ladder_rungs_tried"].append("cached_selector")
        accepted, gate = _try_selector(html, cached, url)
        diagnostics["cached_selector"] = gate

        if accepted:
            result.candidates = accepted
            result.selector_used = cached
            result.diagnostics = diagnostics
            return result

        log.info("cached selector %r no longer works for %s", cached, url)

    # ── Rung 2: selectors proven elsewhere, same fingerprint first ──────────
    # Ranked by fingerprint match then track record: a selector that has worked
    # on 20 pages of the same CMS is likelier than one that worked once.
    ranked = sorted(
        strategies,
        key=lambda s: (
            s.get("fingerprint") != page_fingerprint,
            -(s.get("times_worked") or 0),
        ),
    )

    if ranked:
        diagnostics["ladder_rungs_tried"].append("reuse_pool")
        attempts = []

        for strategy in ranked[:10]:  # bounded: each attempt is a parse
            selector = strategy["list_selector"]
            if selector == cached:
                continue  # already tried as rung 1

            accepted, gate = _try_selector(html, selector, url)
            attempts.append(
                {"strategy_id": strategy["id"], "same_fingerprint":
                 strategy.get("fingerprint") == page_fingerprint, **gate}
            )

            if accepted:
                result.candidates = accepted
                result.selector_used = selector
                result.strategy_id = strategy["id"]
                result.worked_strategy_id = strategy["id"]
                diagnostics["reuse_pool"] = attempts
                result.diagnostics = diagnostics
                log.info("reused selector %r from the pool for %s", selector, url)
                return result

            result.failed_strategy_ids.append(strategy["id"])

        diagnostics["reuse_pool"] = attempts

    # ── Rung 3: Stage A infers a new selector ──────────────────────────────
    if llm is not None:
        diagnostics["ladder_rungs_tried"].append("stage_a")
        try:
            inferred, reasoning = infer_list_selector(llm, url=url, html=html)
        except CallCapExceeded as exc:
            diagnostics["stage_a"] = {"skipped": str(exc)}
            result.diagnostics = diagnostics
            return result
        except LLMError as exc:
            diagnostics["stage_a"] = {"error": str(exc)[:200]}
            result.diagnostics = diagnostics
            return result

        if inferred:
            accepted, gate = _try_selector(html, inferred, url)
            diagnostics["stage_a"] = {"reasoning": reasoning, **gate}

            if accepted:
                result.candidates = accepted
                result.selector_used = inferred
                result.diagnostics = diagnostics
                log.info("stage A inferred selector %r for %s", inferred, url)
                return result

            # Inferred but gated out: the model proposed something that matches
            # the wrong elements. Falling through is correct — caching this would
            # be the silent-garbage case.
            log.info("stage A selector %r rejected by the gate for %s", inferred, url)
        else:
            diagnostics["stage_a"] = {"selector": None, "reasoning": reasoning}

    # ── Rung 4: give up honestly ───────────────────────────────────────────
    result.diagnostics = diagnostics
    return result
