"""Tests for retrying failed / stale pending items with backoff."""

import sqlite3
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from curator.config import CuratorSettings
from curator.daemon import SubscriptionDaemon
from curator.models import SubscriptionType
from curator.storage import CuratorStorage


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def storage(temp_dir):
    return CuratorStorage(str(temp_dir / "test.db"))


@pytest.fixture
def settings(temp_dir):
    return CuratorSettings(data_dir=temp_dir / "data", cache_dir=temp_dir / "cache")


def _row(storage, video_id, status, hours_ago, error="[Errno -2] Name or service not known",
         subscription_id=None, retry_count=0, next_retry_in_hours=None):
    item_id = storage.create_ingested_item(
        source_type="youtube", source_id=video_id,
        source_url=f"https://youtube.com/watch?v={video_id}", title=video_id,
        subscription_id=subscription_id,
    )
    storage.update_ingested_item(item_id, status=status, error_message=error, retry_count=retry_count)
    with sqlite3.connect(storage.database_path) as conn:
        conn.execute("UPDATE ingested_items SET ingested_at = datetime('now', ?) WHERE id = ?",
                     (f"-{hours_ago} hours", item_id))
        if next_retry_in_hours is not None:
            conn.execute("UPDATE ingested_items SET next_retry_at = datetime('now', ?) WHERE id = ?",
                         (f"{next_retry_in_hours:+} hours", item_id))
    return item_id


def _due_ids(storage, limit=100):
    items = storage.get_items_due_for_retry(
        max_attempts=5, first_retry_hours=1, stale_pending_hours=6, limit=limit,
    )
    return {i["source_id"] for i in items}


# --- Schema -----------------------------------------------------------------

def test_existing_database_gets_retry_columns(temp_dir):
    """A DB created before the retry columns existed is migrated on startup."""
    db = temp_dir / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE ingested_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER,
                source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_url TEXT NOT NULL,
                title TEXT NOT NULL, author TEXT, published_at TIMESTAMP,
                ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                chunk_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT, metadata TEXT DEFAULT '{}',
                UNIQUE(source_type, source_id))
        """)
        conn.execute("INSERT INTO ingested_items (source_type, source_id, source_url, title, status) "
                     "VALUES ('youtube', 'aaaaaaaaaaa', 'u', 't', 'failed')")

    storage = CuratorStorage(str(db))

    item = storage.get_ingested_item_by_source("youtube", "aaaaaaaaaaa")
    assert item["retry_count"] == 0
    assert item["next_retry_at"] is None


# --- Eligibility ------------------------------------------------------------

def test_due_for_retry_selection(storage):
    _row(storage, "failed_old_", "failed", hours_ago=48)
    _row(storage, "failed_new_", "failed", hours_ago=0.5)          # < 1 h since failure
    _row(storage, "pending_old", "pending", hours_ago=7)
    _row(storage, "pending_new", "pending", hours_ago=2)           # may still be running
    _row(storage, "skipped____", "skipped", hours_ago=48, error="members-only content")
    _row(storage, "completed__", "completed", hours_ago=48, error=None)
    _row(storage, "exhausted__", "failed", hours_ago=48, retry_count=5)
    _row(storage, "backoff____", "failed", hours_ago=48, retry_count=2, next_retry_in_hours=3)
    _row(storage, "backoffdone", "failed", hours_ago=48, retry_count=2, next_retry_in_hours=-1)

    assert _due_ids(storage) == {"failed_old_", "pending_old", "backoffdone"}


def test_due_for_retry_ignores_disabled_subscriptions(storage):
    sub_id = storage.create_subscription("Off", SubscriptionType.YOUTUBE_CHANNEL,
                                         "https://youtube.com/@off", enabled=False)
    _row(storage, "disabled___", "failed", hours_ago=48, subscription_id=sub_id)
    assert _due_ids(storage) == set()


# --- Daemon -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_retries_are_capped_per_scan_and_backed_off(storage, settings):
    """A big backlog is worked off a few per scan, never in a burst."""
    for n in range(30):
        _row(storage, f"vid{n:08d}", "failed", hours_ago=48)
    daemon = SubscriptionDaemon(storage, settings)

    with patch.object(daemon.orchestrator, "ingest_url", new_callable=AsyncMock,
                      return_value=False) as ingest:
        await daemon._retry_failed_items()
        assert ingest.await_count == settings.retry_max_per_scan == 10

        await daemon._retry_failed_items()  # 20 left, 10 more
        await daemon._retry_failed_items()  # last 10
        await daemon._retry_failed_items()  # all 30 now waiting for their 4 h backoff
        assert ingest.await_count == 30

    retried = storage.get_ingested_item_by_source("youtube", "vid00000000")
    assert retried["retry_count"] == 1
    assert retried["next_retry_at"] is not None
    # Passes the row id, so even a metadata failure is recorded on this row.
    assert ingest.await_args_list[0].kwargs["item_id"] == retried["id"]


@pytest.mark.asyncio
async def test_retry_backoff_schedule(storage, settings):
    item_id = _row(storage, "vid00000000", "failed", hours_ago=48, retry_count=2)
    daemon = SubscriptionDaemon(storage, settings)

    with patch.object(daemon.orchestrator, "ingest_url", new_callable=AsyncMock, return_value=False):
        await daemon._retry_failed_items()

    with sqlite3.connect(storage.database_path) as conn:
        hours = conn.execute(
            "SELECT (julianday(next_retry_at) - julianday('now')) * 24, retry_count "
            "FROM ingested_items WHERE id = ?", (item_id,)).fetchone()
    assert hours[1] == 3
    assert hours[0] == pytest.approx(settings.retry_backoff_hours[3], abs=0.1)  # 24 h


@pytest.mark.asyncio
async def test_no_retries_during_youtube_cooldown(storage, settings):
    _row(storage, "vid00000000", "failed", hours_ago=48)
    daemon = SubscriptionDaemon(storage, settings)
    daemon.orchestrator.youtube_cooldown.trip("bot check")

    with patch.object(daemon.orchestrator, "ingest_url", new_callable=AsyncMock) as ingest:
        await daemon._retry_failed_items()

    ingest.assert_not_called()


@pytest.mark.asyncio
async def test_bot_check_during_retry_stops_and_does_not_use_an_attempt(storage, settings):
    for n in range(3):
        _row(storage, f"vid{n:08d}", "failed", hours_ago=48 - n)
    daemon = SubscriptionDaemon(storage, settings)

    async def blocked(*args, **kwargs):
        daemon.orchestrator.youtube_cooldown.trip("not a bot")
        return False

    with patch.object(daemon.orchestrator, "ingest_url", side_effect=blocked) as ingest:
        await daemon._retry_failed_items()

    assert ingest.await_count == 1
    first = storage.get_ingested_item_by_source("youtube", "vid00000000")
    assert first["retry_count"] == 0
    assert first["next_retry_at"] is None


@pytest.mark.asyncio
async def test_check_subscriptions_runs_retries(storage, settings):
    daemon = SubscriptionDaemon(storage, settings)
    with patch.object(daemon, "_retry_failed_items", new_callable=AsyncMock) as retry:
        await daemon._check_subscriptions()
    retry.assert_awaited_once()


def test_retry_settings_defaults():
    s = CuratorSettings()
    assert s.retry_max_attempts == 5
    assert s.retry_backoff_hours == [1, 4, 12, 24, 48]
    assert s.retry_max_per_scan == 10
    assert s.retry_stale_pending_hours == 6
