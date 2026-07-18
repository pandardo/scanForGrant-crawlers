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

    try:
        # Frequency gating (§6.7). The workflow fires hourly and this decides
        # whether to proceed, so changing the setting in the UI takes effect
        # without editing the cron.
        if args.respect_frequency:
            frequency = db.settings().get("scan_frequency", "daily")
            run_now, reason = should_run(frequency)
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

        results = []
        for source in sources:
            log.info("→ %s", source["label"])
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


if __name__ == "__main__":
    sys.exit(main())
