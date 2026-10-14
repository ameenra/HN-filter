"""Unit tests for Hacker News API client."""

import asyncio

import httpx
import pytest
import respx

from hnfilter.hn import HN_BASE_URL, fetch_item, fetch_stories, fetch_top_story_ids


@pytest.mark.asyncio
@respx.mock
async def test_fetch_top_story_ids():
    """Verify top story IDs are fetched and sliced to the limit."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    )

    async with httpx.AsyncClient() as client:
        ids = await fetch_top_story_ids(client, limit=5)
        assert ids == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stories_success():
    """Verify concurrent story fetching and filtering."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[101, 102])
    )
    respx.get(f"{HN_BASE_URL}/item/101.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 101,
                "type": "story",
                "title": "Rust Compiler Optimization",
                "url": "https://rust-lang.org/blog/1",
                "score": 120,
                "descendants": 35,
                "time": 1700000000,
            },
        )
    )
    respx.get(f"{HN_BASE_URL}/item/102.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 102,
                "type": "story",
                "title": "Ask HN: Recommended Architecture Books?",
                "score": 50,
                "descendants": 12,
                "time": 1700001000,
            },
        )
    )

    async with httpx.AsyncClient() as client:
        stories = await fetch_stories(limit=2, min_score=10, client=client)
        assert len(stories) == 2
        assert stories[0].id == 101
        assert stories[0].domain == "rust-lang.org"
        assert stories[1].id == 102
        assert stories[1].url == "https://news.ycombinator.com/item?id=102"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stories_filters_low_score_and_non_stories():
    """Verify items below min_score or non-story types are excluded."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[201, 202, 203])
    )
    # Story below min_score (score: 5 < 10)
    respx.get(f"{HN_BASE_URL}/item/201.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 201,
                "type": "story",
                "title": "Low Score Story",
                "url": "https://example.com/low",
                "score": 5,
            },
        )
    )
    # Non-story item (job)
    respx.get(f"{HN_BASE_URL}/item/202.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 202,
                "type": "job",
                "title": "Staff Engineer at Acme",
                "url": "https://acme.com/job",
                "score": 25,
            },
        )
    )
    # Valid story meeting threshold
    respx.get(f"{HN_BASE_URL}/item/203.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 203,
                "type": "story",
                "title": "High Performance Postgres",
                "url": "https://example.com/postgres",
                "score": 88,
            },
        )
    )

    async with httpx.AsyncClient() as client:
        stories = await fetch_stories(limit=3, min_score=10, client=client)
        assert len(stories) == 1
        assert stories[0].id == 203
        assert stories[0].score == 88


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stories_handles_null_item():
    """Verify null (deleted) items from HN API are skipped gracefully."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[301])
    )
    respx.get(f"{HN_BASE_URL}/item/301.json").mock(
        return_value=httpx.Response(200, json=None)
    )

    async with httpx.AsyncClient() as client:
        stories = await fetch_stories(limit=1, min_score=10, client=client)
        assert stories == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stories_handles_http_errors():
    """Verify 4xx/5xx errors on individual items do not crash the batch."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[401, 402])
    )
    # 401 returns 500 error
    respx.get(f"{HN_BASE_URL}/item/401.json").mock(
        return_value=httpx.Response(500)
    )
    # 402 succeeds
    respx.get(f"{HN_BASE_URL}/item/402.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 402,
                "type": "story",
                "title": "Building a Modern Search Engine",
                "url": "https://example.com/search",
                "score": 45,
            },
        )
    )

    async with httpx.AsyncClient() as client:
        stories = await fetch_stories(limit=2, min_score=10, client=client)
        assert len(stories) == 1
        assert stories[0].id == 402


@pytest.mark.asyncio
@respx.mock
async def test_fetch_item_network_timeout_retry():
    """Verify fetch_item retries on network errors and recovers."""
    route = respx.get(f"{HN_BASE_URL}/item/501.json")
    route.side_effect = [
        httpx.ConnectTimeout("Connection timed out"),
        httpx.Response(
            200,
            json={
                "id": 501,
                "type": "story",
                "title": "Recovered Story",
                "url": "https://example.com/recovered",
                "score": 30,
            },
        ),
    ]

    semaphore = asyncio.Semaphore(1)
    async with httpx.AsyncClient() as client:
        data = await fetch_item(client, 501, semaphore=semaphore, max_retries=1)
        assert data is not None
        assert data["id"] == 501
        assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stories_without_explicit_client():
    """Verify fetch_stories works and cleans up when client is not passed."""
    respx.get(f"{HN_BASE_URL}/topstories.json").mock(
        return_value=httpx.Response(200, json=[601])
    )
    respx.get(f"{HN_BASE_URL}/item/601.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 601,
                "type": "story",
                "title": "Self Managed Client Story",
                "url": "https://example.com/story",
                "score": 20,
            },
        )
    )

    stories = await fetch_stories(limit=1, min_score=10, client=None)
    assert len(stories) == 1
    assert stories[0].id == 601


@pytest.mark.asyncio
@respx.mock
async def test_fetch_item_network_error_exhausted_retries():
    """Verify fetch_item returns None when retries are exhausted on network error."""
    route = respx.get(f"{HN_BASE_URL}/item/701.json")
    route.side_effect = httpx.ConnectError("Network unreachable")

    semaphore = asyncio.Semaphore(1)
    async with httpx.AsyncClient() as client:
        data = await fetch_item(client, 701, semaphore=semaphore, max_retries=1)
        assert data is None
        assert route.call_count == 2

