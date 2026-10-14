"""Unit tests for configuration management."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from hnfilter.config import Settings


def test_settings_defaults(monkeypatch):
    """Verify default settings values when no environment variables are set."""
    # Ensure env vars aren't leaking from current environment
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FETCH_LIMIT", raising=False)
    monkeypatch.delenv("MIN_HN_SCORE", raising=False)
    monkeypatch.delenv("BATCH_SIZE", raising=False)
    monkeypatch.delenv("THRESHOLD", raising=False)
    monkeypatch.delenv("MAX_ITEMS", raising=False)

    settings = Settings(_env_file=None)
    assert settings.gemini_api_key == ""
    assert settings.gemini_model == "gemini-2.5-flash"
    assert settings.discord_webhook_url == ""
    assert settings.fetch_limit == 150
    assert settings.min_hn_score == 10
    assert settings.batch_size == 40
    assert settings.threshold == 7
    assert settings.max_items == 10
    assert settings.interests_file == Path("interests.md")
    assert settings.seen_file == Path("data/seen.json")


def test_settings_env_override(monkeypatch):
    """Verify environment variables override defaults with appropriate type casting."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    monkeypatch.setenv("FETCH_LIMIT", "50")
    monkeypatch.setenv("MIN_HN_SCORE", "25")
    monkeypatch.setenv("BATCH_SIZE", "20")
    monkeypatch.setenv("THRESHOLD", "8")
    monkeypatch.setenv("MAX_ITEMS", "5")
    monkeypatch.setenv("INTERESTS_FILE", "custom_interests.md")
    monkeypatch.setenv("SEEN_FILE", "custom_seen.json")

    settings = Settings(_env_file=None)
    assert settings.gemini_api_key == "test-key-123"
    assert settings.gemini_model == "gemini-2.5-flash-lite"
    assert settings.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert settings.fetch_limit == 50
    assert settings.min_hn_score == 25
    assert settings.batch_size == 20
    assert settings.threshold == 8
    assert settings.max_items == 5
    assert settings.interests_file == Path("custom_interests.md")
    assert settings.seen_file == Path("custom_seen.json")


def test_settings_validation_bounds(monkeypatch):
    """Verify validation boundaries on configuration integers."""
    monkeypatch.setenv("THRESHOLD", "15")  # > 10
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    monkeypatch.setenv("THRESHOLD", "7")
    monkeypatch.setenv("BATCH_SIZE", "0")  # < 1
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_empty_model_fallback(monkeypatch):
    """Verify empty or blank GEMINI_MODEL falls back to gemini-2.5-flash."""
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-2.5-flash"

