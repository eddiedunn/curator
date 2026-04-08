"""Tests for the Glimpse HTTP client."""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from curator.glimpse_client import (
    SelectedFrame,
    _naive_interval_timestamps,
    select_frames,
    collect_visual_context,
)


# ---------------------------------------------------------------------------
# _naive_interval_timestamps
# ---------------------------------------------------------------------------

def test_naive_interval_short_video():
    """Videos <= 15s return empty list."""
    result = _naive_interval_timestamps(10.0, 60, 5)
    assert result == []


def test_naive_interval_normal_video():
    """Returns interval-sampled frames with interval_fallback signal."""
    result = _naive_interval_timestamps(300.0, 60, 5)
    assert len(result) == 5
    assert all(f.signals == ["interval_fallback"] for f in result)
    assert all(f.score == 1 for f in result)
    assert result[0].timestamp_sec == pytest.approx(10.0)


def test_naive_interval_respects_max_frames():
    result = _naive_interval_timestamps(600.0, 60, 3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# select_frames
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_frames_happy_path():
    """Returns SelectedFrame list from valid Glimpse response."""
    response_data = {
        "video_id": "abc123",
        "selected_timestamps": [
            {"timestamp_sec": 42.0, "score": 2, "signals": ["segment", "scene_change"]},
            {"timestamp_sec": 120.0, "score": 1, "signals": ["segment"]},
        ],
        "scene_change_count": 3,
        "latency_ms": 500,
        "error": None,
    }

    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await select_frames(
            video_id="abc123",
            duration_seconds=300.0,
            segment_timestamps=[40.0, 120.0],
            glimpse_url="http://glimpse:8730",
            max_frames=5,
            scene_threshold=0.3,
            proximity_seconds=5.0,
            timeout_seconds=180.0,
            fallback_interval_seconds=60,
        )

    assert len(result) == 2
    assert result[0].timestamp_sec == pytest.approx(42.0)
    assert result[0].score == 2
    assert "segment" in result[0].signals
    assert result[1].timestamp_sec == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_select_frames_http_failure_fallback():
    """Falls back to naive interval timestamps on HTTP error."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await select_frames(
            video_id="abc123",
            duration_seconds=300.0,
            segment_timestamps=[],
            glimpse_url="http://glimpse:8730",
            max_frames=5,
            scene_threshold=0.3,
            proximity_seconds=5.0,
            timeout_seconds=180.0,
            fallback_interval_seconds=60,
        )

    assert len(result) > 0
    assert all(f.signals == ["interval_fallback"] for f in result)


@pytest.mark.asyncio
async def test_select_frames_error_payload_fallback():
    """Falls back to naive interval timestamps when error present and no timestamps."""
    response_data = {
        "video_id": "abc123",
        "selected_timestamps": [],
        "scene_change_count": 0,
        "latency_ms": 100,
        "error": "stream_url_failed: yt-dlp returned rc=1",
    }

    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await select_frames(
            video_id="abc123",
            duration_seconds=300.0,
            segment_timestamps=[],
            glimpse_url="http://glimpse:8730",
            max_frames=5,
            scene_threshold=0.3,
            proximity_seconds=5.0,
            timeout_seconds=180.0,
            fallback_interval_seconds=60,
        )

    assert len(result) > 0
    assert all(f.signals == ["interval_fallback"] for f in result)


@pytest.mark.asyncio
async def test_select_frames_partial_error_returns_timestamps():
    """scene_detection_unavailable error with timestamps returns those timestamps (not fallback)."""
    response_data = {
        "video_id": "abc123",
        "selected_timestamps": [
            {"timestamp_sec": 60.0, "score": 1, "signals": ["segment"]},
        ],
        "scene_change_count": 0,
        "latency_ms": 200,
        "error": "scene_detection_unavailable",
    }

    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await select_frames(
            video_id="abc123",
            duration_seconds=300.0,
            segment_timestamps=[60.0],
            glimpse_url="http://glimpse:8730",
            max_frames=5,
            scene_threshold=0.3,
            proximity_seconds=5.0,
            timeout_seconds=180.0,
            fallback_interval_seconds=60,
        )

    # Has timestamps AND error — should return those timestamps (not fallback)
    assert len(result) == 1
    assert result[0].timestamp_sec == pytest.approx(60.0)
    assert result[0].signals == ["segment"]


# ---------------------------------------------------------------------------
# collect_visual_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_visual_context_empty_input():
    """Empty selected list returns empty results without making HTTP calls."""
    result = await collect_visual_context(
        video_id="abc123",
        selected=[],
        glimpse_url="http://glimpse:8730",
        timeout_seconds=60.0,
    )
    assert result == []


@pytest.mark.asyncio
async def test_collect_visual_context_happy_path():
    """Returns frame data for each successfully processed frame."""
    frames = [
        SelectedFrame(timestamp_sec=42.0, score=2, signals=["segment", "scene_change"]),
        SelectedFrame(timestamp_sec=120.0, score=1, signals=["segment"]),
    ]

    vlm_responses = [
        {"caption": "A circuit board", "ocr_text": "STM32", "entity_types": ["electronics"]},
        {"caption": "Person speaking", "ocr_text": "", "entity_types": ["person"]},
    ]

    call_count = 0

    async def fake_post(url, **kwargs):
        nonlocal call_count
        resp = MagicMock()
        resp.json.return_value = vlm_responses[call_count]
        resp.raise_for_status = MagicMock()
        call_count += 1
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await collect_visual_context(
            video_id="abc123",
            selected=frames,
            glimpse_url="http://glimpse:8730",
            timeout_seconds=60.0,
        )

    assert len(result) == 2
    assert result[0]["caption"] == "A circuit board"
    assert result[0]["ocr_text"] == "STM32"
    assert result[0]["timestamp_sec"] == pytest.approx(42.0)
    assert result[0]["score"] == 2
    assert result[1]["caption"] == "Person speaking"


@pytest.mark.asyncio
async def test_collect_visual_context_partial_failures():
    """Frame HTTP failures are skipped; successful frames are still returned."""
    frames = [
        SelectedFrame(timestamp_sec=42.0, score=2, signals=["segment", "scene_change"]),
        SelectedFrame(timestamp_sec=120.0, score=1, signals=["segment"]),
        SelectedFrame(timestamp_sec=200.0, score=1, signals=["scene_change"]),
    ]

    call_count = 0

    async def fake_post(url, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        if idx == 1:
            raise httpx.ReadTimeout("timed out")
        resp = MagicMock()
        resp.json.return_value = {
            "caption": f"Frame at index {idx}",
            "ocr_text": "",
            "entity_types": [],
        }
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await collect_visual_context(
            video_id="abc123",
            selected=frames,
            glimpse_url="http://glimpse:8730",
            timeout_seconds=60.0,
        )

    # Middle frame failed, first and third succeed
    assert len(result) == 2
    assert result[0]["timestamp_sec"] == pytest.approx(42.0)
    assert result[1]["timestamp_sec"] == pytest.approx(200.0)
