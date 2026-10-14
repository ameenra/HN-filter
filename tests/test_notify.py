"""Unit tests for Discord notifier and message chunker."""

import json

import httpx
import pytest
import respx

from hnfilter.models import DigestItem, Story, Verdict
from hnfilter.notify import (
    DiscordDeliveryError,
    chunk_messages,
    format_digest_header,
    format_story_block,
    send_digest,
    send_discord_message,
)

TEST_WEBHOOK_URL = "https://discord.com/api/webhooks/123/xyz"


def _make_digest_item(story_id: int, title: str = "Test Story", score: int = 8, url: str | None = None) -> DigestItem:
    story = Story(
        id=story_id,
        title=title,
        url=url or f"https://example.com/story-{story_id}",
        domain="example.com",
        score=150,
        comments=40,
        time=1700000000,
    )
    verdict = Verdict(id=story_id, score=score, reason="High engineering relevance")
    return DigestItem(story=story, verdict=verdict)


def test_format_story_block_standard():
    """Verify standard story formatting with suppressed link previews."""
    item = _make_digest_item(101, title="Fast SQLite WASM", score=9)
    formatted = format_story_block(item)

    assert "**[9] Fast SQLite WASM** (150 pts · 40 cmts)" in formatted
    assert "*High engineering relevance*" in formatted
    assert "<https://example.com/story-101> · <https://news.ycombinator.com/item?id=101>" in formatted


def test_format_story_block_ask_hn():
    """Verify Ask HN or text items with identical URLs only display one link."""
    hn_url = "https://news.ycombinator.com/item?id=202"
    item = _make_digest_item(202, title="Ask HN: Favorite tools?", score=7, url=hn_url)
    formatted = format_story_block(item)

    assert "<https://news.ycombinator.com/item?id=202>" in formatted
    # Should only have one dot separator (for points and comments), not between two URLs
    assert formatted.count("·") == 1


def test_format_digest_header():
    """Verify header format with custom or default date."""
    header = format_digest_header(5, 120, date_str="Wed 14 Oct")
    assert header == "**HN Digest, Wed 14 Oct** · 5 picks from 120 new stories\n"


def test_chunk_messages_under_limit():
    """Verify multiple blocks that fit under max_chars stay in a single chunk."""
    blocks = [
        "**[8] Story One**\nReason 1\n<https://example.com/1>",
        "**[7] Story Two**\nReason 2\n<https://example.com/2>",
    ]
    chunks = chunk_messages(blocks, header="**Header**", max_chars=1800)
    assert len(chunks) == 1
    assert "**Header**" in chunks[0]
    assert "Story One" in chunks[0]
    assert "Story Two" in chunks[0]


def test_chunk_messages_splits_cleanly():
    """Verify blocks exceeding max_chars are split across chunks without splitting any story."""
    # Create blocks of ~150 chars each
    blocks = [
        f"**[{i}] Long Title For Story Number {i}**\nReason explaining why this is relevant\n<https://example.com/{i}>"
        for i in range(20)
    ]
    # Set max_chars small enough to force multiple chunks
    chunks = chunk_messages(blocks, header="**Header**", max_chars=500)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500

    # Ensure every single story is present intact
    combined = "\n\n".join(chunks)
    for i in range(20):
        assert f"Story Number {i}" in combined


def test_chunk_messages_oversized_single_block():
    """Verify a single block exceeding max_chars is safely truncated."""
    giant_block = "A" * 2000
    chunks = chunk_messages([giant_block], header="", max_chars=500)
    assert len(chunks) == 1
    assert len(chunks[0]) <= 500
    assert chunks[0].endswith("...")


def test_chunk_messages_empty():
    """Verify empty blocks list returns header or empty list."""
    assert chunk_messages([], header="") == []
    assert chunk_messages([], header="**Header**") == ["**Header**"]


@respx.mock
def test_send_discord_message_success():
    """Verify successful POST to Discord webhook."""
    route = respx.post(TEST_WEBHOOK_URL).mock(return_value=httpx.Response(204))

    send_discord_message(TEST_WEBHOOK_URL, "Test message content")
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.read()) == {"content": "Test message content"}


@respx.mock
def test_send_discord_message_rate_limit():
    """Verify 429 rate limit retries after retry_after delay."""
    route = respx.post(TEST_WEBHOOK_URL)
    route.side_effect = [
        httpx.Response(429, json={"retry_after": 0.01}),
        httpx.Response(204),
    ]

    send_discord_message(TEST_WEBHOOK_URL, "Retry message", max_retries=1)
    assert route.call_count == 2


@respx.mock
def test_send_discord_message_rate_limit_via_header():
    """Verify 429 rate limit uses Retry-After header if JSON is unavailable."""
    route = respx.post(TEST_WEBHOOK_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.01"}, text="Too Many Requests"),
        httpx.Response(204),
    ]

    send_discord_message(TEST_WEBHOOK_URL, "Retry header message", max_retries=1)
    assert route.call_count == 2


@respx.mock
def test_send_discord_message_non_2xx_raises():
    """Verify HTTP errors raise DiscordDeliveryError."""
    respx.post(TEST_WEBHOOK_URL).mock(return_value=httpx.Response(500, text="Internal Error"))

    with pytest.raises(DiscordDeliveryError, match="HTTP 500"):
        send_discord_message(TEST_WEBHOOK_URL, "Fail message", max_retries=0)


def test_send_discord_message_missing_url():
    """Verify missing webhook URL raises DiscordDeliveryError immediately."""
    with pytest.raises(DiscordDeliveryError, match="DISCORD_WEBHOOK_URL is required"):
        send_discord_message("", "Content")


@respx.mock
def test_send_digest_zero_picks():
    """Verify sending digest with zero picks delivers status notification."""
    route = respx.post(TEST_WEBHOOK_URL).mock(return_value=httpx.Response(204))

    messages = send_digest(TEST_WEBHOOK_URL, picks=[], total_new=140)
    assert len(messages) == 1
    assert "0 picks from 140 new stories" in messages[0]
    assert route.call_count == 1


@respx.mock
def test_send_digest_with_picks():
    """Verify send_digest sends formatted picks with zero pause in test."""
    route = respx.post(TEST_WEBHOOK_URL).mock(return_value=httpx.Response(204))

    items = [_make_digest_item(i, f"Story {i}", score=8) for i in range(3)]
    messages = send_digest(
        TEST_WEBHOOK_URL,
        picks=items,
        total_new=50,
        pause_between_chunks=0.0,
    )

    assert len(messages) == 1
    assert "3 picks from 50 new stories" in messages[0]
    assert "Story 0" in messages[0]
    assert route.call_count == 1
