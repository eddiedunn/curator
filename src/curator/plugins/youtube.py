"""YouTube plugin for Curator.

This plugin provides content ingestion from YouTube videos using yt-dlp.
It handles:
- Video metadata extraction (title, channel, duration, publication date)
- Transcript/subtitle fetching
- Audio download for transcription fallback
"""

import asyncio
import json
import logging
import random
import re
import tempfile
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Optional, List, Dict, TypeVar, Callable, Any
import yt_dlp

from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate,
)
from curator.plugins.youtube_utils import (
    extract_video_id,
    extract_channel_id,
    is_youtube_url,
    build_video_url,
)

logger = logging.getLogger(__name__)

T = TypeVar('T')


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
):
    """
    Decorator for retry with exponential backoff.

    Args:
        max_attempts: Maximum retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        jitter: Add random jitter to delay
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)

                except yt_dlp.utils.DownloadError as e:
                    last_exception = e
                    error_msg = str(e).lower()

                    # Don't retry for certain errors
                    if 'video unavailable' in error_msg:
                        raise
                    if 'private video' in error_msg:
                        raise
                    if 'removed' in error_msg:
                        raise

                    if attempt == max_attempts:
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)

                    # Add jitter
                    if jitter:
                        delay += random.uniform(0, delay * 0.1)

                    logger.warning(
                        f"Attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)

                except Exception as e:
                    # For other exceptions, re-raise immediately
                    raise

            raise last_exception

        return wrapper
    return decorator


class YouTubePlugin(IngestionPlugin):
    """Plugin for ingesting YouTube video content.

    This plugin extracts metadata and content from YouTube videos using yt-dlp.
    It prefers existing subtitles for cost efficiency, but can fall back to
    audio transcription via Whisper if subtitles are unavailable.

    Supports:
    - YouTube video URLs in standard formats (youtube.com/watch, youtu.be)
    - Channel URLs for listing recent videos
    - Subtitle extraction with automatic retry on rate limiting
    - Audio download for transcription when subtitles unavailable
    """

    def __init__(self, cookies_path: Optional[str] = None):
        """
        Initialize YouTube plugin.

        Args:
            cookies_path: Optional path to cookies.txt for rate limiting bypass
        """
        self.cookies_path = cookies_path
        self._ydl_opts = self._build_ydl_opts()

    def _build_ydl_opts(self) -> dict:
        """Build yt-dlp options."""
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,  # Get full metadata
            'skip_download': True,   # Don't download video
        }
        if self.cookies_path:
            opts['cookiefile'] = self.cookies_path
        return opts

    @property
    def source_type(self) -> str:
        """Return unique source type identifier."""
        return "youtube"

    @property
    def name(self) -> str:
        """Return human-readable plugin name."""
        return "YouTube Videos"

    def validate_url(self, url: str) -> bool:
        """Check if URL is a valid YouTube video URL.

        Args:
            url: URL to validate

        Returns:
            True if URL appears to be a YouTube video URL, False otherwise
        """
        return is_youtube_url(url)

    # Valid YouTube video IDs are exactly 11 alphanumeric/hyphen/underscore chars.
    _VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')

    @staticmethod
    def _normalize_channel_url(channel_url: str) -> str:
        """Append /videos to a channel URL if it points at the channel root.

        Without /videos, yt-dlp's extract_flat='in_playlist' resolves the
        channel homepage and returns tab entries (Videos/Shorts/Live) whose
        'id' field is the 24-char channel ID, not individual video IDs.
        Pointing directly at the /videos tab forces yt-dlp to enumerate
        actual video entries.
        """
        url = channel_url.rstrip('/')
        # Already targeting a specific tab or playlist — leave as-is.
        if any(url.endswith(tab) for tab in ('/videos', '/shorts', '/live', '/streams', '/playlists')):
            return url
        if '/playlist' in url or '/watch' in url:
            return url
        return url + '/videos'

    @with_retry(max_attempts=3)
    async def fetch_channel_videos(
        self,
        channel_url: str,
        max_videos: int = 50,
    ) -> List[str]:
        """
        Fetch recent video IDs from a YouTube channel.

        Args:
            channel_url: YouTube channel URL (e.g., https://youtube.com/@username)
            max_videos: Maximum number of videos to fetch (default 50)

        Returns:
            List of video IDs (newest first)
        """
        channel_id = extract_channel_id(channel_url)
        if not channel_id:
            logger.error(f"Could not extract channel ID from: {channel_url}")
            return []

        # Normalise to the /videos tab so yt-dlp returns individual video IDs.
        # A bare @handle URL resolves to the channel homepage which, with
        # extract_flat='in_playlist', yields tab entries whose 'id' is the
        # 24-char channel ID rather than a video ID.
        videos_url = self._normalize_channel_url(channel_url)

        try:
            # Use yt-dlp with extract_flat to get video list without downloading
            ydl_opts = {
                **self._ydl_opts,
                'extract_flat': 'in_playlist',  # Just get list, not full metadata
                'playlistend': max_videos,  # Limit number of videos
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # For channels, yt-dlp can extract the uploads playlist
                info = ydl.extract_info(videos_url, download=False)

                if not info:
                    logger.warning(f"No channel info found for: {channel_url}")
                    return []

                # Get video IDs from entries, filtering out any non-video IDs.
                # Defensive guard: channel IDs (24-char "UC...") or other
                # non-video entries are rejected here even if the URL
                # normalisation above already prevents them in practice.
                video_ids = []
                entries = info.get('entries', [])

                for entry in entries:
                    if not entry:
                        continue
                    vid_id = entry.get('id', '')
                    if not self._VIDEO_ID_RE.match(vid_id):
                        logger.warning(
                            f"Skipping non-video ID from channel listing: {vid_id!r}",
                            channel_id=channel_id,
                        )
                        continue
                    video_ids.append(vid_id)

                    if len(video_ids) >= max_videos:
                        break

                logger.info(
                    f"Fetched {len(video_ids)} videos from channel",
                    channel_id=channel_id,
                )

                return video_ids

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp error fetching channel {channel_url}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching channel videos: {e}")
            return []

    @with_retry(max_attempts=3)
    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        """
        Fetch video metadata using yt-dlp.

        No API key required - yt-dlp scrapes the page.

        Args:
            source_url: YouTube video URL or video ID

        Returns:
            ContentMetadata with video information, or None if not found/error
        """
        video_id = extract_video_id(source_url)
        if not video_id:
            logger.error(f"Could not extract video ID from: {source_url}")
            return None

        try:
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(source_url, download=False)

            if not info:
                return None

            # Parse duration (yt-dlp returns seconds directly)
            duration = info.get('duration', 0)

            # Parse upload date
            upload_date = info.get('upload_date')  # Format: YYYYMMDD
            published_at = None
            if upload_date:
                published_at = datetime.strptime(upload_date, '%Y%m%d').isoformat()

            return ContentMetadata(
                content_id=video_id,
                title=info.get('title', 'Unknown'),
                url=f"https://youtube.com/watch?v={video_id}",
                description=info.get('description'),
                author=info.get('channel') or info.get('uploader'),
                published_at=published_at,
                duration_seconds=duration,
                extra={
                    'channel_id': info.get('channel_id'),
                    'channel_url': info.get('channel_url'),
                    'view_count': info.get('view_count'),
                    'like_count': info.get('like_count'),
                    'tags': info.get('tags', []),
                    'categories': info.get('categories', []),
                    'thumbnail': info.get('thumbnail'),
                }
            )

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp error for {source_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching metadata: {e}")
            return None

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        """Fetch the full content for a YouTube video.

        Uses subtitles if ANY exist (even sparse ones for music videos).
        Only falls back to Whisper if yt-dlp finds NO subtitles at all.

        Strategy:
        1. Try to get subtitles via yt-dlp (fast, no download)
        2. If ANY subtitles exist (even sparse/low-quality), use them
        3. Only use Whisper if NO subtitles are available

        This avoids expensive Whisper transcription for music videos with
        sparse captions - user prefers videos with proper transcripts anyway.

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            ContentResult with video transcript/audio, or None on error
        """
        video_id = metadata.content_id

        # Try subtitles first
        transcript, segments = await self._try_get_subtitles(video_id)

        # Use subtitles if ANY exist, regardless of quality/density
        # Check segments instead of transcript text to handle sparse subtitles
        if segments:
            return ContentResult(
                text=transcript or "",  # Use empty string if no text but segments exist
                segments=segments,
                source="youtube_subtitles",
                needs_transcription=False,
            )

        # No subtitles at all - need to download audio for Whisper
        logger.info(f"No subtitles found for {video_id}, will use Whisper")
        audio_path = await self._download_audio(video_id)

        if audio_path:
            return ContentResult(
                text=str(audio_path),  # Path to audio file
                segments=[],
                source="whisper_pending",
                needs_transcription=True,
            )

        return None

    @with_retry(max_attempts=3)
    async def _try_get_subtitles(
        self,
        video_id: str,
        languages: List[str] = None,
    ) -> tuple[Optional[str], List[Dict]]:
        """
        Try to get subtitles using yt-dlp.

        Args:
            video_id: YouTube video ID
            languages: Preferred languages in order (defaults to ['en', 'en-US', 'en-GB'])

        Returns:
            Tuple of (transcript_text, segments_with_timestamps)
        """
        if languages is None:
            languages = ['en', 'en-US', 'en-GB']

        url = f"https://youtube.com/watch?v={video_id}"

        # Use temp directory for subtitle download
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                **self._ydl_opts,
                'writesubtitles': True,
                'writeautomaticsub': True,  # Also try auto-generated
                'subtitleslangs': languages,
                'subtitlesformat': 'json3',
                'skip_download': True,
                'quiet': True,
                'no_warnings': True,
                'outtmpl': f'{tmpdir}/%(id)s.%(ext)s',
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Download subtitle files
                    ydl.download([url])

                    # Try to find subtitle file
                    tmpdir_path = Path(tmpdir)

                    # Try each language in order
                    for lang in languages:
                        subtitle_file = tmpdir_path / f'{video_id}.{lang}.json3'
                        if subtitle_file.exists():
                            return await self._parse_subtitles(subtitle_file)

                    # Try auto-generated with -orig suffix
                    for lang in languages:
                        subtitle_file = tmpdir_path / f'{video_id}.{lang}-orig.json3'
                        if subtitle_file.exists():
                            return await self._parse_subtitles(subtitle_file)

                    logger.debug(f"No subtitles found for {video_id}")
                    return None, []

            except Exception as e:
                logger.warning(f"Error getting subtitles for {video_id}: {e}")
                return None, []

    async def _parse_subtitles(
        self,
        subtitle_file: Path,
    ) -> tuple[str, List[Dict]]:
        """Parse subtitle data into text and segments.

        Args:
            subtitle_file: Path to subtitle file in json3 format

        Returns:
            Tuple of (full_text, segments_list)
            where segments_list contains dicts with 'text', 'start', 'end'
        """
        try:
            data = json.loads(subtitle_file.read_text())

            text_parts = []
            segments = []

            for event in data.get('events', []):
                # Extract start time (in milliseconds, convert to seconds)
                start_time = event.get('tStartMs', 0) / 1000.0

                # Extract duration and calculate end time
                duration = event.get('dDurationMs', 0) / 1000.0
                end_time = start_time + duration

                # Extract text from segments
                segment_text = []
                if 'segs' in event:
                    for seg in event['segs']:
                        if 'utf8' in seg:
                            segment_text.append(seg['utf8'])

                if segment_text:
                    text = ''.join(segment_text).replace('\n', ' ').strip()
                    if text:
                        text_parts.append(text)
                        segments.append({
                            'text': text,
                            'start': start_time,
                            'end': end_time,
                        })

            full_text = ' '.join(text_parts)
            return full_text, segments

        except Exception as e:
            logger.error(f"Error parsing subtitle file {subtitle_file}: {e}")
            return "", []

    @with_retry(max_attempts=2)  # Fewer retries for downloads (expensive)
    async def _download_audio(self, video_id: str) -> Optional[Path]:
        """
        Download audio for Whisper transcription.

        Returns path to temporary audio file.
        Caller is responsible for cleanup.

        Args:
            video_id: YouTube video ID

        Returns:
            Path to downloaded audio file, or None on error
        """
        url = f"https://youtube.com/watch?v={video_id}"

        # Create temp file
        temp_dir = Path(tempfile.gettempdir()) / "curator_audio"
        temp_dir.mkdir(exist_ok=True)
        audio_path = temp_dir / f"{video_id}.wav"

        ydl_opts = {
            **self._ydl_opts,
            'format': 'bestaudio/best',
            'outtmpl': str(audio_path.with_suffix('')),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
            }],
            'skip_download': False,  # Actually download
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if audio_path.exists():
                return audio_path

            return None

        except Exception as e:
            logger.error(f"Error downloading audio for {video_id}: {e}")
            return None

    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        """
        Estimate cost for YouTube video ingestion.

        Costs:
        - yt-dlp calls: $0 (free)
        - Subtitles: $0 (free, from YouTube)
        - Whisper (local): $0 (using mlx-whisper on Apple Silicon)
        - Embeddings: $0 (local BAAI model)

        Main cost is compute time, not money.

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            CostEstimate with cost breakdown
        """
        duration_seconds = metadata.duration_seconds or 0
        duration_minutes = duration_seconds / 60

        # Estimate transcript length
        # Average speaking rate: ~150 words/minute
        estimated_words = int(duration_minutes * 150)
        estimated_tokens = int(estimated_words * 1.3)

        # Estimate chunks
        chunk_size = 500  # tokens
        estimated_chunks = max(1, estimated_tokens // chunk_size)

        warnings = []

        # Warn for very long videos
        if duration_minutes > 120:
            warnings.append(f"Long video ({duration_minutes:.0f} min) - transcription may take a while")

        if duration_minutes > 240:
            warnings.append("Very long video - consider splitting or skipping")

        return CostEstimate(
            api_calls=1,  # One yt-dlp call
            api_cost_usd=0.0,  # Free
            transcription_minutes=duration_minutes if self._needs_whisper(metadata) else 0,
            transcription_cost_usd=0.0,  # Local Whisper is free
            embedding_tokens=estimated_tokens,
            embedding_cost_usd=0.0,  # Local embeddings are free
            total_cost_usd=0.0,  # Everything is local/free
            warnings=warnings if warnings else None,
        )

    def _needs_whisper(self, metadata: ContentMetadata) -> bool:
        """
        Estimate if video will need Whisper (no subtitles).

        Heuristic: Can't know for sure without checking, but some channels
        typically have subtitles and others don't.
        """
        # For now, assume we might need Whisper
        # Could be smarter based on channel history
        return True

    def chunk_content(
        self,
        content: ContentResult,
        metadata: ContentMetadata,
        target_tokens: int = 500,
    ) -> List[Dict]:
        """
        Chunk YouTube transcript with timestamp preservation.

        Enhances chunks with YouTube-specific metadata:
        - start_time, end_time
        - video_url_with_timestamp (for easy jumping to source)
        """
        from curator.chunking import chunk_with_timestamps, chunk_by_semantic

        video_id = metadata.content_id

        if content.segments:
            # Use timestamp-aware chunking
            chunks = chunk_with_timestamps(
                content.text,
                content.segments,
                target_tokens=target_tokens,
            )

            # Enhance with YouTube-specific metadata
            for chunk in chunks:
                start_time = chunk.get('metadata', {}).get('start_time', 0)
                chunk['metadata'] = chunk.get('metadata', {})
                chunk['metadata'].update({
                    'video_id': video_id,
                    'video_title': metadata.title,
                    'channel': metadata.author,
                    'video_url_with_timestamp': build_video_url(video_id, int(start_time)),
                })

            return chunks

        else:
            # No timestamps, use semantic chunking
            chunks = chunk_by_semantic(content.text, target_tokens=target_tokens)

            # Add basic video metadata
            for chunk in chunks:
                chunk['metadata'] = chunk.get('metadata', {})
                chunk['metadata'].update({
                    'video_id': video_id,
                    'video_title': metadata.title,
                    'channel': metadata.author,
                    'video_url': build_video_url(video_id),
                })

            return chunks
