"""Data models for HN Filter."""

from __future__ import annotations

import urllib.parse
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Story(BaseModel):
    """Represents a curated Hacker News story."""

    id: int
    title: str
    url: str
    domain: str
    score: int
    comments: int = 0
    time: int = 0

    @property
    def hn_url(self) -> str:
        """Direct link to the discussion thread on Hacker News."""
        return f"https://news.ycombinator.com/item?id={self.id}"

    @classmethod
    def from_hn_item(cls, item: dict[str, Any] | None) -> Story | None:
        """Parse and normalize an item from the HN Firebase API.

        Returns None if item is not a valid, living story.
        """
        if not item or not isinstance(item, dict):
            return None
        if item.get("type") != "story":
            return None
        if item.get("dead") or item.get("deleted"):
            return None

        item_id = item.get("id")
        title = item.get("title")
        if item_id is None or not title:
            return None

        hn_item_url = f"https://news.ycombinator.com/item?id={item_id}"
        raw_url = item.get("url")
        if raw_url and isinstance(raw_url, str) and raw_url.strip():
            url = raw_url.strip()
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc or "news.ycombinator.com"
            domain = domain.removeprefix("www.")
        else:
            url = hn_item_url
            domain = "news.ycombinator.com"

        return cls(
            id=int(item_id),
            title=str(title).strip(),
            url=url,
            domain=domain,
            score=int(item.get("score") or 0),
            comments=int(item.get("descendants") or 0),
            time=int(item.get("time") or 0),
        )


class Verdict(BaseModel):
    """LLM scoring verdict for an individual story."""

    id: int
    score: int = Field(default=0, ge=0, le=10, description="Relevance score from 0 to 10")
    reason: str = Field(default="", description="Short one-line reason")

    @field_validator("reason", mode="before")
    @classmethod
    def sanitize_reason(cls, v: Any) -> str:
        text = str(v).strip() if v is not None else ""
        if len(text) > 200:
            return text[:197] + "..."
        return text

    @field_validator("score", mode="before")
    @classmethod
    def clamp_score(cls, v: Any) -> int:
        try:
            val = round(float(v))
            return max(0, min(10, val))
        except (ValueError, TypeError):
            return 0


class VerdictBatch(BaseModel):
    """Collection of verdicts used for Gemini structured outputs."""

    verdicts: list[Verdict] = Field(default_factory=list)


class DigestItem(BaseModel):
    """A story paired with its scoring verdict, ready for delivery."""

    story: Story
    verdict: Verdict

    @property
    def score(self) -> int:
        return self.verdict.score

    @property
    def reason(self) -> str:
        return self.verdict.reason
