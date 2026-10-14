"""Hacker News API client for fetching top stories."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from hnfilter.models import Story

logger = logging.getLogger(__name__)

HN_BASE_URL = "https://hacker-news.firebaseio.com/v0"


async def fetch_top_story_ids(
    client: httpx.AsyncClient,
    limit: int = 150,
    base_url: str = HN_BASE_URL,
) -> list[int]:
    """Fetch top story IDs from Hacker News API."""
    url = f"{base_url}/topstories.json"
    response = await client.get(url)
    response.raise_for_status()
    ids: list[int] = response.json()
    return ids[:limit]


async def fetch_item(
    client: httpx.AsyncClient,
    item_id: int,
    semaphore: asyncio.Semaphore,
    base_url: str = HN_BASE_URL,
    max_retries: int = 2,
) -> dict[str, Any] | None:
    """Fetch a single HN item by ID with concurrency throttle and retry logic."""
    url = f"{base_url}/item/{item_id}.json"
    async with semaphore:
        for attempt in range(max_retries + 1):
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except (ValueError, TypeError):
                        return None
                    return data if isinstance(data, dict) else None
                if response.status_code >= 500 and attempt < max_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                logger.warning("Failed to fetch item %d: HTTP %d", item_id, response.status_code)
                return None
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                logger.warning("Network error fetching item %d: %s", item_id, exc)
                return None
    return None


async def fetch_stories(
    limit: int = 150,
    min_score: int = 10,
    client: httpx.AsyncClient | None = None,
    concurrency: int = 10,
    base_url: str = HN_BASE_URL,
) -> list[Story]:
    """Concurrently fetch and filter top stories from Hacker News.

    Keeps items where type == 'story', not dead/deleted, and score >= min_score.
    """
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        close_client = True

    try:
        top_ids = await fetch_top_story_ids(client, limit=limit, base_url=base_url)
        semaphore = asyncio.Semaphore(concurrency)

        tasks = [
            fetch_item(client, item_id, semaphore=semaphore, base_url=base_url)
            for item_id in top_ids
        ]
        raw_items = await asyncio.gather(*tasks)

        stories: list[Story] = []
        for raw in raw_items:
            story = Story.from_hn_item(raw)
            if story and story.score >= min_score:
                stories.append(story)

        logger.info(
            "Fetched %d stories from %d top IDs (min_score=%d)",
            len(stories),
            len(top_ids),
            min_score,
        )
        return stories
    finally:
        if close_client:
            await client.aclose()
