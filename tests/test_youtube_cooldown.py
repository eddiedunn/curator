"""Tests for the pause on YouTube requests after a bot check / rate limit."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from curator.config import CuratorSettings
from curator.daemon import SubscriptionDaemon
from curator.models import SubscriptionType
from curator.orchestrator import IngestionOrchestrator, YouTubeCooldown
from curator.plugins.base import ContentMetadata, RateLimitedError
from curator.storage import CuratorStorage

BOT_CHECK_MSG = (
    "ERROR: [youtube] abcdefghijk: Sign in to confirm you’re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)
VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://youtube.com/watch?v={VIDEO_ID}"


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def storage(temp_dir):
    return CuratorStorage(str(temp_dir / "test.db"))


@pytest.fixture
def settings(temp_dir):
    return CuratorSettings(
        data_dir=temp_dir / "data",
        cache_dir=temp_dir / "cache",
        check_interval=60,
    )


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- YouTubeCooldown --------------------------------------------------------

def test_cooldown_doubles_on_repeated_hits_up_to_cap_and_resets_on_success():
    clock = FakeClock()
    cooldown = YouTubeCooldown(base_minutes=60, max_minutes=180, clock=clock)
    assert cooldown.active is False

    cooldown.trip("bot check")
    assert cooldown.active is True
    clock.now += 59 * 60
    assert cooldown.active is True
    clock.now += 61
    assert cooldown.active is False

    cooldown.trip("bot check")  # second hit in a row: 120 min
    clock.now += 119 * 60
    assert cooldown.active is True
    clock.now += 61
    cooldown.trip("bot check")  # third: 240 capped to 180
    clock.now += 179 * 60
    assert cooldown.active is True
    clock.now += 61
    assert cooldown.active is False

    cooldown.record_success()
    cooldown.trip("bot check")  # back to 60 min
    clock.now += 61 * 60
    assert cooldown.active is False


def test_cooldown_hit_while_active_does_not_extend():
    clock = FakeClock()
    cooldown = YouTubeCooldown(base_minutes=60, max_minutes=360, clock=clock)
    cooldown.trip("bot check")
    clock.now += 30 * 60
    cooldown.trip("bot check")
    clock.now += 31 * 60
    assert cooldown.active is False


def test_settings_defaults():
    s = CuratorSettings()
    assert s.youtube_cooldown_minutes == 60
    assert s.youtube_cooldown_max_minutes == 360


# --- Orchestrator -----------------------------------------------------------

def _metadata():
    return ContentMetadata(content_id=VIDEO_ID, title="A video", url=VIDEO_URL, duration_seconds=600)


@pytest.mark.asyncio
async def test_manual_fetch_bot_check_fails_job_with_clear_message(storage, settings):
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = AsyncMock(side_effect=RateLimitedError(BOT_CHECK_MSG))
    storage.create_fetch_job("job-1", VIDEO_URL)

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        result = await orchestrator.ingest_url(VIDEO_URL, job_id="job-1")

    assert result is False
    job = storage.get_fetch_job("job-1")
    assert job["status"] == "failed"
    assert "bot check" in job["error_message"]
    assert "not a bot" in job["error_message"]
    assert orchestrator.youtube_cooldown.active is True
    # No metadata, so no row: the next scan rediscovers it.
    assert storage.get_ingested_item_by_source("youtube", VIDEO_ID) is None


@pytest.mark.asyncio
async def test_bot_check_during_download_marks_row_failed_not_skipped(storage, settings):
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = AsyncMock(return_value=_metadata())
    plugin.fetch_content = AsyncMock(side_effect=RateLimitedError(BOT_CHECK_MSG))
    not_in_engram = MagicMock(status_code=404)

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin), \
         patch("curator.orchestrator.httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=not_in_engram)
        result = await orchestrator.ingest_url(VIDEO_URL)

    assert result is False
    item = storage.get_ingested_item_by_source("youtube", VIDEO_ID)
    assert item["status"] == "failed"
    assert "bot check" in item["error_message"]


# --- Daemon -----------------------------------------------------------------

def _channel_plugin(video_ids, fetch_metadata):
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_channel_videos = AsyncMock(return_value=video_ids)
    plugin.fetch_metadata = fetch_metadata
    return plugin


@pytest.mark.asyncio
async def test_bot_check_aborts_scan_and_skips_next_scans(storage, settings):
    daemon = SubscriptionDaemon(storage, settings)
    storage.create_subscription("A", SubscriptionType.YOUTUBE_CHANNEL,
                                "https://youtube.com/@a")
    storage.create_subscription("B", SubscriptionType.YOUTUBE_CHANNEL,
                                "https://youtube.com/@b")
    fetch_metadata = AsyncMock(side_effect=RateLimitedError(BOT_CHECK_MSG))
    plugin = _channel_plugin(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"], fetch_metadata)

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        await daemon._check_subscriptions()
        assert fetch_metadata.await_count == 1
        assert plugin.fetch_channel_videos.await_count == 1  # second channel not scanned

        # Make both subscriptions due again; the next scan must not touch YouTube.
        for sub in storage.list_subscriptions():
            storage.update_subscription(sub["id"], last_checked_at=None)
        await daemon._check_subscriptions()

    assert fetch_metadata.await_count == 1
    assert plugin.fetch_channel_videos.await_count == 1
    for sub in storage.list_subscriptions():
        assert sub["status"] == "active"


@pytest.mark.asyncio
async def test_bot_check_on_channel_listing_does_not_error_subscription(storage, settings):
    daemon = SubscriptionDaemon(storage, settings)
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_channel_videos = AsyncMock(side_effect=RateLimitedError(BOT_CHECK_MSG))
    subscription = {"id": 1, "name": "A", "source_url": "https://youtube.com/@a",
                    "subscription_type": "youtube_channel"}

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        with patch.object(daemon.storage, "update_subscription") as mock_update:
            await daemon._process_subscription(subscription)

    assert not any("error" in str(c).lower() for c in mock_update.call_args_list)
    assert daemon.orchestrator.youtube_cooldown.active is True


@pytest.mark.asyncio
async def test_visual_context_enrichment_skipped_during_cooldown(storage, settings):
    daemon = SubscriptionDaemon(storage, settings)
    daemon.orchestrator.youtube_cooldown.trip("bot check")

    with patch.object(storage, "get_items_pending_visual_context") as mock_queue:
        await daemon._enrich_visual_context()

    mock_queue.assert_not_called()


@pytest.mark.asyncio
async def test_glimpse_bot_check_trips_cooldown_without_using_an_attempt(storage, settings):
    daemon = SubscriptionDaemon(storage, settings)
    items = [{"id": 1, "source_id": "abc123", "visual_context_attempts": 1,
              "visual_context_status": "failed"},
             {"id": 2, "source_id": "def456", "visual_context_attempts": 0,
              "visual_context_status": None}]
    engram = MagicMock(status_code=200)
    engram.json.return_value = {"metadata": {"duration_seconds": 300, "segments": []}}

    with patch.object(storage, "get_items_pending_visual_context", return_value=items), \
         patch.object(storage, "update_visual_context_status") as mock_status, \
         patch("httpx.AsyncClient") as client_cls, \
         patch("curator.glimpse_client.select_frames", new_callable=AsyncMock,
               side_effect=RateLimitedError("stream_url_failed: yt-dlp -g failed rc=1: not a bot")) as mock_select:
        client_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=engram)
        await daemon._enrich_visual_context()

    assert mock_select.await_count == 1  # second item not attempted
    assert daemon.orchestrator.youtube_cooldown.active is True
    # Item 1 put back exactly as it was (status failed, 1 attempt).
    assert mock_status.call_args_list[-1].args == (1, "failed", 1)
