"""Tests for daemon._enrich_visual_context background job."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
from curator.daemon import SubscriptionDaemon
from curator.storage import CuratorStorage
from curator.config import CuratorSettings


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def storage(temp_dir):
    db_path = temp_dir / "test.db"
    return CuratorStorage(str(db_path))


@pytest.fixture
def settings(temp_dir):
    return CuratorSettings(
        data_dir=temp_dir / "data",
        cache_dir=temp_dir / "cache",
        check_interval=60,
        glimpse_service_url="http://glimpse:8730",
        engram_api_url="http://engram:8800",
        glimpse_max_attempts=3,
        glimpse_max_frames=5,
        glimpse_scene_threshold=0.3,
        glimpse_proximity_seconds=5.0,
        glimpse_select_timeout_seconds=180.0,
        glimpse_frame_interval_seconds=60,
        glimpse_timeout_seconds=60.0,
        visual_context_batch_size=10,
        visual_context_enrich_interval_seconds=300,
    )


@pytest.fixture
def daemon(storage, settings):
    d = SubscriptionDaemon(storage, settings)
    yield d
    try:
        if d.running:
            d.shutdown()
    except Exception:
        d.running = False


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_item(item_id=1, source_id="abc123", attempts=0):
    return {
        "id": item_id,
        "source_id": source_id,
        "visual_context_attempts": attempts,
    }


def _make_engram_response(duration=300.0, segments=None):
    segs = segments or [{"start": 42.0}, {"start": 120.0}]
    return {
        "content_id": "abc123",
        "metadata": {
            "duration_seconds": duration,
            "segments": segs,
        },
    }


def _make_http_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Full cycle test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_full_cycle(daemon, storage):
    """Happy path: GET engram → select_frames → collect → PATCH → status=complete."""
    item = _make_item()
    engram_data = _make_engram_response()
    frame_results = [
        {"timestamp_sec": 42.0, "score": 2, "signals": ["segment", "scene_change"],
         "caption": "Test caption", "ocr_text": "TEST", "entity_types": ["person"]},
    ]

    from curator.glimpse_client import SelectedFrame
    selected_frames = [SelectedFrame(timestamp_sec=42.0, score=2, signals=["segment", "scene_change"])]

    get_resp = _make_http_response(engram_data)
    patch_resp = _make_http_response({})

    with patch.object(storage, "get_items_pending_visual_context", return_value=[item]):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            with patch("curator.glimpse_client.select_frames", new_callable=AsyncMock,
                       return_value=selected_frames) as mock_select:
                with patch("curator.glimpse_client.collect_visual_context", new_callable=AsyncMock,
                           return_value=frame_results) as mock_collect:
                    with patch("httpx.AsyncClient") as mock_client_cls:
                        mock_client = AsyncMock()
                        # GET engram returns engram data; PATCH returns empty
                        mock_client.get = AsyncMock(return_value=get_resp)
                        mock_client.patch = AsyncMock(return_value=patch_resp)
                        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                        await daemon._enrich_visual_context()

            # Status transitions: processing then complete
            calls = mock_status.call_args_list
            assert calls[0] == call(1, "processing", 1)
            assert calls[1] == call(1, "complete", 1)

            # select_frames called with correct video_id and segment timestamps
            mock_select.assert_called_once()
            call_kwargs = mock_select.call_args
            assert call_kwargs.kwargs["video_id"] == "abc123"
            assert 42.0 in call_kwargs.kwargs["segment_timestamps"]
            assert 120.0 in call_kwargs.kwargs["segment_timestamps"]

            # collect called with the selected frames
            mock_collect.assert_called_once()
            assert mock_collect.call_args.kwargs["video_id"] == "abc123"

            # PATCH issued to engram
            mock_client.patch.assert_called_once()
            patch_url = mock_client.patch.call_args.args[0]
            assert "abc123" in patch_url
            patch_body = mock_client.patch.call_args.kwargs["json"]
            assert "visual_context" in patch_body["metadata"]
            assert patch_body["metadata"]["visual_context"]["frame_count"] == 1


# ---------------------------------------------------------------------------
# No items in queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_no_items(daemon, storage):
    """No items pending — job returns silently."""
    with patch.object(storage, "get_items_pending_visual_context", return_value=[]):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            await daemon._enrich_visual_context()
            mock_status.assert_not_called()


# ---------------------------------------------------------------------------
# Retry cap enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_retry_cap_fails_permanently(daemon, storage):
    """Item that fails on its max_attempts-th attempt stays failed (not reset to None)."""
    # Attempt 2 of 3 — this will be the 3rd attempt (attempts+1 == max_attempts)
    item = _make_item(attempts=2)

    get_resp = _make_http_response(_make_engram_response())

    with patch.object(storage, "get_items_pending_visual_context", return_value=[item]):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            with patch("curator.glimpse_client.select_frames", new_callable=AsyncMock,
                       side_effect=Exception("Glimpse unavailable")):
                with patch("httpx.AsyncClient") as mock_client_cls:
                    mock_client = AsyncMock()
                    mock_client.get = AsyncMock(return_value=get_resp)
                    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                    await daemon._enrich_visual_context()

            calls = mock_status.call_args_list
            # First: mark processing with attempts=3
            assert calls[0] == call(1, "processing", 3)
            # Second: mark failed
            assert calls[1][0][1] == "failed"


# ---------------------------------------------------------------------------
# Engram 404 → item marked failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_engram_not_found(daemon, storage):
    """If Engram returns 404, item is marked skipped without consuming an attempt."""
    item = _make_item()
    not_found_resp = _make_http_response({}, status_code=404)
    not_found_resp.status_code = 404

    with patch.object(storage, "get_items_pending_visual_context", return_value=[item]):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=not_found_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                await daemon._enrich_visual_context()

            calls = mock_status.call_args_list
            assert calls[0] == call(1, "processing", 1)
            assert calls[1] == call(1, "skipped", 0)


# ---------------------------------------------------------------------------
# Interval fallback path (select_frames returns empty → status=complete, no PATCH)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_no_frames_selected_marks_complete(daemon, storage):
    """When no frames are selected, item is marked complete without PATCHing Engram."""
    item = _make_item()
    get_resp = _make_http_response(_make_engram_response())

    with patch.object(storage, "get_items_pending_visual_context", return_value=[item]):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            with patch("curator.glimpse_client.select_frames", new_callable=AsyncMock,
                       return_value=[]):
                with patch("httpx.AsyncClient") as mock_client_cls:
                    mock_client = AsyncMock()
                    mock_client.get = AsyncMock(return_value=get_resp)
                    mock_client.patch = AsyncMock()
                    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                    await daemon._enrich_visual_context()

            calls = mock_status.call_args_list
            assert calls[0] == call(1, "processing", 1)
            assert calls[1] == call(1, "complete", 1)
            # No PATCH should be issued
            mock_client.patch.assert_not_called()


# ---------------------------------------------------------------------------
# Multiple items in batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_multiple_items(daemon, storage):
    """Multiple items in batch are each processed independently."""
    items = [_make_item(item_id=1, source_id="vid1"), _make_item(item_id=2, source_id="vid2")]
    engram_data = _make_engram_response()
    get_resp = _make_http_response(engram_data)
    patch_resp = _make_http_response({})
    selected = []  # no frames → complete without PATCH

    with patch.object(storage, "get_items_pending_visual_context", return_value=items):
        with patch.object(storage, "update_visual_context_status") as mock_status:
            with patch("curator.glimpse_client.select_frames", new_callable=AsyncMock,
                       return_value=selected):
                with patch("httpx.AsyncClient") as mock_client_cls:
                    mock_client = AsyncMock()
                    mock_client.get = AsyncMock(return_value=get_resp)
                    mock_client.patch = AsyncMock(return_value=patch_resp)
                    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                    await daemon._enrich_visual_context()

            # 2 items × 2 status updates = 4 calls
            assert mock_status.call_count == 4
            item_ids = {c.args[0] for c in mock_status.call_args_list}
            assert item_ids == {1, 2}
