"""Gemini LLM story scoring engine using structured outputs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from google import genai
from google.genai import errors, types

from hnfilter.models import Story, Verdict, VerdictBatch

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_INSTRUCTION = """You are a technical news curator filtering Hacker News stories for a software engineer.
Your task is to score each story from 0 to 10 based strictly on how well it matches the user's interests.
Be selective and strict: most stories (unrelated, hype, superficial) should score 4 or lower.
Only stories with high technical substance, practical engineering value, or deep dives matching interests should score 7 or above.
Return a verdict for every story provided.
"""


def load_interests(interests_path: Path | str | None) -> str:
    """Load the user's interest profile from markdown file, falling back to default."""
    if interests_path is not None:
        path = Path(interests_path)
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return f"{DEFAULT_SYSTEM_INSTRUCTION}\n\nUSER INTEREST PROFILE:\n{content}"
            except OSError as exc:
                logger.warning("Could not read interests file at %s: %s", path, exc)
    return DEFAULT_SYSTEM_INSTRUCTION


def chunk_stories(stories: Sequence[Story], batch_size: int = 40) -> list[list[Story]]:
    """Split a list of stories into batches of at most batch_size."""
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    return [list(stories[i : i + batch_size]) for i in range(0, len(stories), batch_size)]


def format_batch_prompt(stories: Sequence[Story]) -> str:
    """Format a batch of stories into concise JSON for the model."""
    items = [
        {
            "id": s.id,
            "title": s.title,
            "domain": s.domain,
            "points": s.score,
            "comments": s.comments,
        }
        for s in stories
    ]
    return (
        "Score each of the following stories from 0 to 10 based on how well it matches my interests. "
        "Return a verdict for each story.\n\n"
        f"Stories:\n{json.dumps(items, indent=2)}"
    )


def score_batch(
    client: genai.Client,
    model: str,
    stories: Sequence[Story],
    system_instruction: str,
    max_retries: int = 3,
    base_backoff: float = 2.0,
) -> list[Verdict]:
    """Score a single batch of stories using Gemini structured outputs."""
    if not stories:
        return []

    prompt = format_batch_prompt(stories)
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=VerdictBatch,
    )
    #for m in client.models.list():
    #    print(m.name)

    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )

            # Extract parsed VerdictBatch or validate text JSON
            if hasattr(response, "parsed") and isinstance(response.parsed, VerdictBatch):
                batch_result = response.parsed
            elif hasattr(response, "text") and response.text:
                batch_result = VerdictBatch.model_validate_json(response.text)
            else:
                raise ValueError("Empty or unparseable response from Gemini API")

            # Map results by ID for consistent output ordering and missing ID handling
            verdict_map = {v.id: v for v in batch_result.verdicts}
            ordered_verdicts: list[Verdict] = []

            for story in stories:
                if story.id in verdict_map:
                    ordered_verdicts.append(verdict_map[story.id])
                else:
                    logger.warning("Gemini omitted verdict for story %d; defaulting to score 0", story.id)
                    ordered_verdicts.append(
                        Verdict(id=story.id, score=0, reason="Scorer omitted story")
                    )

            return ordered_verdicts

        except Exception as exc:
            is_retryable = False
            if isinstance(exc, errors.APIError) and exc.code in (429, 500, 502, 503, 504) or any(token in str(exc) for token in ("429", "ResourceExhausted", "503", "500")):
                is_retryable = True

            if is_retryable and attempt < max_retries:
                sleep_seconds = base_backoff * (2**attempt)
                logger.warning(
                    "Gemini API rate limit or transient error on attempt %d: %s. Backing off for %.1fs...",
                    attempt + 1,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                continue

            logger.error("Failed to score batch of %d stories: %s", len(stories), exc)
            raise

    raise RuntimeError(f"Failed to score batch of {len(stories)} stories")


def score_stories(
    stories: Sequence[Story],
    api_key: str,
    model: str = "gemini-2.5-flash",
    batch_size: int = 40,
    interests_path: Path | str | None = None,
    client: genai.Client | None = None,
    max_retries: int = 3,
    base_backoff: float = 2.0,
) -> list[Verdict]:
    """Score all stories in batches using Gemini API."""
    if not stories:
        return []

    if client is None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required to score stories")
        client = genai.Client(api_key=api_key)

    system_instruction = load_interests(interests_path)
    batches = chunk_stories(stories, batch_size=batch_size)
    all_verdicts: list[Verdict] = []

    logger.info("Scoring %d stories across %d batch(es) with model '%s'", len(stories), len(batches), model)

    for i, batch in enumerate(batches, 1):
        logger.debug("Scoring batch %d/%d (%d stories)...", i, len(batches), len(batch))
        batch_verdicts = score_batch(
            client=client,
            model=model,
            stories=batch,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_backoff=base_backoff,
        )
        all_verdicts.extend(batch_verdicts)

    return all_verdicts
