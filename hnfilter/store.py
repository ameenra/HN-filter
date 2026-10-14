"""Persistent storage and deduplication for seen Hacker News stories."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hnfilter.models import Story

logger = logging.getLogger(__name__)


class SeenStore:
    """Manages tracking, deduplication, and atomic persistence of seen story IDs."""

    def __init__(
        self,
        path: Path | str,
        version: int = 1,
        last_run: str | None = None,
        seen: dict[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.version = version
        self.last_run = last_run
        self.seen: dict[str, str] = seen if seen is not None else {}

    def __contains__(self, item: int | str | Story) -> bool:
        if isinstance(item, Story):
            return str(item.id) in self.seen
        return str(item) in self.seen

    def __len__(self) -> int:
        return len(self.seen)

    @classmethod
    def load(cls, path: Path | str) -> SeenStore:
        """Load seen store from disk.

        Returns an empty store if the file does not exist or is corrupted.
        """
        store_path = Path(path)
        if not store_path.exists():
            logger.debug("Seen store file not found at %s; starting with empty store", store_path)
            return cls(path=store_path)

        try:
            with open(store_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning("Invalid format in %s; resetting store", store_path)
                return cls(path=store_path)

            version = int(data.get("version", 1))
            last_run = data.get("last_run")
            raw_seen = data.get("seen", {})

            seen: dict[str, str] = {}
            if isinstance(raw_seen, dict):
                for k, v in raw_seen.items():
                    seen[str(k)] = str(v)

            logger.info("Loaded %d seen stories from %s", len(seen), store_path)
            return cls(path=store_path, version=version, last_run=last_run, seen=seen)

        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "Failed to parse seen store at %s (%s); starting fresh",
                store_path,
                exc,
            )
            return cls(path=store_path)

    def filter_new(self, stories: Sequence[Story]) -> list[Story]:
        """Return only stories that have not been seen before."""
        new_stories = [story for story in stories if str(story.id) not in self.seen]
        logger.info(
            "Deduplication: %d new stories remaining after filtering %d known",
            len(new_stories),
            len(stories) - len(new_stories),
        )
        return new_stories

    def mark_seen(
        self,
        items: Sequence[Story | int | str],
        date_str: str | None = None,
    ) -> None:
        """Mark stories or story IDs as seen with given or current UTC date."""
        if date_str is None:
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")

        for item in items:
            story_id = str(item.id) if isinstance(item, Story) else str(item)
            self.seen[story_id] = date_str

    def prune(self, max_age_days: int = 14, now: datetime | None = None) -> int:
        """Remove entries older than max_age_days to keep storage compact.

        Returns the number of pruned entries.
        """
        if now is None:
            now = datetime.now(UTC)

        cutoff_date = (now - timedelta(days=max_age_days)).date()
        pruned_count = 0
        retained: dict[str, str] = {}

        for story_id, date_str in self.seen.items():
            try:
                entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if entry_date >= cutoff_date:
                    retained[story_id] = date_str
                else:
                    pruned_count += 1
            except ValueError:
                # Discard corrupted/unparseable date entries
                pruned_count += 1

        self.seen = retained
        if pruned_count > 0:
            logger.info("Pruned %d expired seen stories older than %d days", pruned_count, max_age_days)
        return pruned_count

    def save(self) -> None:
        """Atomically persist seen state to disk via a temporary file and os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_run = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "version": self.version,
            "last_run": self.last_run,
            "seen": self.seen,
        }

        temp_path = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(temp_path, self.path)
            logger.info("Saved %d seen stories to %s", len(self.seen), self.path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise
