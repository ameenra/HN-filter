"""CLI entrypoint and pipeline orchestrator for HN Filter."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from hnfilter.config import Settings
from hnfilter.hn import fetch_stories
from hnfilter.models import DigestItem, Verdict
from hnfilter.notify import (
    chunk_messages,
    format_digest_header,
    format_story_block,
    send_digest,
    send_discord_message,
)
from hnfilter.scorer import score_stories
from hnfilter.store import SeenStore

logger = logging.getLogger("hnfilter")


def setup_logging(verbose: bool = False) -> None:
    """Configure structured logging for the application."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_pipeline(
    settings: Settings,
    dry_run: bool = False,
    limit: int | None = None,
    threshold: int | None = None,
) -> int:
    """Run the end-to-end HN Filter pipeline.

    Returns exit code: 0 on success, 1 on error.
    """
    effective_limit = limit if limit is not None else settings.fetch_limit
    effective_threshold = threshold if threshold is not None else settings.threshold

    logger.info(
        "Starting HN Filter pipeline (dry_run=%s, limit=%d, threshold=%d)",
        dry_run,
        effective_limit,
        effective_threshold,
    )

    # 1. Validate credentials
    if not settings.gemini_api_key:
        logger.error("GEMINI_API_KEY is not configured in settings or environment")
        return 1

    if not dry_run and not settings.discord_webhook_url:
        logger.error("DISCORD_WEBHOOK_URL is required for live runs")
        return 1

    try:
        # 2. Load seen store
        store = SeenStore.load(settings.seen_file)

        # 3. Fetch top stories from HN
        stories = await fetch_stories(
            limit=effective_limit,
            min_score=settings.min_hn_score,
        )

        # 4. Filter against seen store
        new_stories = store.filter_new(stories)

        if not new_stories:
            logger.info("No new stories to process")
            if not dry_run:
                msg = f"**HN Digest** · 0 new stories to evaluate today (checked {len(stories)} stories)."
                send_discord_message(settings.discord_webhook_url, msg)
            else:
                print("\n[Dry Run] No new stories to evaluate; seen store already tracks all items.")
            return 0

        # 5. Score new stories using Gemini
        verdicts = score_stories(
            stories=new_stories,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            batch_size=settings.batch_size,
            interests_path=settings.interests_file,
        )

        # 6. Pair stories with verdicts and filter picks
        verdict_map = {v.id: v for v in verdicts}
        digest_items: list[DigestItem] = []
        for story in new_stories:
            verdict = verdict_map.get(story.id) or Verdict(
                id=story.id, score=0, reason="Verdict not found"
            )
            digest_items.append(DigestItem(story=story, verdict=verdict))

        # Filter: score >= threshold, sort descending by score then points
        picks = [item for item in digest_items if item.score >= effective_threshold]
        picks.sort(key=lambda x: (x.score, x.story.score), reverse=True)
        top_picks = picks[: settings.max_items]

        # 7. Delivery or Dry-Run Output
        if dry_run:
            print("\n" + "=" * 60)
            print("                HN FILTER DRY RUN REPORT                ")
            print("=" * 60)
            print(f"Total stories fetched from HN: {len(stories)}")
            print(f"New stories evaluated:         {len(new_stories)}")
            print(f"Stories meeting threshold (>={effective_threshold}):  {len(picks)}")
            print(f"Top picks capped for digest:   {len(top_picks)}")
            print("-" * 60)

            if top_picks:
                header = format_digest_header(len(top_picks), len(new_stories))
                blocks = [format_story_block(item) for item in top_picks]
                chunks = chunk_messages(blocks, header=header)
                print("\n--- Formatted Discord Message Preview ---")
                for chunk in chunks:
                    print(chunk)
                print("-----------------------------------------")
            else:
                print("No stories met the scoring threshold today.")

            print("\n--- Summary of All Scored Stories ---")
            for item in sorted(digest_items, key=lambda x: x.score, reverse=True):
                flag = "✓ PICK" if item.score >= effective_threshold else "  SKIP"
                print(f"[{item.score:2d}/10] {flag} | {item.story.title[:65]} ({item.story.domain})")
                print(f"         Reason: {item.reason}")

            print("=" * 60)
            print("Dry run completed successfully. (seen.json unmodified)")
            return 0

        # Live run: Post to Discord
        send_digest(
            webhook_url=settings.discord_webhook_url,
            picks=top_picks,
            total_new=len(new_stories),
        )

        # 8. CRITICAL: Atomic state update ONLY on successful delivery
        store.mark_seen(new_stories)
        store.prune(max_age_days=14)
        store.save()
        logger.info("Pipeline completed successfully; state updated")
        return 0

    except Exception:
        logger.exception("Pipeline failed with unexpected error")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint parsing flags and executing pipeline."""
    parser = argparse.ArgumentParser(
        prog="hnfilter",
        description="Curate Hacker News stories with Gemini and post digests to Discord.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and score stories, print report to console without posting or modifying state.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Override the number of top stories to inspect.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Override minimum LLM score required to include a story in the digest.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging.",
    )

    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)

    settings = Settings()
    return asyncio.run(
        run_pipeline(
            settings=settings,
            dry_run=args.dry_run,
            limit=args.limit,
            threshold=args.threshold,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
