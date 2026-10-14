"""Unit tests for SeenStore state storage and deduplication."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

from hnfilter.models import Story
from hnfilter.store import SeenStore


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


def test_store_load_missing_file(tmp_path):
    """Verify loading from a non-existent path returns a clean empty store."""
    file_path = tmp_path / "seen.json"
    store = SeenStore.load(file_path)

    assert store.path == file_path
    assert len(store) == 0
    assert store.seen == {}
    assert store.last_run is None


def test_store_load_corrupt_file(tmp_path):
    """Verify corrupt JSON file logs warning and resets to empty store."""
    file_path = tmp_path / "seen.json"
    file_path.write_text("{ corrupt json ... not valid", encoding="utf-8")

    store = SeenStore.load(file_path)
    assert len(store) == 0
    assert store.seen == {}


def test_store_load_non_dict_json(tmp_path):
    """Verify JSON that is a list instead of a dictionary resets cleanly."""
    file_path = tmp_path / "seen.json"
    file_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    store = SeenStore.load(file_path)
    assert len(store) == 0


def test_store_load_valid_file(tmp_path):
    """Verify loading a valid seen.json file populates fields correctly."""
    file_path = tmp_path / "seen.json"
    content = {
        "version": 1,
        "last_run": "2026-10-14T06:00:00Z",
        "seen": {
            "1001": "2026-10-13",
            "1002": "2026-10-14",
        },
    }
    file_path.write_text(json.dumps(content), encoding="utf-8")

    store = SeenStore.load(file_path)
    assert len(store) == 2
    assert "1001" in store
    assert 1002 in store
    assert store.last_run == "2026-10-14T06:00:00Z"


def test_filter_new(tmp_path):
    """Verify filter_new drops already-seen stories and keeps new ones."""
    file_path = tmp_path / "seen.json"
    store = SeenStore(path=file_path, seen={"101": "2026-10-14"})

    story1 = _make_story(101)  # already seen
    story2 = _make_story(102)  # new
    story3 = _make_story(103)  # new

    new_stories = store.filter_new([story1, story2, story3])
    assert len(new_stories) == 2
    assert [s.id for s in new_stories] == [102, 103]


def test_mark_seen_and_save(tmp_path):
    """Verify mark_seen adds IDs and save writes atomic JSON."""
    file_path = tmp_path / "subfolder" / "seen.json"
    store = SeenStore.load(file_path)

    story = _make_story(501)
    store.mark_seen([story, 502, "503"], date_str="2026-10-14")
    assert len(store) == 3
    assert story in store
    assert 502 in store
    assert "503" in store

    store.save()

    assert file_path.exists()
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == 1
    assert data["seen"]["501"] == "2026-10-14"
    assert data["seen"]["502"] == "2026-10-14"
    assert data["seen"]["503"] == "2026-10-14"
    assert "last_run" in data


def test_prune_old_entries(tmp_path):
    """Verify prune drops entries older than max_age_days and keeps fresh ones."""
    file_path = tmp_path / "seen.json"
    store = SeenStore(
        path=file_path,
        seen={
            "old_story": "2026-09-01",     # ~43 days ago
            "fresh_story": "2026-10-12",   # 2 days ago
            "today_story": "2026-10-14",   # today
            "bad_date_story": "not-a-date", # malformed date -> should be pruned
        },
    )

    reference_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    pruned = store.prune(max_age_days=14, now=reference_now)

    assert pruned == 2
    assert len(store) == 2
    assert "fresh_story" in store
    assert "today_story" in store
    assert "old_story" not in store
    assert "bad_date_story" not in store


def test_prune_default_now(tmp_path):
    """Verify prune uses current UTC time by default."""
    file_path = tmp_path / "seen.json"
    store = SeenStore(path=file_path)
    store.mark_seen([101])  # Today's date
    store.seen["old"] = "2020-01-01"

    pruned = store.prune(max_age_days=14)
    assert pruned == 1
    assert 101 in store
    assert "old" not in store


def test_save_atomic_cleanup_on_error(tmp_path):
    """Verify that if atomic replace fails, the temporary file is cleaned up."""
    file_path = tmp_path / "seen.json"
    store = SeenStore(path=file_path)
    store.mark_seen([101])

    with patch("os.replace", side_effect=OSError("Disk write error")):
        try:
            store.save()
        except OSError:
            pass

    # Ensure no lingering .tmp file remains in tmp_path
    tmp_files = list(tmp_path.glob("*.tmp*"))
    assert tmp_files == []
