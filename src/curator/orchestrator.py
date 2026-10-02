import time
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentNotYetAvailableError,
    ContentUnavailableError,
    RateLimitedError,
)
from curator.plugins.youtube import YouTubePlugin
from curator.plugins.youtube_utils import is_youtube_url
from curator.models import IngestionStatus
from curator.chunking import chunk_by_semantic, chunk_with_timestamps

if TYPE_CHECKING:
    from curator.storage import CuratorStorage
    from curator.config import CuratorSettings

logger = structlog.get_logger()


class ServiceBusyError(Exception):
    """Raised when the transcription service returns 503 (temporarily unavailable)."""
    pass


class YouTubeCooldown:
    """Pause on YouTube requests after a bot check / rate limit.

    Each hit in a row doubles the pause (up to max_minutes); a successful
    ingestion resets it. Hits while already paused don't extend the pause.
    """

    def __init__(self, base_minutes: float, max_minutes: float, clock=time.monotonic):
        self._base_minutes = base_minutes
        self._max_minutes = max_minutes
        self._next_minutes = base_minutes
        self._clock = clock
        self._until: float | None = None

    @property
    def active(self) -> bool:
        return self._until is not None and self._clock() < self._until

    @property
    def remaining_minutes(self) -> float:
        if not self.active:
            return 0.0
        return round((self._until - self._clock()) / 60, 1)

    def trip(self, reason: str) -> None:
        if self.active:
            return
        minutes = self._next_minutes
        self._until = self._clock() + minutes * 60
        self._next_minutes = min(minutes * 2, self._max_minutes)
        resume_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        logger.warning(
            "YouTube bot check / rate limit hit; pausing all YouTube requests",
            cooldown_minutes=minutes,
            resume_at=resume_at.isoformat(timespec="seconds"),
            reason=reason[:200],
        )

    def record_success(self) -> None:
        self._next_minutes = self._base_minutes


def _metadata_failure_message(url: str, plugin: IngestionPlugin) -> str:
    """'Failed to fetch metadata' plus the plugin's reason when it gave one."""
    reason = getattr(plugin, "last_error", None)
    if isinstance(reason, str) and reason:
        return f"Failed to fetch metadata for {url}: {reason}"
    return f"Failed to fetch metadata for {url}"


def _chunk_count(response: httpx.Response) -> Optional[int]:
    """chunk_count from an Engram content response (store or get), if present."""
    try:
        value = response.json().get("chunk_count")
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _is_dns_error(exc: BaseException) -> bool:
    """Return True for transient DNS resolution failures (Errno -2 / -3)."""
    return isinstance(exc, OSError) and exc.errno in (-2, -3)


def _format_diarized_text(segments: list[dict]) -> str:
    """Format transcription segments into speaker-labeled paragraphs.

    Groups consecutive same-speaker segments and prefixes each group
    with the speaker label. Falls back to plain concatenation when
    segments have no speaker field.
    """
    if not segments:
        return ""

    # Check if any segment has a non-None speaker
    has_speakers = any(seg.get("speaker") for seg in segments)
    if not has_speakers:
        return " ".join(seg.get("text", "").strip() for seg in segments).strip()

    paragraphs = []
    current_speaker = None
    current_texts: list[str] = []

    for seg in segments:
        speaker = seg.get("speaker")
        text = seg.get("text", "").strip()
        if not text:
            continue

        if speaker != current_speaker:
            if current_texts:
                label = f"{current_speaker}: " if current_speaker else ""
                paragraphs.append(f"{label}{' '.join(current_texts)}")
            current_speaker = speaker
            current_texts = [text]
        else:
            current_texts.append(text)

    # Flush last group
    if current_texts:
        label = f"{current_speaker}: " if current_speaker else ""
        paragraphs.append(f"{label}{' '.join(current_texts)}")

    return "\n\n".join(paragraphs)

class IngestionOrchestrator:
    """Synchronous orchestrator for content ingestion."""

    def __init__(self, storage: 'CuratorStorage', settings: 'CuratorSettings'):
        self.storage = storage
        self.settings = settings
        self._engram_url = settings.engram_api_url
        self._transcribe_url = settings.transcribe_service_url
        self.youtube_cooldown = YouTubeCooldown(
            settings.youtube_cooldown_minutes,
            settings.youtube_cooldown_max_minutes,
        )
        # Long timeout for transcription (1 hour default)
        self._transcribe_timeout = httpx.Timeout(
            connect=30.0,
            read=3600.0,
            write=60.0,
            pool=30.0
        )

    async def ingest_url(
        self,
        url: str,
        subscription_id: Optional[int] = None,
        job_id: Optional[str] = None,
        item_id: Optional[int] = None,
    ) -> bool:
        """Ingest content from URL (auto-detects plugin).

        Args:
            url: URL to ingest
            subscription_id: Optional subscription ID to associate with
            job_id: Optional job ID for status tracking
            item_id: Existing ingested_items row being retried, so failures
                before metadata is known are still recorded on it

        Returns:
            True on success, False on failure
        """
        try:
            # Update job status to processing if job_id provided
            if job_id:
                self.storage.update_fetch_job(job_id, status="processing")

            # Detect content type and create plugin
            plugin = self._get_plugin_for_url(url)
            if not plugin:
                raise ValueError(f"Unsupported URL type: {url}")

            logger.info("Starting ingestion", url=url, plugin=plugin.source_type)

            # Fetch metadata early so we can persist a record before the long ingest
            metadata = await plugin.fetch_metadata(url)
            if not metadata:
                raise ValueError(_metadata_failure_message(url, plugin))

            source_type = plugin.source_type.lower()

            # Create a pending record immediately so failures are tracked and not
            # silently rediscovered on every hourly scan.
            item_id = self.storage.create_ingested_item(
                source_type=source_type,
                source_id=metadata.content_id,
                source_url=url,
                title=metadata.title,
                author=metadata.author,
                published_at=metadata.published_at,
                subscription_id=subscription_id,
                metadata={"duration_seconds": metadata.duration_seconds}
            )
            # When a record already exists (IntegrityError → None), look it up so
            # we can still update its status on success or failure.
            if item_id is None:
                existing = self.storage.get_ingested_item_by_source(source_type, metadata.content_id)
                if existing:
                    item_id = existing["id"]

            # Call the main ingest method, passing pre-fetched metadata to avoid
            # a redundant fetch_metadata call inside ingest().
            _, content_id, chunk_count = await self.ingest(url, plugin, prefetched_metadata=metadata)

            # Mark completed and clear any error left by an earlier attempt
            if item_id is not None:
                fields = {"status": "completed", "error_message": None}
                if chunk_count is not None:
                    fields["chunk_count"] = chunk_count
                self.storage.update_ingested_item(item_id, **fields)

            # Update job status to completed if job_id provided
            if job_id:
                self.storage.update_fetch_job(
                    job_id,
                    status="completed",
                    content_id=content_id
                )

            self.youtube_cooldown.record_success()
            logger.info("Ingestion completed", content_id=content_id, url=url)
            return True

        except RateLimitedError as e:
            # Not the video's fault: never skip it. A row (if any) is left
            # failed so the daemon's retry picks it up after the cool-down.
            error_msg = (
                "Blocked by YouTube's bot check / rate limit; try again later. "
                f"yt-dlp: {e}"
            )
            self.youtube_cooldown.trip(str(e))
            if item_id is not None:
                self.storage.update_ingested_item(item_id, status="failed", error_message=error_msg)
            if job_id:
                self.storage.update_fetch_job(job_id, status="failed", error_message=error_msg)
            return False

        except ContentNotYetAvailableError as e:
            # Upcoming premiere / live event not started. No row is recorded,
            # so the next channel scan picks it up once it is out.
            logger.info("Video not yet available, will retry on a later scan", error=str(e), url=url)
            if job_id:
                self.storage.update_fetch_job(job_id, status="failed", error_message=str(e))
            return False

        except ContentUnavailableError as e:
            # Members-only / private / removed: record it as skipped so the
            # daemon's "already ingested" check stops re-attempting it.
            error_msg = str(e)
            logger.warning("Content permanently unavailable, skipping", error=error_msg, url=url)

            source_type = plugin.source_type.lower()
            item_id = self.storage.create_ingested_item(
                source_type=source_type,
                source_id=e.content_id,
                source_url=url,
                title=e.content_id,
                subscription_id=subscription_id,
            )
            if item_id is None:
                existing = self.storage.get_ingested_item_by_source(source_type, e.content_id)
                if existing:
                    item_id = existing["id"]
            if item_id is not None:
                self.storage.update_ingested_item(
                    item_id,
                    status=IngestionStatus.SKIPPED.value,
                    error_message=error_msg,
                )

            if job_id:
                self.storage.update_fetch_job(job_id, status="failed", error_message=error_msg)

            return False

        except ServiceBusyError as e:
            error_msg = str(e)
            logger.warning("Transcription service busy, will retry next scan", error=error_msg, url=url)

            # Leave item as pending so it is retried on the next scan
            if item_id is not None:
                self.storage.update_ingested_item(
                    item_id,
                    status="pending",
                    error_message=error_msg
                )

            if job_id:
                self.storage.update_fetch_job(
                    job_id,
                    status="failed",
                    error_message=error_msg
                )

            return False

        except Exception as e:
            error_msg = str(e)

            if _is_dns_error(e):
                logger.warning("Transient DNS failure, will retry next scan", error=error_msg, url=url)
                if item_id is not None:
                    self.storage.update_ingested_item(item_id, status="pending", error_message=error_msg)
                if job_id:
                    self.storage.update_fetch_job(job_id, status="failed", error_message=error_msg)
                return False

            logger.error("Ingestion failed", error=error_msg, url=url)

            # Update ingested item status to failed if it was created
            if item_id is not None:
                self.storage.update_ingested_item(
                    item_id,
                    status="failed",
                    error_message=error_msg
                )

            # Update job status to failed if job_id provided
            if job_id:
                self.storage.update_fetch_job(
                    job_id,
                    status="failed",
                    error_message=error_msg
                )

            return False

    def _get_plugin_for_url(self, url: str) -> Optional[IngestionPlugin]:
        """Detect content type and return appropriate plugin.

        Args:
            url: URL to check

        Returns:
            Plugin instance or None if unsupported
        """
        if is_youtube_url(url):
            return YouTubePlugin()

        # TODO: Add RSS and podcast plugin detection
        # elif is_rss_url(url):
        #     return RSSPlugin()
        # elif is_podcast_url(url):
        #     return PodcastPlugin()

        return None

    async def ingest(
        self,
        url: str,
        plugin: IngestionPlugin,
        prefetched_metadata: Optional[ContentMetadata] = None,
    ) -> tuple[ContentMetadata, str, Optional[int]]:
        """Ingest content from URL using plugin.

        Returns (metadata, content_id, chunk_count) on success; chunk_count is
        Engram's count, or None if Engram did not report one.
        """
        # 1. Fetch metadata (skip if already fetched by caller)
        if prefetched_metadata is not None:
            metadata = prefetched_metadata
        else:
            metadata = await plugin.fetch_metadata(url)
            if not metadata:
                raise ValueError(_metadata_failure_message(url, plugin))

        # 2. Check duplicate in Engram
        async with httpx.AsyncClient(base_url=self._engram_url) as client:
            response = await client.get(f"/api/v1/content/{metadata.content_id}")
            if response.status_code == 200:
                logger.info("Content already exists", content_id=metadata.content_id)
                return metadata, metadata.content_id, _chunk_count(response)

        # 3. Fetch content
        content = await plugin.fetch_content(metadata)
        if not content:
            raise ValueError(f"Failed to fetch content for {url}")

        # 4. Transcribe if needed (async call with long timeout)
        speakers = []
        if content.needs_transcription:
            # When needs_transcription=True, the audio file path is in content.text
            audio_path = Path(content.text)
            result = await self._transcribe(audio_path)
            content.text = _format_diarized_text(result["segments"])
            content.segments = result["segments"]
            speakers = result.get("speakers", [])

        # 5. Store in Engram
        engram_metadata = {
            "description": metadata.description,
            "author": metadata.author,
            "published_at": metadata.published_at,
            "duration_seconds": metadata.duration_seconds,
            "segments": content.segments,
        }
        if speakers:
            engram_metadata["speakers"] = speakers
            engram_metadata["speaker_count"] = len(speakers)

        async with httpx.AsyncClient(base_url=self._engram_url, timeout=60) as client:
            response = await client.post(
                "/api/v1/content",
                json={
                    "content_id": metadata.content_id,
                    "content_type": plugin.source_type.lower(),
                    "title": metadata.title,
                    "text": content.text,
                    "url": metadata.url,
                    "metadata": engram_metadata,
                }
            )
            response.raise_for_status()

        return metadata, metadata.content_id, _chunk_count(response)

    async def _transcribe(self, audio_path: Path) -> dict:
        """Call Transcribe service (async with long timeout)."""
        async with httpx.AsyncClient(timeout=self._transcribe_timeout) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    f"{self._transcribe_url}/v1/transcribe",
                    files={"audio": (audio_path.name, f)},
                    data={"cleanup": "true", "include_embeddings": "true", "identify_speakers": "true", "auto_enroll_speakers": "false"}
                )
            if response.status_code == 503:
                retry_after = int(response.headers.get("Retry-After", "60"))
                raise ServiceBusyError(f"Transcription service busy, retry after {retry_after}s")
            response.raise_for_status()
            return response.json()
