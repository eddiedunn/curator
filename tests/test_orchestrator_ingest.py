"""Tests for what ingest_url records on the ingested_items row."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from curator.config import CuratorSettings
from curator.orchestrator import IngestionOrchestrator
from curator.plugins.base import ContentMetadata, ContentResult
from curator.storage import CuratorStorage

VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://youtube.com/watch?v={VIDEO_ID}"
OLD_DNS_ERROR = "[Errno -2] Name or service not known"


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


def _metadata(duration=600):
    return ContentMetadata(content_id=VIDEO_ID, title="A video", url=VIDEO_URL, duration_seconds=duration)


def _plugin(fetch_content):
    plugin = MagicMock()
    plugin.source_type = "youtube"
    plugin.fetch_metadata = AsyncMock(return_value=_metadata())
    plugin.fetch_content = fetch_content
    return plugin


def _response(status_code, data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data or {}
    resp.raise_for_status = MagicMock()
    return resp


def _engram(get_status=404, get_data=None, post_data=None):
    """Patch httpx.AsyncClient so Engram GET/POST return the given responses."""
    client_cls = patch("curator.orchestrator.httpx.AsyncClient").start()
    client = client_cls.return_value.__aenter__.return_value
    client.get = AsyncMock(return_value=_response(get_status, get_data))
    client.post = AsyncMock(return_value=_response(200, post_data))
    return client


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


def _existing_failed_row(storage):
    item_id = storage.create_ingested_item(
        source_type="youtube", source_id=VIDEO_ID, source_url=VIDEO_URL, title="A video",
    )
    storage.update_ingested_item(item_id, status="failed", error_message=OLD_DNS_ERROR)
    return item_id


@pytest.mark.asyncio
async def test_success_clears_old_error_and_records_chunk_count(storage, settings):
    item_id = _existing_failed_row(storage)
    orchestrator = IngestionOrchestrator(storage, settings)
    content = ContentResult(text="Hello there.", segments=[], source="api", needs_transcription=False)
    _engram(post_data={"id": "5d0f3c1e-0000-0000-0000-000000000000", "content_id": VIDEO_ID,
                       "chunk_count": 7, "message": "Content stored successfully"})

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=_plugin(AsyncMock(return_value=content))):
        assert await orchestrator.ingest_url(VIDEO_URL) is True

    item = storage.get_ingested_item(item_id)
    assert item["status"] == "completed"
    assert item["error_message"] is None
    assert item["chunk_count"] == 7


@pytest.mark.asyncio
async def test_already_in_engram_takes_chunk_count_from_engram(storage, settings):
    item_id = _existing_failed_row(storage)
    orchestrator = IngestionOrchestrator(storage, settings)
    _engram(get_status=200, get_data={"content_id": VIDEO_ID, "chunk_count": 12})

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=_plugin(AsyncMock())):
        assert await orchestrator.ingest_url(VIDEO_URL) is True

    item = storage.get_ingested_item(item_id)
    assert item["status"] == "completed"
    assert item["error_message"] is None
    assert item["chunk_count"] == 12


@pytest.mark.asyncio
async def test_failure_overwrites_old_error_with_current_reason(storage, settings):
    item_id = _existing_failed_row(storage)
    orchestrator = IngestionOrchestrator(storage, settings)
    _engram()
    download_error = RuntimeError(
        "Audio download failed: ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )

    with patch.object(orchestrator, "_get_plugin_for_url",
                      return_value=_plugin(AsyncMock(side_effect=download_error))):
        assert await orchestrator.ingest_url(VIDEO_URL) is False

    item = storage.get_ingested_item(item_id)
    assert item["status"] == "failed"
    assert "HTTP Error 403" in item["error_message"]
    assert "Errno -2" not in item["error_message"]


@pytest.mark.asyncio
async def test_metadata_failure_reports_yt_dlp_reason_on_fetch_job(storage, settings):
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(AsyncMock())
    plugin.fetch_metadata = AsyncMock(return_value=None)
    plugin.last_error = "ERROR: [youtube] abcdefghijk: Unable to download webpage: HTTP Error 503"
    storage.create_fetch_job("job-1", VIDEO_URL)

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        assert await orchestrator.ingest_url(VIDEO_URL, job_id="job-1") is False

    assert "HTTP Error 503" in storage.get_fetch_job("job-1")["error_message"]


@pytest.mark.asyncio
async def test_retry_metadata_failure_is_recorded_on_the_retried_row(storage, settings):
    item_id = _existing_failed_row(storage)
    orchestrator = IngestionOrchestrator(storage, settings)
    plugin = _plugin(AsyncMock())
    plugin.fetch_metadata = AsyncMock(return_value=None)
    plugin.last_error = "ERROR: [youtube] abcdefghijk: Unable to download webpage: HTTP Error 503"

    with patch.object(orchestrator, "_get_plugin_for_url", return_value=plugin):
        assert await orchestrator.ingest_url(VIDEO_URL, item_id=item_id) is False

    item = storage.get_ingested_item(item_id)
    assert item["status"] == "failed"
    assert "HTTP Error 503" in item["error_message"]
