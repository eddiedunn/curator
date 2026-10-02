"""Tests for per-subscription expiry (content_ttl_days), counted from the publish date."""

import pytest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from curator.config import CuratorSettings
from curator.daemon import SubscriptionDaemon
from curator.models import SubscriptionType
from curator.orchestrator import IngestionOrchestrator
from curator.plugins.base import ContentMetadata
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


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


def _days_ago(days):
    return (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat()


def _subscription(storage, ttl_days):
    return storage.create_subscription(
        name="Channel",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://www.youtube.com/@channel",
        content_ttl_days=ttl_days,
    )


def _item(storage, sub_id, video_id, published_days_ago, status="completed"):
    item_id = storage.create_ingested_item(
        source_type="youtube",
        source_id=video_id,
        source_url=f"https://youtube.com/watch?v={video_id}",
        title=f"Video {video_id}",
        published_at=_days_ago(published_days_ago) if published_days_ago is not None else None,
        subscription_id=sub_id,
    )
    storage.update_ingested_item(item_id, status=status)
    return item_id


def _response(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {}
    return resp


def _engram_delete(status_code=200):
    client_cls = patch("curator.daemon.httpx.AsyncClient").start()
    client = client_cls.return_value.__aenter__.return_value
    client.delete = AsyncMock(return_value=_response(status_code))
    return client


# --- which items count as expired -------------------------------------------

def test_expired_items_are_counted_from_publish_date(storage):
    sub = _subscription(storage, ttl_days=180)
    old = _item(storage, sub, "oldvideo001", published_days_ago=400)
    _item(storage, sub, "newvideo001", published_days_ago=30)

    expired = storage.get_expired_items()

    assert [i["id"] for i in expired] == [old]
    assert expired[0]["content_ttl_days"] == 180


def test_subscriptions_without_a_limit_never_expire(storage):
    sub = _subscription(storage, ttl_days=None)
    _item(storage, sub, "ancient0001", published_days_ago=3000)

    assert storage.get_expired_items() == []


def test_expired_failed_and_pending_items_are_included_but_skipped_ones_are_not(storage):
    sub = _subscription(storage, ttl_days=190)
    failed = _item(storage, sub, "failedold01", published_days_ago=500, status="failed")
    pending = _item(storage, sub, "pendingold1", published_days_ago=500, status="pending")
    _item(storage, sub, "skippedold1", published_days_ago=500, status="skipped")

    assert sorted(i["id"] for i in storage.get_expired_items()) == sorted([failed, pending])


def test_items_without_a_publish_date_fall_back_to_when_they_were_collected(storage):
    sub = _subscription(storage, ttl_days=180)
    _item(storage, sub, "nodate00001", published_days_ago=None)  # collected just now

    assert storage.get_expired_items() == []


# --- the daily purge ----------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_removes_expired_video_from_engram_and_keeps_a_skipped_record(storage, settings):
    sub = _subscription(storage, ttl_days=180)
    old = _item(storage, sub, "oldvideo001", published_days_ago=400)
    recent = _item(storage, sub, "newvideo001", published_days_ago=30)
    client = _engram_delete(200)

    await SubscriptionDaemon(storage, settings)._purge_expired_content()

    client.delete.assert_awaited_once_with("/api/v1/content/oldvideo001")
    item = storage.get_ingested_item(old)
    assert item["status"] == "skipped"
    assert "180" in item["error_message"] and "Engram" in item["error_message"]
    # the record stays, so the next channel scan doesn't collect the video again
    assert storage.get_ingested_item_by_source("youtube", "oldvideo001") is not None
    assert storage.get_ingested_item(recent)["status"] == "completed"


@pytest.mark.asyncio
async def test_purge_treats_a_video_already_missing_from_engram_as_removed(storage, settings):
    sub = _subscription(storage, ttl_days=190)
    old = _item(storage, sub, "gonealready", published_days_ago=400, status="failed")
    _engram_delete(404)

    await SubscriptionDaemon(storage, settings)._purge_expired_content()

    assert storage.get_ingested_item(old)["status"] == "skipped"


@pytest.mark.asyncio
async def test_purge_leaves_the_record_alone_when_engram_fails(storage, settings):
    sub = _subscription(storage, ttl_days=180)
    old = _item(storage, sub, "oldvideo001", published_days_ago=400)
    _engram_delete(500)

    await SubscriptionDaemon(storage, settings)._purge_expired_content()

    # still completed, so tomorrow's purge tries again
    assert storage.get_ingested_item(old)["status"] == "completed"


# --- not collecting videos that are already too old ---------------------------

def _plugin(published_days_ago):
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = AsyncMock(return_value=ContentMetadata(
        content_id="oldvideo001", title="Old video", url="https://youtube.com/watch?v=oldvideo001",
        published_at=_days_ago(published_days_ago), duration_seconds=600,
    ))
    plugin.fetch_content = AsyncMock()
    return plugin


@pytest.mark.asyncio
async def test_video_older_than_the_limit_is_recorded_as_skipped_and_not_downloaded(storage, settings):
    sub = _subscription(storage, ttl_days=180)
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(published_days_ago=400)

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        await orchestrator.ingest_url("https://youtube.com/watch?v=oldvideo001", subscription_id=sub)

    plugin.fetch_content.assert_not_called()
    item = storage.get_ingested_item_by_source("youtube", "oldvideo001")
    assert item["status"] == "skipped"
    assert "180" in item["error_message"]


@pytest.mark.asyncio
async def test_video_within_the_limit_is_collected_as_usual(storage, settings):
    sub = _subscription(storage, ttl_days=180)
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(published_days_ago=30)

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin), \
         patch.object(orchestrator, "ingest", AsyncMock(return_value=(None, "oldvideo001", 3))):
        assert await orchestrator.ingest_url("https://youtube.com/watch?v=oldvideo001", subscription_id=sub) is True

    assert storage.get_ingested_item_by_source("youtube", "oldvideo001")["status"] == "completed"
