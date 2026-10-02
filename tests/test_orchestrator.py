"""Tests for IngestionOrchestrator failure handling."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from curator.config import CuratorSettings
from curator.daemon import SubscriptionDaemon
from curator.orchestrator import IngestionOrchestrator
from curator.plugins.base import ContentUnavailableError
from curator.storage import CuratorStorage

VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://youtube.com/watch?v={VIDEO_ID}"
MEMBERS_ONLY_MSG = (
    f"ERROR: [youtube] {VIDEO_ID}: Join this channel to get access to "
    "members-only content like this video, and other exclusive perks."
)


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


def _plugin(fetch_metadata):
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = fetch_metadata
    return plugin


@pytest.mark.asyncio
async def test_ingest_url_records_unavailable_video_as_skipped(storage, settings):
    """A members-only video gets a 'skipped' row carrying the yt-dlp message."""
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(AsyncMock(side_effect=ContentUnavailableError(MEMBERS_ONLY_MSG, content_id=VIDEO_ID)))

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        result = await orchestrator.ingest_url(VIDEO_URL, subscription_id=None)

    assert result is False
    item = storage.get_ingested_item_by_source("youtube", VIDEO_ID)
    assert item is not None
    assert item["status"] == "skipped"
    assert "members-only" in item["error_message"]
    assert storage.get_ingested_item_counts_by_status() == {"skipped": 1}


@pytest.mark.asyncio
async def test_ingest_url_transient_metadata_failure_leaves_no_row(storage, settings):
    """A transient metadata failure (fetch_metadata -> None) is still not recorded."""
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(AsyncMock(return_value=None))

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        result = await orchestrator.ingest_url(VIDEO_URL)

    assert result is False
    assert storage.get_ingested_item_by_source("youtube", VIDEO_ID) is None


@pytest.mark.asyncio
async def test_daemon_does_not_retry_members_only_video(storage, settings):
    """Two channel scans: a members-only video is fetched once, then skipped."""
    daemon = SubscriptionDaemon(storage, settings)
    fetch_metadata = AsyncMock(side_effect=ContentUnavailableError(MEMBERS_ONLY_MSG, content_id=VIDEO_ID))
    plugin = _plugin(fetch_metadata)
    plugin.fetch_channel_videos = AsyncMock(return_value=[VIDEO_ID])

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        await daemon._process_youtube_channel(1, "https://youtube.com/@chan", plugin)
        await daemon._process_youtube_channel(1, "https://youtube.com/@chan", plugin)

    assert fetch_metadata.await_count == 1


@pytest.mark.asyncio
async def test_daemon_still_retries_transient_metadata_failure(storage, settings):
    """Two channel scans: a transient metadata failure is attempted both times."""
    daemon = SubscriptionDaemon(storage, settings)
    fetch_metadata = AsyncMock(return_value=None)
    plugin = _plugin(fetch_metadata)
    plugin.fetch_channel_videos = AsyncMock(return_value=[VIDEO_ID])

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        await daemon._process_youtube_channel(1, "https://youtube.com/@chan", plugin)
        await daemon._process_youtube_channel(1, "https://youtube.com/@chan", plugin)

    assert fetch_metadata.await_count == 2


@pytest.mark.asyncio
async def test_single_video_subscription_unavailable_does_not_error_subscription(storage, settings):
    """fetch_metadata raising ContentUnavailableError must not flip the subscription to 'error'."""
    daemon = SubscriptionDaemon(storage, settings)
    plugin = _plugin(AsyncMock(side_effect=ContentUnavailableError(MEMBERS_ONLY_MSG, content_id=VIDEO_ID)))
    subscription = {"id": 1, "name": "Test", "source_url": VIDEO_URL, "subscription_type": "youtube_video"}

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        with patch.object(daemon.storage, "update_subscription") as mock_update:
            await daemon._process_subscription(subscription)

    assert not any("error" in str(call).lower() for call in mock_update.call_args_list)


PREMIERE_MSG = f"ERROR: [youtube] {VIDEO_ID}: Premieres in 8 hours"


@pytest.mark.asyncio
async def test_ingest_url_upcoming_premiere_logs_info_and_leaves_no_row(storage, settings):
    """A premiere is not an error: info log only, no row, so a later scan retries it."""
    from structlog.testing import capture_logs
    from curator.plugins.base import ContentNotYetAvailableError

    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(AsyncMock(side_effect=ContentNotYetAvailableError(PREMIERE_MSG, content_id=VIDEO_ID)))

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        with capture_logs() as logs:
            result = await orchestrator.ingest_url(VIDEO_URL)

    assert result is False
    assert storage.get_ingested_item_by_source("youtube", VIDEO_ID) is None
    assert not [entry for entry in logs if entry["log_level"] == "error"]
    assert any(entry["log_level"] == "info" and "not yet available" in entry["event"] for entry in logs)


@pytest.mark.asyncio
async def test_single_video_subscription_premiere_does_not_error_subscription(storage, settings):
    from curator.plugins.base import ContentNotYetAvailableError

    daemon = SubscriptionDaemon(storage, settings)
    plugin = _plugin(AsyncMock(side_effect=ContentNotYetAvailableError(PREMIERE_MSG, content_id=VIDEO_ID)))
    subscription = {"id": 1, "name": "Test", "source_url": VIDEO_URL, "subscription_type": "youtube_video"}

    with patch.object(daemon.orchestrator, "_get_plugin_for_url", return_value=plugin):
        with patch.object(daemon.storage, "update_subscription") as mock_update:
            await daemon._process_subscription(subscription)

    assert not any("error" in str(call).lower() for call in mock_update.call_args_list)
