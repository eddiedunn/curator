"""Thin HTTP client for the Glimpse visual context service."""
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class SelectedFrame:
    timestamp_sec: float
    score: int
    signals: list[str] = field(default_factory=list)


def _naive_interval_timestamps(
    duration_seconds: float, interval_seconds: int, max_frames: int
) -> list[SelectedFrame]:
    if duration_seconds <= 15:
        return []
    start, end = 10.0, duration_seconds * 0.95
    out: list[SelectedFrame] = []
    t = start
    while t <= end and len(out) < max_frames:
        out.append(SelectedFrame(timestamp_sec=t, score=1, signals=["interval_fallback"]))
        t += interval_seconds
    return out


async def select_frames(
    video_id: str,
    duration_seconds: float,
    segment_timestamps: list[float],
    glimpse_url: str,
    max_frames: int,
    scene_threshold: float,
    proximity_seconds: float,
    timeout_seconds: float,
    fallback_interval_seconds: int,
) -> list[SelectedFrame]:
    """Request frame selection from Glimpse, falling back to interval sampling on failure."""
    payload = {
        "video_id": video_id,
        "duration_seconds": duration_seconds,
        "segment_timestamps": segment_timestamps,
        "max_frames": max_frames,
        "scene_threshold": scene_threshold,
        "proximity_seconds": proximity_seconds,
    }
    timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=10.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{glimpse_url}/v1/select-frames", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("glimpse_select_frames_failed", video_id=video_id, error=str(exc))
        return _naive_interval_timestamps(duration_seconds, fallback_interval_seconds, max_frames)

    if data.get("error") and not data.get("selected_timestamps"):
        logger.warning(
            "glimpse_select_frames_error_payload",
            video_id=video_id,
            error=data.get("error"),
        )
        return _naive_interval_timestamps(duration_seconds, fallback_interval_seconds, max_frames)

    return [
        SelectedFrame(
            timestamp_sec=float(f["timestamp_sec"]),
            score=int(f["score"]),
            signals=list(f.get("signals") or []),
        )
        for f in data.get("selected_timestamps") or []
    ]


async def collect_visual_context(
    video_id: str,
    selected: list[SelectedFrame],
    glimpse_url: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Call /v1/glimpse for each selected frame, collecting captions and OCR."""
    results: list[dict[str, Any]] = []
    if not selected:
        return results
    timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for f in selected:
            try:
                r = await client.post(
                    f"{glimpse_url}/v1/glimpse",
                    json={"video_id": video_id, "timestamp_sec": f.timestamp_sec},
                )
                r.raise_for_status()
                data = r.json()
                results.append({
                    "timestamp_sec": f.timestamp_sec,
                    "score": f.score,
                    "signals": f.signals,
                    "caption": data.get("caption", "") or "",
                    "ocr_text": data.get("ocr_text", "") or "",
                    "entity_types": data.get("entity_types", []) or [],
                })
            except Exception as exc:
                logger.warning(
                    "glimpse_frame_failed",
                    video_id=video_id,
                    timestamp_sec=f.timestamp_sec,
                    error=str(exc),
                )
                continue
    return results
