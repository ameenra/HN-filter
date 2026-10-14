"""Configuration management for HN Filter."""

from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    gemini_api_key: str = Field(
        default="",
        description="Google Gemini API key from AI Studio (required for scoring)",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model name to use for story scoring",
    )

    @field_validator("gemini_model", mode="before")
    @classmethod
    def default_empty_model(cls, v: Any) -> str:
        if not v or not str(v).strip():
            return "gemini-2.5-flash"
        return str(v).strip()

    discord_webhook_url: str = Field(
        default="",
        description="Discord Webhook URL for posting digests",
    )
    fetch_limit: int = Field(
        default=150,
        ge=1,
        le=500,
        description="Number of top HN stories to inspect",
    )
    min_hn_score: int = Field(
        default=10,
        ge=0,
        description="Minimum HN score threshold to consider a story",
    )
    batch_size: int = Field(
        default=40,
        ge=1,
        le=100,
        description="Number of story titles per Gemini API request batch",
    )
    threshold: int = Field(
        default=7,
        ge=0,
        le=10,
        description="Minimum LLM score for a story to be included in digest",
    )
    max_items: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of stories in a single digest",
    )
    interests_file: Path = Field(
        default=Path("interests.md"),
        description="Path to interest profile markdown file",
    )
    seen_file: Path = Field(
        default=Path("data/seen.json"),
        description="Path to persisted seen stories JSON file",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
