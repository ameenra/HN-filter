"""Unit tests for Gemini scoring engine."""

from unittest.mock import MagicMock

import pytest
from google.genai import errors

from hnfilter.models import Story, Verdict, VerdictBatch
from hnfilter.scorer import (
    DEFAULT_SYSTEM_INSTRUCTION,
    chunk_stories,
    format_batch_prompt,
    load_interests,
    score_batch,
    score_stories,
)


def _make_story(story_id: int, title: str = "Test Story") -> Story:
    return Story(
        id=story_id,
        title=title,
        url=f"https://example.com/{story_id}",
        domain="example.com",
        score=100,
        comments=10,
        time=1700000000,
    )


def test_chunk_stories():
    """Verify chunk_stories splits items into exact batch sizes."""
    stories = [_make_story(i) for i in range(95)]
    chunks = chunk_stories(stories, batch_size=40)
    assert len(chunks) == 3
    assert len(chunks[0]) == 40
    assert len(chunks[1]) == 40
    assert len(chunks[2]) == 15

    # Edge cases
    assert chunk_stories([], batch_size=10) == []
    with pytest.raises(ValueError):
        chunk_stories(stories, batch_size=0)


def test_load_interests(tmp_path):
    """Verify loading interest profile from file with fallback to default."""
    # Non-existent file
    default_text = load_interests(tmp_path / "non_existent.md")
    assert default_text == DEFAULT_SYSTEM_INSTRUCTION

    # Valid interests file
    interests_file = tmp_path / "my_interests.md"
    interests_file.write_text("I love Rust, Python, and Linux internals.", encoding="utf-8")
    loaded = load_interests(interests_file)
    assert "USER INTEREST PROFILE:" in loaded
    assert "I love Rust, Python, and Linux internals." in loaded

    # None path
    assert load_interests(None) == DEFAULT_SYSTEM_INSTRUCTION


def test_format_batch_prompt():
    """Verify JSON structure in prompt."""
    stories = [_make_story(101, "Compilers in Rust"), _make_story(102, "Fast DB")]
    prompt = format_batch_prompt(stories)
    assert '"id": 101' in prompt
    assert '"title": "Compilers in Rust"' in prompt
    assert '"id": 102' in prompt


def test_score_batch_success_via_parsed():
    """Verify score_batch correctly returns verdicts when client provides response.parsed."""
    stories = [_make_story(101), _make_story(102)]
    mock_client = MagicMock()

    batch_verdicts = VerdictBatch(
        verdicts=[
            Verdict(id=101, score=9, reason="Compilers match"),
            Verdict(id=102, score=3, reason="Unrelated web framework"),
        ]
    )

    mock_response = MagicMock()
    mock_response.parsed = batch_verdicts
    mock_client.models.generate_content.return_value = mock_response

    verdicts = score_batch(
        client=mock_client,
        model="gemini-2.5-flash",
        stories=stories,
        system_instruction="Instructions",
    )

    assert len(verdicts) == 2
    assert verdicts[0].id == 101
    assert verdicts[0].score == 9
    assert verdicts[1].id == 102
    assert verdicts[1].score == 3
    assert mock_client.models.generate_content.call_count == 1


def test_score_batch_success_via_text_json():
    """Verify score_batch falls back to response.text JSON parsing."""
    stories = [_make_story(201)]
    mock_client = MagicMock()

    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = '{"verdicts": [{"id": 201, "score": 8, "reason": "Systems paper"}]}'
    mock_client.models.generate_content.return_value = mock_response

    verdicts = score_batch(
        client=mock_client,
        model="gemini-2.5-flash",
        stories=stories,
        system_instruction="Instructions",
    )

    assert len(verdicts) == 1
    assert verdicts[0].id == 201
    assert verdicts[0].score == 8


def test_score_batch_handles_omitted_id():
    """Verify stories omitted by the LLM get defaulted to score 0."""
    stories = [_make_story(301), _make_story(302)]
    mock_client = MagicMock()

    mock_response = MagicMock()
    # Model only returned verdict for 301, forgot 302
    mock_response.parsed = VerdictBatch(
        verdicts=[Verdict(id=301, score=7, reason="Good match")]
    )
    mock_client.models.generate_content.return_value = mock_response

    verdicts = score_batch(
        client=mock_client,
        model="gemini-2.5-flash",
        stories=stories,
        system_instruction="Instructions",
    )

    assert len(verdicts) == 2
    assert verdicts[0].id == 301
    assert verdicts[0].score == 7
    assert verdicts[1].id == 302
    assert verdicts[1].score == 0
    assert "omitted" in verdicts[1].reason.lower()


def test_score_batch_rate_limit_retry():
    """Verify 429 rate limit error retries and succeeds."""
    stories = [_make_story(401)]
    mock_client = MagicMock()

    mock_response = MagicMock()
    mock_response.parsed = VerdictBatch(
        verdicts=[Verdict(id=401, score=8, reason="High value")]
    )

    # First call raises 429, second call succeeds
    mock_client.models.generate_content.side_effect = [
        errors.APIError(429, "Rate limit exceeded"),
        mock_response,
    ]

    verdicts = score_batch(
        client=mock_client,
        model="gemini-2.5-flash",
        stories=stories,
        system_instruction="Instructions",
        max_retries=1,
        base_backoff=0.01,
    )

    assert len(verdicts) == 1
    assert verdicts[0].score == 8
    assert mock_client.models.generate_content.call_count == 2


def test_score_batch_exhausted_retries_raises():
    """Verify exhausted retries on 429 raises exception."""
    stories = [_make_story(501)]
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = errors.APIError(429, "Quota exhausted")

    with pytest.raises(errors.APIError):
        score_batch(
            client=mock_client,
            model="gemini-2.5-flash",
            stories=stories,
            system_instruction="Instructions",
            max_retries=2,
            base_backoff=0.01,
        )

    assert mock_client.models.generate_content.call_count == 3


def test_score_stories_orchestration():
    """Verify score_stories orchestrates batches and returns combined verdicts."""
    stories = [_make_story(1), _make_story(2), _make_story(3)]
    mock_client = MagicMock()

    def mock_generate(*args, **kwargs):
        mock_resp = MagicMock()
        contents = kwargs.get("contents", "")
        # Find which stories are in contents
        verdicts = []
        for i in [1, 2, 3]:
            if f'"id": {i}' in contents:
                verdicts.append(Verdict(id=i, score=i * 2, reason=f"Reason {i}"))
        mock_resp.parsed = VerdictBatch(verdicts=verdicts)
        return mock_resp

    mock_client.models.generate_content.side_effect = mock_generate

    verdicts = score_stories(
        stories=stories,
        api_key="test-key",
        batch_size=2,  # Will make 2 batches: [1, 2] and [3]
        client=mock_client,
        base_backoff=0.01,
    )

    assert len(verdicts) == 3
    assert [v.id for v in verdicts] == [1, 2, 3]
    assert [v.score for v in verdicts] == [2, 4, 6]
    assert mock_client.models.generate_content.call_count == 2


def test_score_stories_empty():
    """Verify empty story list returns empty verdicts without calling client."""
    assert score_stories([], api_key="any") == []
    assert score_batch(MagicMock(), "gemini-2.5-flash", [], "sys") == []


def test_score_stories_requires_api_key():
    """Verify ValueError is raised if api_key is empty and client is None."""
    stories = [_make_story(1)]
    with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
        score_stories(stories, api_key="", client=None)


def test_load_interests_read_error(tmp_path, monkeypatch):
    """Verify load_interests gracefully falls back on OSError."""
    f = tmp_path / "broken.md"
    f.touch()
    monkeypatch.setattr("pathlib.Path.read_text", MagicMock(side_effect=OSError("Read error")))
    result = load_interests(f)
    assert result == DEFAULT_SYSTEM_INSTRUCTION


def test_score_batch_empty_response_raises():
    """Verify ValueError is raised when Gemini returns no parsed object and empty text."""
    stories = [_make_story(99)]
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.parsed = None
    mock_resp.text = ""
    mock_client.models.generate_content.return_value = mock_resp

    with pytest.raises(ValueError, match="Empty or unparseable"):
        score_batch(
            client=mock_client,
            model="gemini-2.5-flash",
            stories=stories,
            system_instruction="Instructions",
            max_retries=0,
        )

