"""Base plugin interface for content ingestion system.

This module provides the abstract base class and data structures that all
ingestion plugins must implement. Each plugin handles a specific content
source (e.g., YouTube, blog posts, PDFs) and provides methods to:
- Fetch metadata about content
- Download/extract the actual content
- Estimate costs before processing
- Chunk content for embedding

Example:
    To create a new plugin, subclass IngestionPlugin and implement
    all abstract methods:

    ```python
    class MyPlugin(IngestionPlugin):
        @property
        def source_type(self) -> str:
            return "my_source"

        @property
        def name(self) -> str:
            return "My Content Source"

        async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
            # Extract content ID and metadata
            return ContentMetadata(
                content_id="123",
                title="Example",
                url=source_url,
            )

        async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
            # Download/extract content
            return ContentResult(
                text="Content text here...",
                source="api",
            )

        def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
            # Calculate expected costs
            return CostEstimate(
                api_calls=1,
                api_cost_usd=0.01,
                total_cost_usd=0.01,
            )
    ```
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class ContentMetadata:
    """Metadata about a piece of content, returned by fetch_metadata.

    This contains identifying information and metadata about content
    without downloading the full content. Used for:
    - Cost estimation before processing
    - Checking if content already exists in database
    - Displaying content info to users

    Attributes:
        content_id: Unique identifier for the content within its source
            (e.g., YouTube video ID, blog post slug)
        title: Content title or headline
        url: Full URL to the content
        description: Optional content description or summary
        author: Optional author/creator name
        published_at: Optional publication date (ISO 8601 format recommended)
        duration_seconds: Optional duration for time-based content (videos, audio)
        extra: Dictionary for source-specific metadata that doesn't fit
            standard fields (e.g., view_count, tags, thumbnail_url)
    """
    content_id: str
    title: str
    url: str
    description: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[str] = None
    duration_seconds: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentResult:
    """Content data returned by fetch_content.

    Contains the actual content text or a reference to audio that needs
    transcription. Includes segmentation information for timestamp-aware
    content like video transcripts.

    Attributes:
        text: The full text content. For transcripts, this is the complete
            transcript text. If needs_transcription=True, this should be
            the path to an audio file.
        segments: Optional list of timestamped segments. Each segment is a dict with:
            - 'start': Start time in seconds (float)
            - 'end': End time in seconds (float)
            - 'text': Text for this segment (str)
            Used for timestamp-aware chunking of transcripts.
        source: How the content was obtained. Examples:
            - "youtube_subtitles": From YouTube's subtitle API
            - "api": From a content API
            - "web_scrape": Scraped from web page
            - "audio_download": Audio file that needs transcription
        needs_transcription: If True, the text field contains a path to an
            audio file that needs to be transcribed using Whisper or similar.
            The transcription step will replace text with the transcript.
    """
    text: str
    segments: Optional[List[Dict]] = None
    source: str = "unknown"
    needs_transcription: bool = False


@dataclass
class CostEstimate:
    """Cost estimation for ingesting a piece of content.

    Used to calculate expected costs before processing, allowing users to
    approve or reject expensive operations. All costs in USD.

    Attributes:
        api_calls: Number of API calls required (for rate limiting)
        api_cost_usd: Cost of API calls (e.g., YouTube API quota costs)
        transcription_minutes: Minutes of audio to transcribe
        transcription_cost_usd: Cost of transcription (e.g., Whisper API)
        embedding_tokens: Estimated tokens to embed
        embedding_cost_usd: Cost of generating embeddings
        total_cost_usd: Sum of all costs
        warnings: List of warning messages (e.g., "Video is very long",
            "No subtitles available, will need transcription")
    """
    api_calls: int = 0
    api_cost_usd: float = 0.0
    transcription_minutes: float = 0.0
    transcription_cost_usd: float = 0.0
    embedding_tokens: int = 0
    embedding_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    warnings: Optional[List[str]] = None


class IngestionPlugin(ABC):
    """Abstract base class for content ingestion plugins.

    Each plugin handles a specific content source (YouTube, blog, PDF, etc.)
    and implements methods to fetch metadata, download content, estimate costs,
    and optionally chunk content for embedding.

    Plugins should handle errors gracefully and return None for methods that
    fail (with appropriate logging). The ingestion system will handle retries
    and error reporting.

    Subclasses must implement all abstract methods and properties:
    - source_type: Unique identifier for this source
    - name: Human-readable name
    - fetch_metadata: Extract metadata from a URL
    - fetch_content: Download/extract the actual content
    - estimate_cost: Calculate expected processing costs

    Subclasses may override:
    - chunk_content: Custom chunking logic (default uses curator.chunking)
    - validate_url: Check if URL is valid for this plugin
    """

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Return unique source type identifier.

        This is used to identify which plugin to use for a given URL and
        to store in the database for tracking content sources.

        Returns:
            Source type string (e.g., 'youtube', 'blog', 'pdf')

        Example:
            ```python
            @property
            def source_type(self) -> str:
                return "youtube"
            ```
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Return human-readable plugin name.

        Used for displaying in UI and logs.

        Returns:
            Human-readable name (e.g., 'YouTube Videos', 'Blog Posts')

        Example:
            ```python
            @property
            def name(self) -> str:
                return "YouTube Videos"
            ```
        """
        ...

    @abstractmethod
    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata about content without downloading full content.

        This should be a lightweight operation that extracts identifying
        information and metadata. Used for:
        - Cost estimation before downloading
        - Checking if content already exists
        - Displaying content info to users

        Error Handling:
            - Return None if URL is invalid or content not found
            - Return None on API errors (log the error)
            - Don't raise exceptions unless truly exceptional

        Args:
            source_url: URL to the content (e.g., YouTube video URL,
                blog post URL)

        Returns:
            ContentMetadata if successful, None if not found or error

        Example:
            ```python
            async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
                video_id = self._extract_video_id(source_url)
                if not video_id:
                    return None

                try:
                    info = await self.api.get_video_info(video_id)
                    return ContentMetadata(
                        content_id=video_id,
                        title=info['title'],
                        url=source_url,
                        description=info.get('description'),
                        author=info.get('channel_name'),
                        published_at=info.get('upload_date'),
                        duration_seconds=info.get('duration'),
                        extra={'view_count': info.get('view_count')},
                    )
                except APIError as e:
                    logger.error(f"Failed to fetch metadata: {e}")
                    return None
            ```
        """
        ...

    @abstractmethod
    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        """Fetch the full content (text, transcript, or audio for transcription).

        This downloads or extracts the actual content. For videos:
        1. Try to get existing subtitles/transcripts first (fastest, cheapest)
        2. If unavailable, download audio and set needs_transcription=True

        Error Handling:
            - Return None on failures (log the error)
            - Don't raise exceptions unless truly exceptional

        Args:
            metadata: Metadata from fetch_metadata() call

        Returns:
            ContentResult with text content or audio file path, None on error

        Example:
            ```python
            async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
                video_id = metadata.content_id

                # Try subtitles first
                subtitles = await self._get_subtitles(video_id)
                if subtitles:
                    segments = [
                        {'start': s.start, 'end': s.end, 'text': s.text}
                        for s in subtitles
                    ]
                    full_text = ' '.join(s['text'] for s in segments)
                    return ContentResult(
                        text=full_text,
                        segments=segments,
                        source='youtube_subtitles',
                        needs_transcription=False,
                    )

                # Fall back to audio download
                audio_path = await self._download_audio(video_id)
                if audio_path:
                    return ContentResult(
                        text=audio_path,
                        source='audio_download',
                        needs_transcription=True,
                    )

                return None
            ```
        """
        ...

    @abstractmethod
    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        """Estimate the cost of processing this content.

        Used to show users expected costs before processing and implement
        approval gates for expensive operations.

        Calculate costs for:
        - API calls (if your source charges per call)
        - Transcription (if audio needs to be transcribed)
        - Embeddings (based on estimated content length)

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            CostEstimate with cost breakdown

        Example:
            ```python
            def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
                estimate = CostEstimate()

                # API cost
                estimate.api_calls = 1
                estimate.api_cost_usd = 0.001

                # Check if we have subtitles
                has_subtitles = self._check_subtitles(metadata.content_id)

                if not has_subtitles and metadata.duration_seconds:
                    # Will need transcription
                    minutes = metadata.duration_seconds / 60
                    estimate.transcription_minutes = minutes
                    estimate.transcription_cost_usd = minutes * 0.006  # Whisper pricing
                    estimate.warnings = ["No subtitles, will need transcription"]

                # Estimate embedding cost (rough)
                est_tokens = (metadata.duration_seconds or 0) * 3  # ~3 tokens/sec speech
                estimate.embedding_tokens = est_tokens
                estimate.embedding_cost_usd = (est_tokens / 1000) * 0.0001

                estimate.total_cost_usd = (
                    estimate.api_cost_usd +
                    estimate.transcription_cost_usd +
                    estimate.embedding_cost_usd
                )

                return estimate
            ```
        """
        ...

    def chunk_content(
        self,
        content: ContentResult,
        metadata: ContentMetadata,
        target_tokens: int = 500,
    ) -> List[Dict]:
        """Chunk content into pieces suitable for embedding.

        Default implementation uses curator.chunking utilities:
        - For content with segments (transcripts): timestamp-aware chunking
        - For plain text: semantic chunking

        Override this method for source-specific chunking logic (e.g., to
        preserve special structure or add custom metadata to chunks).

        Args:
            content: Content from fetch_content()
            metadata: Metadata from fetch_metadata()
            target_tokens: Target size for each chunk in tokens

        Returns:
            List of chunk dictionaries. Each dict should contain:
            - 'text': The chunk text
            - 'metadata': Dict with chunk metadata (timestamps, etc.)

        Example Override:
            ```python
            def chunk_content(self, content, metadata, target_tokens=500):
                chunks = []
                for segment in content.segments:
                    chunks.append({
                        'text': segment['text'],
                        'metadata': {
                            'start': segment['start'],
                            'end': segment['end'],
                            'video_id': metadata.content_id,
                            'video_title': metadata.title,
                        }
                    })
                return chunks
            ```
        """
        from curator.chunking import chunk_by_semantic, chunk_with_timestamps

        if content.segments:
            # Use timestamp-aware chunking for transcripts
            return chunk_with_timestamps(
                content.text,
                content.segments,
                target_tokens=target_tokens,
            )
        else:
            # Use semantic chunking for plain text
            return chunk_by_semantic(content.text, target_tokens=target_tokens)

    def validate_url(self, url: str) -> bool:
        """Check if a URL is valid for this plugin.

        Default implementation returns True (accepts all URLs). Override
        to validate URLs before attempting to fetch metadata.

        Args:
            url: URL to validate

        Returns:
            True if URL is valid for this plugin, False otherwise

        Example:
            ```python
            def validate_url(self, url: str) -> bool:
                import re
                patterns = [
                    r'youtube\.com/watch\?v=',
                    r'youtu\.be/',
                ]
                return any(re.search(p, url) for p in patterns)
            ```
        """
        return True
