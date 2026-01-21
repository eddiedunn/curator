"""Tests for the subscription daemon."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from curator.daemon import SubscriptionDaemon
from curator.storage import CuratorStorage
from curator.config import CuratorSettings


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def storage(temp_dir):
    """Create test storage."""
    db_path = temp_dir / "test.db"
    return CuratorStorage(str(db_path))


@pytest.fixture
def settings(temp_dir):
    """Create test settings."""
    return CuratorSettings(
        data_dir=temp_dir / "data",
        cache_dir=temp_dir / "cache",
        check_interval=60  # 1 minute for tests
    )


@pytest.fixture
def daemon(storage, settings):
    """Create daemon instance."""
    # Add daemon_check_interval_seconds if not present
    if not hasattr(settings, 'daemon_check_interval_seconds'):
        settings.daemon_check_interval_seconds = settings.check_interval

    daemon = SubscriptionDaemon(storage, settings)
    yield daemon

    # Clean up
    if daemon.running:
        daemon.shutdown()


def test_daemon_initialization(daemon, storage, settings):
    """Test daemon initializes correctly."""
    assert daemon.storage == storage
    assert daemon.settings == settings
    assert daemon.running is False
    assert daemon.scheduler is not None


def test_daemon_lock_acquisition(daemon, temp_dir):
    """Test daemon acquires file lock."""
    daemon._acquire_lock()

    lock_path = temp_dir / "data" / "daemon.lock"
    assert lock_path.exists()
    assert daemon._lock_file is not None


def test_daemon_lock_prevents_multiple_instances(daemon, storage, settings, temp_dir):
    """Test that only one daemon instance can run."""
    # First daemon acquires lock
    daemon._acquire_lock()

    # Second daemon should fail to acquire lock
    daemon2 = SubscriptionDaemon(storage, settings)
    with pytest.raises(RuntimeError, match="Another daemon instance is already running"):
        daemon2._acquire_lock()

    daemon.shutdown()


def test_daemon_shutdown(daemon):
    """Test daemon shutdown."""
    # Mock scheduler
    daemon.scheduler = MagicMock()
    daemon.running = True
    daemon._lock_file = MagicMock()

    daemon.shutdown()

    assert daemon.running is False
    daemon.scheduler.shutdown.assert_called_once_with(wait=True)


def test_daemon_scheduler_configuration(daemon, settings):
    """Test scheduler is configured with correct interval."""
    with patch.object(daemon, '_acquire_lock'):
        with patch.object(daemon.scheduler, 'start'):
            with patch('curator.daemon.asyncio.get_event_loop') as mock_loop:
                mock_loop.return_value.run_forever.side_effect = KeyboardInterrupt

                try:
                    daemon.run()
                except (KeyboardInterrupt, SystemExit):
                    pass

                # Check that scheduler job was added
                jobs = daemon.scheduler.get_jobs()
                assert len(jobs) == 1
                assert jobs[0].id == "check_subscriptions"


@pytest.mark.asyncio
async def test_check_subscriptions_no_subscriptions(daemon):
    """Test checking subscriptions when none are due."""
    with patch.object(daemon.storage, 'get_subscriptions_due_for_check', return_value=[]):
        await daemon._check_subscriptions()
        # Should complete without error


@pytest.mark.asyncio
async def test_check_subscriptions_with_subscriptions(daemon):
    """Test checking subscriptions."""
    mock_subscription = {
        "id": 1,
        "name": "Test Channel",
        "source_url": "https://youtube.com/@test",
        "subscription_type": "youtube_channel"
    }

    with patch.object(daemon.storage, 'get_subscriptions_due_for_check', return_value=[mock_subscription]):
        with patch.object(daemon, '_process_subscription', new_callable=AsyncMock) as mock_process:
            await daemon._check_subscriptions()
            mock_process.assert_called_once_with(mock_subscription)


@pytest.mark.asyncio
async def test_process_subscription_no_plugin(daemon):
    """Test processing subscription with no matching plugin."""
    subscription = {
        "id": 1,
        "name": "Test",
        "source_url": "https://example.com/unknown",
        "subscription_type": "unknown"
    }

    with patch.object(daemon.storage, 'update_subscription') as mock_update:
        with patch.object(daemon.orchestrator, 'get_plugin_for_url', return_value=None):
            await daemon._process_subscription(subscription)

            # Should update subscription with error status
            assert mock_update.call_count >= 1
            # First call should update last_checked_at
            # Second call should set error status


@pytest.mark.asyncio
async def test_process_subscription_existing_content(daemon):
    """Test processing subscription when content already ingested."""
    subscription = {
        "id": 1,
        "name": "Test",
        "source_url": "https://youtube.com/watch?v=test123",
        "subscription_type": "youtube_video"
    }

    mock_plugin = MagicMock()
    mock_metadata = MagicMock()
    mock_metadata.content_id = "test123"
    mock_metadata.title = "Test Video"
    mock_plugin.fetch_metadata = AsyncMock(return_value=mock_metadata)
    mock_plugin.source_type = "youtube"

    existing_item = {"id": 1, "source_id": "test123"}

    with patch.object(daemon.orchestrator, 'get_plugin_for_url', return_value=mock_plugin):
        with patch.object(daemon.storage, 'get_ingested_item_by_source', return_value=existing_item):
            with patch.object(daemon.storage, 'update_subscription'):
                with patch.object(daemon.orchestrator, 'ingest_url') as mock_ingest:
                    await daemon._process_subscription(subscription)

                    # Should not trigger ingestion for existing content
                    mock_ingest.assert_not_called()


@pytest.mark.asyncio
async def test_process_subscription_new_content(daemon):
    """Test processing subscription with new content."""
    subscription = {
        "id": 1,
        "name": "Test",
        "source_url": "https://youtube.com/watch?v=test123",
        "subscription_type": "youtube_video"
    }

    mock_plugin = MagicMock()
    mock_metadata = MagicMock()
    mock_metadata.content_id = "test123"
    mock_metadata.title = "Test Video"
    mock_plugin.fetch_metadata = AsyncMock(return_value=mock_metadata)
    mock_plugin.source_type = "youtube"

    with patch.object(daemon.orchestrator, 'get_plugin_for_url', return_value=mock_plugin):
        with patch.object(daemon.storage, 'get_ingested_item_by_source', return_value=None):
            with patch.object(daemon.storage, 'update_subscription'):
                with patch.object(daemon.orchestrator, 'ingest_url', new_callable=AsyncMock) as mock_ingest:
                    await daemon._process_subscription(subscription)

                    # Should trigger ingestion for new content
                    mock_ingest.assert_called_once_with(
                        "https://youtube.com/watch?v=test123",
                        subscription_id=1
                    )


@pytest.mark.asyncio
async def test_process_subscription_error_handling(daemon):
    """Test error handling during subscription processing."""
    subscription = {
        "id": 1,
        "name": "Test",
        "source_url": "https://youtube.com/watch?v=test123",
        "subscription_type": "youtube_video"
    }

    with patch.object(daemon.storage, 'update_subscription') as mock_update:
        with patch.object(daemon.orchestrator, 'get_plugin_for_url', side_effect=Exception("Test error")):
            await daemon._process_subscription(subscription)

            # Should update subscription with error status
            calls = mock_update.call_args_list
            # Check that at least one call set status to ERROR
            assert any('status' in str(call) and 'error' in str(call).lower() for call in calls)


def test_signal_handler(daemon):
    """Test signal handler calls shutdown."""
    with patch.object(daemon, 'shutdown') as mock_shutdown:
        with pytest.raises(SystemExit):
            daemon._signal_handler(2, None)  # SIGINT

        mock_shutdown.assert_called_once()
