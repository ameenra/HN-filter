"""Unit tests for HN Filter data models."""

import pytest
from pydantic import ValidationError

from hnfilter.models import DigestItem, Story, Verdict, VerdictBatch


def test_story_from_standard_hn_item():
    """Verify parsing a standard external link story."""
    raw_item = {
        "id": 12345,
        "type": "story",
        "title": "Show HN: Fast SQLite In WebAssembly",
        "url": "https://www.example.com/posts/wasm-sqlite",
        "score": 150,
        "descendants": 42,
        "time": 1700000000,
    }
    story = Story.from_hn_item(raw_item)
    assert story is not None
    assert story.id == 12345
    assert story.title == "Show HN: Fast SQLite In WebAssembly"
    assert story.url == "https://www.example.com/posts/wasm-sqlite"
    assert story.domain == "example.com"  # www. stripped
    assert story.score == 150
    assert story.comments == 42
    assert story.time == 1700000000
    assert story.hn_url == "https://news.ycombinator.com/item?id=12345"


def test_story_validation_failure():
    """Verify Story raises ValidationError when given incompatible field types."""
    with pytest.raises(ValidationError):
        Story(id="invalid_id", title="Test", url="https://example.com", domain="example.com", score=10)


def test_story_without_url_fallback():
    """Verify Ask HN or text stories default url to the HN item link."""
    raw_item = {
        "id": 67890,
        "type": "story",
        "title": "Ask HN: Favorite compilers book?",
        "score": 85,
        "descendants": 30,
        "time": 1700001000,
    }
    story = Story.from_hn_item(raw_item)
    assert story is not None
    assert story.id == 67890
    assert story.url == "https://news.ycombinator.com/item?id=67890"
    assert story.domain == "news.ycombinator.com"
    assert story.hn_url == "https://news.ycombinator.com/item?id=67890"


@pytest.mark.parametrize(
    "invalid_item",
    [
        None,
        {},
        {"id": 1, "type": "job", "title": "Software Engineer at Stripe"},
        {"id": 2, "type": "comment", "text": "Interesting perspective."},
        {"id": 3, "type": "story", "title": "Dead story", "dead": True},
        {"id": 4, "type": "story", "title": "Deleted story", "deleted": True},
        {"id": 5, "type": "story", "title": ""},  # empty title
        {"type": "story", "title": "Missing ID"},
    ],
)
def test_story_from_invalid_item(invalid_item):
    """Verify non-story, dead, deleted, or incomplete items return None."""
    assert Story.from_hn_item(invalid_item) is None


def test_verdict_validation():
    """Verify valid and invalid scores in Verdict."""
    v_valid = Verdict(id=123, score=8, reason="Strong match for compilers")
    assert v_valid.score == 8
    assert v_valid.reason == "Strong match for compilers"

    # Score below 0 clamped to 0
    v_low = Verdict(id=123, score=-5, reason="Negative score")
    assert v_low.score == 0

    # Score above 10 clamped to 10
    v_high = Verdict(id=123, score=15, reason="Score exceeds 10")
    assert v_high.score == 10

    # Long reason truncated safely without error
    long_reason = "A" * 250
    v_long = Verdict(id=123, score=5, reason=long_reason)
    assert len(v_long.reason) == 200
    assert v_long.reason.endswith("...")


def test_verdict_batch_deserialization():
    """Verify deserializing list of verdicts into VerdictBatch."""
    data = {
        "verdicts": [
            {"id": 101, "score": 9, "reason": "Systems engineering"},
            {"id": 102, "score": 2, "reason": "Hype crypto"},
        ]
    }
    batch = VerdictBatch.model_validate(data)
    assert len(batch.verdicts) == 2
    assert batch.verdicts[0].id == 101
    assert batch.verdicts[0].score == 9
    assert batch.verdicts[1].id == 102
    assert batch.verdicts[1].score == 2


def test_digest_item():
    """Verify DigestItem correctly links Story and Verdict."""
    story = Story(
        id=555,
        title="Linux Kernel Deep Dive",
        url="https://kernel.org/dive",
        domain="kernel.org",
        score=200,
        comments=50,
        time=1700000000,
    )
    verdict = Verdict(id=555, score=9, reason="Deep systems dive")
    digest_item = DigestItem(story=story, verdict=verdict)

    assert digest_item.score == 9
    assert digest_item.reason == "Deep systems dive"
    assert digest_item.story.title == "Linux Kernel Deep Dive"
