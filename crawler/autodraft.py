"""Auto-draft on persistent error (§6.4 Track 2).

When a source has errored `AUTODRAFT_AFTER_ERRORS` scans in a row, the scan run
drafts an extraction rule for it and opens a PR — the same flow as the manual
`--draft-adapter` command, triggered by the error streak instead of a human.

The safety story is unchanged: the draft is inert data validated against
rules.py, the PR is never auto-merged, and nothing reaches the database until a
human merges and applies it. Automating the *proposal* is fine; the review stays.

Fires only when the streak EQUALS the threshold, not at-or-above: an outage that
persists for a week should produce one PR, not one per scan. (github_pr.py reuses
a stable branch per source anyway, so even a re-fire updates the same PR rather
than spawning siblings.)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Source types a drafted rule can actually fix. An rss/api source that errors is
# a feed or endpoint problem, not an extraction problem; a `rule` source already
# has a rule — a broken one needs human eyes, not a second draft on top.
DRAFTABLE_TYPES = ("html", "html_js")


def threshold_from_env() -> int:
    """Errors-in-a-row before drafting. 0 disables auto-drafting."""
    return int(os.environ.get("AUTODRAFT_AFTER_ERRORS", "3"))


def should_autodraft(source: dict, consecutive_errors: int, threshold: int) -> bool:
    if threshold <= 0:
        return False
    if source.get("type") not in DRAFTABLE_TYPES:
        return False
    return consecutive_errors == threshold


def draft_and_open_pr(
    source: dict, *, fetcher, llm, renderer, consecutive_errors: int | None = None
) -> str | None:
    """Fetch the source's list page, draft a rule, open a PR.

    Returns the PR URL, or None when no usable rule came out or PR credentials
    are missing. Never raises: an auto-draft failure must not fail the scan run
    that triggered it.
    """
    from .clean import clean_html
    from .draft import draft_rule
    from .github_pr import GitHubPR, PRError
    from .render import RenderError

    url = source.get("list_url") or source["url"]

    # Fetch the page the way a human debugging it would: render if plain fetch is
    # thin, since the hard sources are usually JS-rendered.
    html = None
    response = fetcher.get(url)
    if response is not None:
        html = response.text

    if not html or clean_html(html).text_length < 500:
        try:
            html = renderer.render(url)
            log.info("used a rendered page (plain fetch was too thin)")
        except RenderError:
            if not html:
                log.warning("auto-draft: could not fetch or render %s", url)
                return None

    try:
        result = draft_rule(llm, url=url, html=html)
    except Exception as exc:  # LLM/network failure — log, never break the run.
        log.warning("auto-draft failed for %s: %s", source["label"], exc)
        return None

    if not result.ok:
        log.info("auto-draft: no usable rule for %s: %s", source["label"], result.reasoning)
        return None

    rule_json = result.rule.model_dump(exclude_none=True, mode="json")
    log.info("auto-drafted rule for %s: %s", source["label"], rule_json)

    token = os.environ.get("GITHUB_PR_TOKEN")
    repo = os.environ.get("GITHUB_CRAWLER_REPO")
    if not token or not repo:
        log.warning(
            "auto-draft: GITHUB_PR_TOKEN/GITHUB_CRAWLER_REPO not set — "
            "drafted a rule for %s but cannot open a PR",
            source["label"],
        )
        return None

    try:
        pr_url = GitHubPR(token=token, repo=repo).open_rule_pr(
            source_id=source["id"],
            source_label=source["label"],
            rule=rule_json,
            note=(
                f"[auto-draft after {consecutive_errors} consecutive errors] "
                if consecutive_errors
                else "[auto-draft] "
            )
            + result.reasoning,
        )
        log.info("auto-draft: opened PR for %s: %s", source["label"], pr_url)
        return pr_url
    except PRError as exc:
        log.warning("auto-draft: could not open PR for %s: %s", source["label"], exc)
        return None
