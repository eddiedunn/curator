import httpx
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from curator.plugins.base import IngestionPlugin, ContentMetadata
from curator.plugins.youtube import YouTubePlugin
from curator.plugins.youtube_utils import is_youtube_url
from curator.chunking import chunk_by_semantic, chunk_with_timestamps

if TYPE_CHECKING:
    from curator.storage import CuratorStorage
    from curator.config import CuratorSettings

logger = structlog.get_logger()


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
        job_id: Optional[str] = None
    ) -> bool:
        """Ingest content from URL (auto-detects plugin).

        Args:
            url: URL to ingest
            subscription_id: Optional subscription ID to associate with
            job_id: Optional job ID for status tracking

        Returns:
            True on success, False on failure
        """
        item_id: int | None = None
        try:
            # Update job status to processing if job_id provided
            if job_id:
                self.storage.update_fetch_job(job_id, status="processing")

            # Detect content type and create plugin
            plugin = self._get_plugin_for_url(url)
            if not plugin:
                raise ValueError(f"Unsupported URL type: {url}")

            logger.info("Starting ingestion", url=url, plugin=plugin.source_type)

            # Call the main ingest method (returns metadata and content_id)
            metadata, content_id = await self.ingest(url, plugin)

            # Normalize source_type to lowercase before storing
            source_type = plugin.source_type.lower()

            # Create ingested_item record with full metadata (always create, subscription is optional)
            item_id = self.storage.create_ingested_item(
                source_type=source_type,
                source_id=content_id,
                source_url=url,
                title=metadata.title,
                author=metadata.author,
                published_at=metadata.published_at,
                subscription_id=subscription_id,
                metadata={"duration_seconds": metadata.duration_seconds}
            )

            # Update ingested item status to completed (item_id is None if duplicate — already completed)
            if item_id is not None:
                self.storage.update_ingested_item(item_id, status="completed")

            # Update job status to completed if job_id provided
            if job_id:
                self.storage.update_fetch_job(
                    job_id,
                    status="completed",
                    content_id=content_id
                )

            logger.info("Ingestion completed", content_id=content_id, url=url)
            return True

        except Exception as e:
            error_msg = str(e)
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

    async def ingest(self, url: str, plugin: IngestionPlugin) -> tuple[ContentMetadata, str]:
        """Ingest content from URL using plugin.

        Returns (metadata, content_id) on success.
        """
        # 1. Fetch metadata
        metadata = await plugin.fetch_metadata(url)
        if not metadata:
            raise ValueError(f"Failed to fetch metadata for {url}")

        # 2. Check duplicate in Engram
        async with httpx.AsyncClient(base_url=self._engram_url) as client:
            response = await client.get(f"/api/v1/content/{metadata.content_id}")
            if response.status_code == 200:
                logger.info("Content already exists", content_id=metadata.content_id)
                return metadata, metadata.content_id

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

        return metadata, metadata.content_id

    async def _transcribe(self, audio_path: Path) -> dict:
        """Call Transcribe service (async with long timeout)."""
        async with httpx.AsyncClient(timeout=self._transcribe_timeout) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    f"{self._transcribe_url}/v1/transcribe",
                    files={"audio": (audio_path.name, f)},
                    data={"cleanup": "true", "include_embeddings": "true", "identify_speakers": "true", "auto_enroll_speakers": "true"}
                )
            response.raise_for_status()
            return response.json()
