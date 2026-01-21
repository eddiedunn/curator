"""YouTube URL parsing utilities.

This module provides lightweight utility functions for parsing YouTube URLs
and extracting IDs from various URL formats. These functions use regex patterns
and do not require external dependencies like yt-dlp.
"""

import re
from typing import Optional
from urllib.parse import urlparse, parse_qs


def extract_video_id(url: str) -> Optional[str]:
    """
    Extract video ID from various YouTube URL formats.

    Supported formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://www.youtube.com/v/VIDEO_ID
    - https://youtube.com/watch?v=VIDEO_ID&t=123
    - https://m.youtube.com/watch?v=VIDEO_ID

    Args:
        url: YouTube URL or video ID string

    Returns:
        Video ID string (11 characters), or None if not a valid YouTube URL

    Examples:
        >>> extract_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ")
        'dQw4w9WgXcQ'
        >>> extract_video_id("https://youtu.be/dQw4w9WgXcQ")
        'dQw4w9WgXcQ'
        >>> extract_video_id("dQw4w9WgXcQ")
        'dQw4w9WgXcQ'
    """
    if not url:
        return None

    # YouTube video IDs are exactly 11 characters: alphanumeric, underscore, hyphen
    video_id_pattern = r'[a-zA-Z0-9_-]{11}'

    # Pattern 1: watch?v=VIDEO_ID (most common)
    # Handles: youtube.com/watch?v=ID, m.youtube.com/watch?v=ID
    match = re.search(rf'[?&]v=({video_id_pattern})', url)
    if match:
        return match.group(1)

    # Pattern 2: youtu.be/VIDEO_ID (short URL)
    match = re.search(rf'youtu\.be/({video_id_pattern})', url)
    if match:
        return match.group(1)

    # Pattern 3: /embed/VIDEO_ID
    match = re.search(rf'/embed/({video_id_pattern})', url)
    if match:
        return match.group(1)

    # Pattern 4: /v/VIDEO_ID
    match = re.search(rf'/v/({video_id_pattern})', url)
    if match:
        return match.group(1)

    # Pattern 5: Raw video ID (if input is just the ID itself)
    if re.match(rf'^{video_id_pattern}$', url):
        return url

    return None


def extract_channel_id(url: str) -> Optional[str]:
    """
    Extract channel ID from YouTube channel URLs.

    Supported formats:
    - https://www.youtube.com/channel/CHANNEL_ID
    - https://www.youtube.com/@username
    - https://www.youtube.com/c/ChannelName

    Args:
        url: YouTube channel URL

    Returns:
        Channel ID or handle, or None if not a channel URL

    Examples:
        >>> extract_channel_id("https://www.youtube.com/channel/UCxxxxxx")
        'UCxxxxxx'
        >>> extract_channel_id("https://www.youtube.com/@username")
        '@username'
        >>> extract_channel_id("https://www.youtube.com/c/ChannelName")
        'ChannelName'
    """
    if not url:
        return None

    # Pattern 1: /channel/UC... (canonical channel ID format)
    match = re.search(r'/channel/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern 2: /@username (handle format)
    match = re.search(r'/@([a-zA-Z0-9_-]+)', url)
    if match:
        return f"@{match.group(1)}"

    # Pattern 3: /c/ChannelName (custom channel name)
    match = re.search(r'/c/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern 4: /user/username (legacy format)
    match = re.search(r'/user/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    return None


def extract_playlist_id(url: str) -> Optional[str]:
    """
    Extract playlist ID from YouTube playlist URLs.

    Supported formats:
    - https://www.youtube.com/playlist?list=PLAYLIST_ID
    - https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID

    Args:
        url: YouTube playlist URL

    Returns:
        Playlist ID string, or None if not a playlist URL

    Examples:
        >>> extract_playlist_id("https://www.youtube.com/playlist?list=PLxxxxxx")
        'PLxxxxxx'
        >>> extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxxxxx")
        'PLxxxxxx'
    """
    if not url:
        return None

    # Pattern: list=PLAYLIST_ID in query parameters
    match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    return None


def build_video_url(video_id: str, timestamp: Optional[int] = None) -> str:
    """
    Build canonical YouTube video URL.

    Args:
        video_id: YouTube video ID (11 characters)
        timestamp: Optional timestamp in seconds to start playback

    Returns:
        Canonical YouTube URL (e.g., https://youtube.com/watch?v=VIDEO_ID&t=123s)

    Examples:
        >>> build_video_url("dQw4w9WgXcQ")
        'https://youtube.com/watch?v=dQw4w9WgXcQ'
        >>> build_video_url("dQw4w9WgXcQ", 123)
        'https://youtube.com/watch?v=dQw4w9WgXcQ&t=123s'
    """
    url = f"https://youtube.com/watch?v={video_id}"
    if timestamp is not None and timestamp > 0:
        url += f"&t={timestamp}s"
    return url


def is_youtube_url(url: str) -> bool:
    """
    Check if URL is a YouTube URL.

    Args:
        url: URL string to check

    Returns:
        True if the URL is from a YouTube domain, False otherwise

    Examples:
        >>> is_youtube_url("https://youtube.com/watch?v=dQw4w9WgXcQ")
        True
        >>> is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
        True
        >>> is_youtube_url("https://example.com")
        False
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
        return parsed.netloc in [
            'youtube.com',
            'www.youtube.com',
            'm.youtube.com',
            'youtu.be',
            'www.youtu.be'
        ]
    except Exception:
        return False
