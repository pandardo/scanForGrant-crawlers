"""CLI entry point.

    python -m crawler                      # scan every active source
    python -m crawler --source <uuid>      # scan one source (even if paused)
    python -m crawler --source <uuid> --dry-run   # no writes; prints what it found
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import logging_setup
from .config import Config, ConfigError
from .db import Database
from .fetch import Fetcher
from .llm import LLMClient
from .pipeline import scan_source
from .render import Renderer
from .schedule import should_run

log = logging.getLogger("crawler")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler", description="Scan sources for new grants.")
    parser.add_argument("--source", help="scan only this source id")
    parser.add_argument(
        "--trigger", choices=("cron", "manual"), default="manual", help="what started this run"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="extract and print, but write nothing"
    )
    parser.add_argument(
        "--respect-frequency",
        action="store_true",
        help="exit unless app_settings.scan_frequency says this hour should scan (§6.7)",
    )
    parser.add_argument(
        "--draft-adapter",
        action="store_true",
        help="Track 2 (§6.4): draft an extraction rule for --source and open a PR. "
             "Does not scan. Needs GITHUB_PR_TOKEN + GITHUB_CRAWLER_REPO.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    # Configure logging before anything else can log a secret.
    logging_setup.configure(level=logging.DEBUG if args.verbose else logging.INFO)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    db = Database(config)
    fetcher = Fetcher(config)
    llm = LLMClient(config)
    renderer = Renderer(config)
    started_at = datetime.now(timezone.utc)

    if args.draft_adapter:
        try:
            return _draft_adapter(args, db=db, fetcher=fetcher, llm=llm, renderer=renderer)
        finally:
            fetcher.close()
            llm.close()
            renderer.close()

    try:
        # Frequency gating (§6.7). The workflow fires hourly and this decides
        # whether to proceed, so changing the setting in the UI takes effect
        # without editing the cron.
        if args.respect_frequency:
            frequency = db.settings().get("scan_frequency", "daily")
            run_now, reason = should_run(frequency, last_scan_at=db.last_successful_scan_at())
            log.info("%s", reason)
            if not run_now:
                return 0

        sources = db.active_sources(args.source)
        if not sources:
            log.info("no sources to scan")
            return 0

        topics = db.active_topics()
        # The §6.4 reuse pool and the §6.3 dedup pool: read once per run.
        strategies = db.extraction_strategies()
        dedup_pool = db.recent_grants_for_dedup()
        log.info(
            "scanning %d source(s) against %d topic(s)%s",
            len(sources),
            len(topics),
            " [dry-run]" if args.dry_run else "",
        )

        # Share the run budget across sources so one paginated site cannot spend
        # it all. A generous share (3x the even split) still lets a rich source
        # use more than its neighbours, while guaranteeing the tail gets a turn.
        per_source_budget = None
        if len(sources) > 1:
            even_split = config.llm_max_calls_per_run // len(sources)
            per_source_budget = max(10, even_split * 3)
            log.info(
                "per-source LLM budget: %d call(s) of %d total",
                per_source_budget,
                config.llm_max_calls_per_run,
            )

        results = []
        for source in sources:
            log.info("→ %s", source["label"])
            llm.begin_source(per_source_budget)
            result = scan_source(
                source,
                db=db,
                fetcher=fetcher,
                llm=llm,
                topics=topics,
                trigger=args.trigger,
                dry_run=args.dry_run,
                strategies=strategies,
                renderer=renderer,
                dedup_pool=dedup_pool,
            )
            results.append(result)

            log.info(
                "  %s: %s — %d new, %d updated, %d pages, %d LLM calls%s",
                result.label,
                result.status,
                result.new_count,
                result.updated_count,
                result.pages_fetched,
                result.llm_calls,
                f" ({result.error})" if result.error else "",
            )

        total_new = sum(r.new_count for r in results)
        errors = [r for r in results if r.status == "error"]

        # Auto-draft (§6.4 Track 2): a source that has errored N scans in a row
        # is not coming back on its own — draft a rule and open a PR for review.
        # After the loop, so a slow draft can never eat another source's scan.
        if not args.dry_run and errors:
            _autodraft_persistent_errors(
                errors, sources=sources, fetcher=fetcher, llm=llm, renderer=renderer
            )

        # The run-level summary row: source_id NULL (§5).
        if not args.dry_run:
            db.record_scan_run(
                {
                    "source_id": None,
                    "started_at": started_at.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "trigger": args.trigger,
                    "status": "error" if errors else "ok",
                    "new_count": total_new,
                    "pages_fetched": fetcher.pages_fetched,
                    "llm_calls": llm.calls_made,
                    "log": "\n".join(
                        f"{r.label}: {r.status}" + (f" — {r.error}" if r.error else "")
                        for r in results
                    ),
                }
            )

        log.info(
            "done: %d new grant(s), %d source error(s), %d LLM calls",
            total_new,
            len(errors),
            llm.calls_made,
        )
        return 1 if errors else 0

    finally:
        fetcher.close()
        llm.close()
        renderer.close()


def _autodraft_persistent_errors(errors, *, sources, fetcher, llm, renderer) -> None:
    """Auto-draft rules for sources whose error streak just hit the threshold.

    Runs after the scan loop so a slow draft never delays a scan; failures are
    logged and swallowed — a broken draft must not error the run that found it.
    """
    from .autodraft import draft_and_open_pr, should_autodraft, threshold_from_env

    threshold = threshold_from_env()
    by_id = {s["id"]: s for s in sources}
    for result in errors:
        source = by_id.get(result.source_id)
        if not source:
            continue
        if not should_autodraft(source, result.consecutive_errors, threshold):
            continue
        log.info(
            "auto-draft: %s has errored %d scan(s) in a row — drafting a rule (§6.4)",
            result.label,
            result.consecutive_errors,
        )
        draft_and_open_pr(
            source,
            fetcher=fetcher,
            llm=llm,
            renderer=renderer,
            consecutive_errors=result.consecutive_errors,
        )


def _draft_adapter(args, *, db, fetcher, llm, renderer) -> int:
    """Track 2 (§6.4): draft an extraction rule for one source, open a PR.

    The rule is validated inert data. Nothing runs until a human merges the PR
    and applies the rule — this command only proposes.
    """
    import os

    from .clean import clean_html
    from .draft import draft_rule
    from .github_pr import GitHubPR, PRError
    from .render import RenderError

    if not args.source:
        log.error("--draft-adapter needs --source <id>")
        return 2

    sources = db.active_sources(args.source)
    if not sources:
        log.error("no such source: %s", args.source)
        return 2
    source = sources[0]
    url = source.get("list_url") or source["url"]
    log.info("drafting an extraction rule for %s", source["label"])

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
                log.error("could not fetch or render %s", url)
                return 1

    result = draft_rule(llm, url=url, html=html)
    if not result.ok:
        log.info("no usable rule drafted: %s", result.reasoning)
        return 1

    rule_json = result.rule.model_dump(exclude_none=True, mode="json")
    log.info("drafted rule: %s", rule_json)

    token = os.environ.get("GITHUB_PR_TOKEN")
    repo = os.environ.get("GITHUB_CRAWLER_REPO")
    if not token or not repo:
        # No PR credentials: still succeed by printing the rule for manual use.
        log.info("GITHUB_PR_TOKEN/GITHUB_CRAWLER_REPO not set — printing rule instead of opening a PR")
        print(_json_dumps(rule_json))
        return 0

    try:
        pr_url = GitHubPR(token=token, repo=repo).open_rule_pr(
            source_id=source["id"],
            source_label=source["label"],
            rule=rule_json,
            note=result.reasoning,
        )
        log.info("opened PR: %s", pr_url)
        return 0
    except PRError as exc:
        log.error("could not open PR: %s", exc)
        return 1


def _json_dumps(obj) -> str:
    import json as _json

    return _json.dumps(obj, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
