"""Discord webhook notification and message formatting for HN Filter."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from hnfilter.models import DigestItem

logger = logging.getLogger(__name__)


class DiscordDeliveryError(Exception):
    """Raised when posting to a Discord webhook fails."""


def format_story_block(item: DigestItem) -> str:
    """Format an individual curated story into markdown for Discord.

    Example:
    **[9] SQLite in WebAssembly** (342 pts · 88 cmts)
    *High-performance local DB persistence in web apps.*
    <https://example.com/article> · <https://news.ycombinator.com/item?id=45012345>
    """
    story = item.story
    score = item.score
    reason = item.reason.strip() if item.reason else "High relevance match."

    # When the URL is identical to the HN URL (e.g. Ask HN), show single link
    if story.url == story.hn_url:
        link_line = f"<{story.hn_url}>"
    else:
        link_line = f"<{story.url}> · <{story.hn_url}>"

    return (
        f"**[{score}] {story.title}** ({story.score} pts · {story.comments} cmts)\n"
        f"*{reason}*\n"
        f"{link_line}"
    )


def format_digest_header(picks_count: int, total_new: int, date_str: str | None = None) -> str:
    """Format the digest header line."""
    if date_str is None:
        date_str = datetime.now(UTC).strftime("%a %d %b")
    return f"**HN Digest, {date_str}** · {picks_count} picks from {total_new} new stories\n"


def chunk_messages(
    blocks: Sequence[str],
    header: str = "",
    max_chars: int = 1800,
) -> list[str]:
    """Chunk story blocks into messages under max_chars without splitting any block."""
    if not blocks:
        return [header.strip()] if header.strip() else []

    chunks: list[str] = []
    current_chunk = header.strip()

    for block in blocks:
        clean_block = block.strip()
        if not clean_block:
            continue

        candidate = f"{current_chunk}\n\n{clean_block}" if current_chunk else clean_block

        if len(candidate) <= max_chars:
            current_chunk = candidate
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(clean_block) > max_chars:
                clean_block = clean_block[: max_chars - 3] + "..."
            current_chunk = clean_block

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def send_discord_message(
    webhook_url: str,
    content: str,
    client: httpx.Client | None = None,
    max_retries: int = 3,
) -> None:
    """Send a single markdown message to a Discord webhook with rate limit backoff."""
    if not webhook_url:
        raise DiscordDeliveryError("DISCORD_WEBHOOK_URL is required for notification")

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        payload = {"content": content}

        for attempt in range(max_retries + 1):
            response = client.post(webhook_url, json=payload)

            if response.status_code in (200, 204):
                logger.debug("Successfully posted message chunk to Discord webhook")
                return

            if response.status_code == 429 and attempt < max_retries:
                # Handle Discord rate limit
                retry_after = 1.0
                try:
                    data = response.json()
                    retry_after = float(data.get("retry_after", 1.0))
                except (ValueError, TypeError, KeyError):
                    header_val = response.headers.get("Retry-After")
                    if header_val:
                        try:
                            retry_after = float(header_val)
                        except ValueError:
                            pass

                retry_after = max(0.01, retry_after)

                logger.warning(
                    "Discord webhook rate limited (429). Retrying after %.2fs (attempt %d/%d)...",
                    retry_after,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(retry_after)
                continue

            error_msg = f"Discord webhook failed with HTTP {response.status_code}: {response.text}"
            logger.error(error_msg)
            raise DiscordDeliveryError(error_msg)

    finally:
        if close_client:
            client.close()


def send_digest(
    webhook_url: str,
    picks: Sequence[DigestItem],
    total_new: int,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    pause_between_chunks: float = 1.0,
) -> list[str]:
    """Format and send the curated daily digest to Discord.

    Returns the list of message chunks sent.
    """
    if not picks:
        message = (
            f"**HN Digest** · 0 picks from {total_new} new stories today. "
            "Nothing met your relevance threshold."
        )
        send_discord_message(webhook_url, message, client=client, max_retries=max_retries)
        return [message]

    header = format_digest_header(len(picks), total_new)
    blocks = [format_story_block(item) for item in picks]
    chunks = chunk_messages(blocks, header=header)

    logger.info("Delivering digest with %d picks in %d message chunk(s)", len(picks), len(chunks))

    for i, chunk in enumerate(chunks):
        if i > 0 and pause_between_chunks > 0:
            time.sleep(pause_between_chunks)
        send_discord_message(webhook_url, chunk, client=client, max_retries=max_retries)

    return chunks
