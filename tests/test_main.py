"""Unit and integration tests for pipeline orchestrator and CLI runner."""

from unittest.mock import AsyncMock, patch

import pytest

from hnfilter.config import Settings
from hnfilter.main import main, run_pipeline
from hnfilter.models import Story, Verdict
from hnfilter.store import SeenStore


def _make_story(story_id: int, title: str = "Test Story", score: int = 100) -> Story:
    return Story(
        id=story_id,
        title=title,
        url=f"https://example.com/{story_id}",
        domain="example.com",
        score=score,
        comments=15,
        time=1700000000,
    )


@pytest.fixture
def mock_pipeline_dependencies():
    """Mock external network boundaries (HN API, Gemini Scorer, Discord Webhook)."""
    stories = [
        _make_story(101, "Low relevance hype story", score=50),
        _make_story(102, "High relevance compilers deep dive", score=200),
    ]
    verdicts = [
        Verdict(id=101, score=2, reason="Generic tech hype"),
        Verdict(id=102, score=9, reason="Compilers match interest profile"),
    ]

    with (
        patch("hnfilter.main.fetch_stories", new_callable=AsyncMock) as mock_fetch,
        patch("hnfilter.main.score_stories") as mock_score,
        patch("hnfilter.main.send_digest") as mock_send_digest,
        patch("hnfilter.main.send_discord_message") as mock_send_msg,
    ):
        mock_fetch.return_value = stories
        mock_score.return_value = verdicts
        mock_send_digest.return_value = ["Message chunk"]
        yield {
            "fetch": mock_fetch,
            "score": mock_score,
            "send_digest": mock_send_digest,
            "send_msg": mock_send_msg,
            "stories": stories,
            "verdicts": verdicts,
        }


@pytest.mark.asyncio
async def test_pipeline_dry_run_does_not_mutate_store(tmp_path, capsys, mock_pipeline_dependencies):
    """Verify --dry-run prints report to console, bypasses Discord, and does not save seen.json."""
    seen_file = tmp_path / "seen.json"
    settings = Settings(
        gemini_api_key="test-api-key",
        discord_webhook_url="https://discord.com/webhook",
        seen_file=seen_file,
        threshold=7,
        _env_file=None,
    )

    exit_code = await run_pipeline(settings, dry_run=True)

    assert exit_code == 0
    assert not seen_file.exists()  # Store was NOT mutated
    mock_pipeline_dependencies["send_digest"].assert_not_called()
    mock_pipeline_dependencies["send_msg"].assert_not_called()

    captured = capsys.readouterr().out
    assert "HN FILTER DRY RUN REPORT" in captured
    assert "High relevance compilers deep dive" in captured
    assert "✓ PICK" in captured
    assert "SKIP" in captured


@pytest.mark.asyncio
async def test_pipeline_live_run_updates_store(tmp_path, mock_pipeline_dependencies):
    """Verify live run delivers digest to Discord and updates seen.json."""
    seen_file = tmp_path / "seen.json"
    settings = Settings(
        gemini_api_key="test-api-key",
        discord_webhook_url="https://discord.com/webhook",
        seen_file=seen_file,
        threshold=7,
        _env_file=None,
    )

    exit_code = await run_pipeline(settings, dry_run=False)

    assert exit_code == 0
    mock_pipeline_dependencies["send_digest"].assert_called_once()
    _args, kwargs = mock_pipeline_dependencies["send_digest"].call_args
    # Only story 102 met threshold (score 9 >= 7)
    assert len(kwargs["picks"]) == 1
    assert kwargs["picks"][0].story.id == 102

    # Verify seen.json was saved with BOTH stories tracked
    store = SeenStore.load(seen_file)
    assert len(store) == 2
    assert "101" in store
    assert "102" in store


@pytest.mark.asyncio
async def test_pipeline_discord_failure_aborts_store_save(tmp_path, mock_pipeline_dependencies):
    """Verify that if Discord delivery fails, seen.json is not modified (allowing retry)."""
    seen_file = tmp_path / "seen.json"
    settings = Settings(
        gemini_api_key="test-api-key",
        discord_webhook_url="https://discord.com/webhook",
        seen_file=seen_file,
        _env_file=None,
    )

    mock_pipeline_dependencies["send_digest"].side_effect = RuntimeError("Discord is down")

    exit_code = await run_pipeline(settings, dry_run=False)

    assert exit_code == 1
    assert not seen_file.exists()  # State was safely preserved untouched


@pytest.mark.asyncio
async def test_pipeline_zero_stories_flow(tmp_path, mock_pipeline_dependencies):
    """Verify when all stories are already seen, scoring is skipped and status is posted."""
    seen_file = tmp_path / "seen.json"
    # Pre-seed seen store with stories 101 and 102
    initial_store = SeenStore(seen_file, seen={"101": "2026-10-14", "102": "2026-10-14"})
    initial_store.save()

    settings = Settings(
        gemini_api_key="test-api-key",
        discord_webhook_url="https://discord.com/webhook",
        seen_file=seen_file,
        _env_file=None,
    )

    exit_code = await run_pipeline(settings, dry_run=False)

    assert exit_code == 0
    mock_pipeline_dependencies["score"].assert_not_called()
    mock_pipeline_dependencies["send_digest"].assert_not_called()
    mock_pipeline_dependencies["send_msg"].assert_called_once()
    assert "0 new stories" in mock_pipeline_dependencies["send_msg"].call_args[0][1]


@pytest.mark.asyncio
async def test_pipeline_missing_credentials(tmp_path):
    """Verify missing credentials return exit code 1."""
    seen_file = tmp_path / "seen.json"

    # Missing Gemini key
    s1 = Settings(gemini_api_key="", discord_webhook_url="https://webhook", seen_file=seen_file, _env_file=None)
    assert await run_pipeline(s1, dry_run=False) == 1

    # Missing Discord URL in live run
    s2 = Settings(gemini_api_key="key", discord_webhook_url="", seen_file=seen_file, _env_file=None)
    assert await run_pipeline(s2, dry_run=False) == 1


def test_main_cli_arg_parsing(monkeypatch):
    """Verify main() parses command line flags and delegates to run_pipeline."""
    mock_run = AsyncMock(return_value=0)
    monkeypatch.setattr("hnfilter.main.run_pipeline", mock_run)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    exit_code = main(["--dry-run", "--limit", "25", "--threshold", "8", "--verbose"])

    assert exit_code == 0
    mock_run.assert_called_once()
    assert mock_run.call_args[1]["dry_run"] is True
    assert mock_run.call_args[1]["limit"] == 25
    assert mock_run.call_args[1]["threshold"] == 8
