"""Podcast plugin for Curator.

This plugin provides content ingestion from podcast RSS feeds.
It handles:
- Podcast RSS feed parsing with iTunes namespace support
- Audio episode download
- Transcription via Transcribe service
"""

import asyncio
import hashlib
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from urllib.parse import urlparse

import feedparser
import httpx

from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate,
)

logger = logging.getLogger(__name__)


class PodcastPlugin(IngestionPlugin):
    """Plugin for ingesting podcast episodes.

    This plugin fetches and parses podcast RSS feeds (with iTunes namespace
    support), downloads episode audio files, and marks them for transcription
    via the Transcribe service.

    Supports:
    - Podcast RSS feeds with iTunes extensions
    - MP3, M4A, and other common audio formats
    - Episode metadata extraction
    - Audio download to temporary files for transcription
    """

    def __init__(self, user_agent: Optional[str] = None):
        """Initialize Podcast plugin.

        Args:
            user_agent: Optional custom user agent for HTTP requests
        """
        self.user_agent = user_agent or "Curator-Podcast/1.0"

    @property
    def source_type(self) -> str:
        """Return unique source type identifier."""
        return "podcast"

    @property
    def name(self) -> str:
        """Return human-readable plugin name."""
        return "Podcast Episodes"

    def validate_url(self, url: str) -> bool:
        """Check if URL is valid.

        Args:
            url: URL to validate

        Returns:
            True if URL appears to be valid HTTP/HTTPS URL
        """
        try:
            result = urlparse(url)
            return result.scheme in ('http', 'https') and bool(result.netloc)
        except Exception:
            return False

    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata about podcast episode.

        Handles both:
        - Direct audio file URLs
        - Podcast feed URLs (returns metadata about the feed/latest episode)

        Args:
            source_url: URL to podcast episode or feed

        Returns:
            ContentMetadata with episode information, or None if error
        """
        try:
            # Check if this is a direct audio file URL
            if self._is_audio_url(source_url):
                return await self._fetch_audio_metadata(source_url)

            # Otherwise, try to parse as podcast feed
            return await self._fetch_feed_metadata(source_url)

        except Exception as e:
            logger.error(f"Error fetching metadata for {source_url}: {e}")
            return None

    async def _is_audio_url(self, url: str) -> bool:
        """Check if URL points to an audio file.

        Args:
            url: URL to check

        Returns:
            True if URL appears to be an audio file
        """
        audio_extensions = ('.mp3', '.m4a', '.wav', '.ogg', '.aac', '.flac', '.opus')
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(ext) for ext in audio_extensions)

    async def _fetch_audio_metadata(self, audio_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata for a direct audio URL.

        Args:
            audio_url: Direct URL to audio file

        Returns:
            ContentMetadata for the audio file
        """
        # Generate content ID from URL
        content_id = self._generate_content_id(audio_url)

        # Extract filename for title
        parsed = urlparse(audio_url)
        filename = Path(parsed.path).name or "Unknown Episode"

        # Try to fetch headers to get file size and content type
        duration_seconds = None
        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(
                    audio_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=10.0,
                    follow_redirects=True,
                )
                content_length = response.headers.get('content-length')
                if content_length:
                    # Rough estimate: 1MB per minute for typical podcast MP3
                    size_mb = int(content_length) / (1024 * 1024)
                    duration_seconds = int(size_mb * 60)
        except Exception as e:
            logger.warning(f"Could not fetch audio file headers: {e}")

        return ContentMetadata(
            content_id=content_id,
            title=filename,
            url=audio_url,
            description=None,
            author=None,
            published_at=None,
            duration_seconds=duration_seconds,
            extra={
                'audio_url': audio_url,
                'is_feed': False,
            }
        )

    async def _fetch_feed_metadata(self, feed_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata for a podcast feed URL.

        Args:
            feed_url: URL to podcast RSS feed

        Returns:
            ContentMetadata for the podcast feed/latest episode
        """
        try:
            # Fetch the feed
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    feed_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=30.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                feed_content = response.text

            # Parse feed with feedparser (handles iTunes namespace automatically)
            feed = await asyncio.to_thread(feedparser.parse, feed_content)

            if not feed.entries:
                logger.error(f"No episodes found in feed: {feed_url}")
                return None

            # Get the latest episode
            latest_entry = feed.entries[0]

            # Extract audio URL from enclosures
            audio_url = None
            duration_seconds = None

            if hasattr(latest_entry, 'enclosures') and latest_entry.enclosures:
                for enclosure in latest_entry.enclosures:
                    # Look for audio/* MIME types
                    if enclosure.get('type', '').startswith('audio/'):
                        audio_url = enclosure.get('href') or enclosure.get('url')
                        # Try to get duration from iTunes tags
                        if hasattr(latest_entry, 'itunes_duration'):
                            duration_seconds = self._parse_duration(latest_entry.itunes_duration)
                        break

            if not audio_url:
                logger.error(f"No audio URL found in latest episode of feed: {feed_url}")
                return None

            # Generate content ID from episode URL or GUID
            episode_id = latest_entry.get('id') or latest_entry.get('link') or audio_url
            content_id = self._generate_content_id(episode_id)

            # Extract metadata
            title = latest_entry.get('title', 'Unknown Episode')
            description = latest_entry.get('summary') or latest_entry.get('description')
            author = latest_entry.get('author') or feed.feed.get('author')

            # iTunes-specific author
            if hasattr(latest_entry, 'itunes_author'):
                author = latest_entry.itunes_author
            elif hasattr(feed.feed, 'itunes_author'):
                author = feed.feed.itunes_author

            # Parse published date
            published_at = None
            if hasattr(latest_entry, 'published_parsed') and latest_entry.published_parsed:
                published_at = datetime(*latest_entry.published_parsed[:6]).isoformat()
            elif hasattr(latest_entry, 'updated_parsed') and latest_entry.updated_parsed:
                published_at = datetime(*latest_entry.updated_parsed[:6]).isoformat()

            return ContentMetadata(
                content_id=content_id,
                title=title,
                url=latest_entry.get('link') or audio_url,
                description=description,
                author=author,
                published_at=published_at,
                duration_seconds=duration_seconds,
                extra={
                    'audio_url': audio_url,
                    'feed_url': feed_url,
                    'podcast_title': feed.feed.get('title'),
                    'episode_url': latest_entry.get('link'),
                    'is_feed': True,
                }
            )

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching feed {feed_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing feed {feed_url}: {e}")
            return None

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        """Fetch the full content for a podcast episode.

        Downloads the audio file to a temporary location and marks it
        for transcription via the Transcribe service.

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            ContentResult with audio file path and needs_transcription=True
        """
        audio_url = metadata.extra.get('audio_url')
        if not audio_url:
            logger.error(f"No audio URL in metadata for {metadata.content_id}")
            return None

        try:
            # Download audio to temp file
            audio_path = await self._download_audio(metadata.content_id, audio_url)

            if not audio_path:
                logger.error(f"Failed to download audio from {audio_url}")
                return None

            # Return with needs_transcription=True
            # The orchestrator will call the Transcribe service
            return ContentResult(
                text=str(audio_path),  # Path to audio file
                segments=None,
                source="podcast_audio",
                needs_transcription=True,
            )

        except Exception as e:
            logger.error(f"Error fetching content from {audio_url}: {e}")
            return None

    async def _download_audio(self, content_id: str, audio_url: str) -> Optional[Path]:
        """Download audio file to temporary location.

        Args:
            content_id: Content ID for naming the file
            audio_url: URL to audio file

        Returns:
            Path to downloaded audio file, or None on error
        """
        try:
            # Create temp directory for podcast audio
            temp_dir = Path(tempfile.gettempdir()) / "curator_podcast"
            temp_dir.mkdir(exist_ok=True)

            # Determine file extension from URL
            parsed = urlparse(audio_url)
            path_lower = parsed.path.lower()
            extension = '.mp3'  # default
            for ext in ['.mp3', '.m4a', '.wav', '.ogg', '.aac', '.flac', '.opus']:
                if path_lower.endswith(ext):
                    extension = ext
                    break

            audio_path = temp_dir / f"{content_id}{extension}"

            # Download the audio file
            logger.info(f"Downloading podcast audio from {audio_url}")
            async with httpx.AsyncClient() as client:
                # Stream download for large files
                async with client.stream(
                    'GET',
                    audio_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0),
                    follow_redirects=True,
                ) as response:
                    response.raise_for_status()

                    # Write to file
                    with open(audio_path, 'wb') as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)

            logger.info(f"Downloaded podcast audio to {audio_path}")
            return audio_path

        except httpx.HTTPError as e:
            logger.error(f"HTTP error downloading audio from {audio_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error downloading audio from {audio_url}: {e}")
            return None

    def _generate_content_id(self, identifier: str) -> str:
        """Generate a stable content ID from identifier.

        Args:
            identifier: Episode ID, URL, or GUID

        Returns:
            Hash-based content ID
        """
        # Use SHA256 hash as content ID
        return hashlib.sha256(identifier.encode()).hexdigest()[:16]

    def _parse_duration(self, duration_str: str) -> Optional[int]:
        """Parse iTunes duration to seconds.

        iTunes duration can be:
        - Seconds: "3600"
        - HH:MM:SS: "1:00:00"
        - MM:SS: "45:30"

        Args:
            duration_str: Duration string from iTunes tag

        Returns:
            Duration in seconds, or None if parsing fails
        """
        if not duration_str:
            return None

        try:
            # Try parsing as integer (seconds)
            return int(duration_str)
        except ValueError:
            pass

        try:
            # Try parsing as HH:MM:SS or MM:SS
            parts = duration_str.split(':')
            if len(parts) == 3:
                hours, minutes, seconds = parts
                return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            elif len(parts) == 2:
                minutes, seconds = parts
                return int(minutes) * 60 + int(seconds)
        except (ValueError, AttributeError):
            pass

        logger.warning(f"Could not parse duration: {duration_str}")
        return None

    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        """Estimate cost for podcast episode ingestion.

        Costs:
        - HTTP fetch: $0 (free)
        - Audio download: $0 (free)
        - Transcription: $0 (local mlx-whisper)
        - Embeddings: $0 (local BAAI model)

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            CostEstimate with cost breakdown
        """
        duration_seconds = metadata.duration_seconds or 3600  # Default to 1 hour
        duration_minutes = duration_seconds / 60

        # Estimate transcript length
        # Average speaking rate: ~150 words/minute
        estimated_words = int(duration_minutes * 150)
        estimated_tokens = int(estimated_words * 1.3)

        # Estimate chunks
        chunk_size = 500  # tokens
        estimated_chunks = max(1, estimated_tokens // chunk_size)

        warnings = []

        # Warn for very long episodes
        if duration_minutes > 180:
            warnings.append(f"Long episode ({duration_minutes:.0f} min) - transcription may take a while")

        if duration_minutes > 360:
            warnings.append("Very long episode (>6 hours) - consider splitting")

        return CostEstimate(
            api_calls=1,  # One HTTP fetch
            api_cost_usd=0.0,  # Free
            transcription_minutes=duration_minutes,
            transcription_cost_usd=0.0,  # Local Whisper is free
            embedding_tokens=estimated_tokens,
            embedding_cost_usd=0.0,  # Local embeddings are free
            total_cost_usd=0.0,  # Everything is local/free
            warnings=warnings if warnings else None,
        )


def list_podcast_episodes(feed_url: str, max_episodes: int = 10) -> List[dict]:
    """Utility function to list episodes from a podcast feed.

    This is a helper function for discovering episodes in a podcast feed.
    Not part of the plugin interface, but useful for podcast subscriptions.

    Args:
        feed_url: URL to podcast RSS feed
        max_episodes: Maximum number of episodes to return

    Returns:
        List of episode dictionaries with title, audio_url, published, etc.
    """
    try:
        feed = feedparser.parse(feed_url)

        episodes = []
        for entry in feed.entries[:max_episodes]:
            # Extract audio URL
            audio_url = None
            if hasattr(entry, 'enclosures') and entry.enclosures:
                for enclosure in entry.enclosures:
                    if enclosure.get('type', '').startswith('audio/'):
                        audio_url = enclosure.get('href') or enclosure.get('url')
                        break

            # Parse duration
            duration_seconds = None
            if hasattr(entry, 'itunes_duration'):
                duration_seconds = PodcastPlugin()._parse_duration(entry.itunes_duration)

            published = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6]).isoformat()

            episodes.append({
                'title': entry.get('title', 'No title'),
                'audio_url': audio_url,
                'link': entry.get('link'),
                'published': published,
                'summary': entry.get('summary'),
                'author': getattr(entry, 'itunes_author', entry.get('author')),
                'duration_seconds': duration_seconds,
            })

        return episodes
    except Exception as e:
        logger.error(f"Error parsing podcast feed {feed_url}: {e}")
        return []
