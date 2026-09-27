"""Tests for YouTube plugin."""

import pytest

from curator.plugins.youtube_utils import (
    extract_video_id,
    is_youtube_url,
    build_video_url,
)


def test_extract_video_id():
    """Test video ID extraction from various URL formats."""
    test_cases = [
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=123", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ]

    for url, expected_id in test_cases:
        assert extract_video_id(url) == expected_id


def test_is_youtube_url():
    """Test YouTube URL detection."""
    assert is_youtube_url("https://youtube.com/watch?v=test") is True
    assert is_youtube_url("https://youtu.be/test") is True
    assert is_youtube_url("https://example.com") is False
    assert is_youtube_url("") is False


def test_build_video_url():
    """Test building canonical YouTube URLs."""
    assert build_video_url("dQw4w9WgXcQ") == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert build_video_url("dQw4w9WgXcQ", 123) == "https://youtube.com/watch?v=dQw4w9WgXcQ&t=123s"


# --- Permanently-unavailable classification --------------------------------

from unittest.mock import MagicMock, patch

import yt_dlp

from curator.plugins.base import ContentUnavailableError
from curator.plugins.youtube import (
    YouTubePlugin,
    is_permanently_unavailable,
    with_retry,
)

MEMBERS_ONLY_MSG = (
    "ERROR: [youtube] abcdefghijk: Join this channel to get access to "
    "members-only content like this video, and other exclusive perks."
)
MEMBERS_LEVEL_MSG = (
    "ERROR: [youtube] abcdefghijk: This video is available to this channel's "
    "members on level: Junior Dev (or any higher level). Join this channel to "
    "get access to members-only content and other exclusive perks."
)


@pytest.mark.parametrize("message", [
    MEMBERS_ONLY_MSG,
    MEMBERS_LEVEL_MSG,
    "ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access to this video",
    "ERROR: [youtube] abcdefghijk: This video has been removed by the uploader",
    "ERROR: [youtube] abcdefghijk: Video unavailable. This video is no longer available "
    "because the YouTube account associated with this video has been terminated.",
])
def test_is_permanently_unavailable_true(message):
    assert is_permanently_unavailable(message) is True


@pytest.mark.parametrize("message", [
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "ERROR: [youtube] abcdefghijk: Unable to download webpage: <urlopen error [Errno -3] "
    "Temporary failure in name resolution>",
    "ERROR: [youtube] abcdefghijk: Video unavailable. This content isn't available, try again later.",
    "ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot.",
    "",
])
def test_is_permanently_unavailable_false(message):
    assert is_permanently_unavailable(message) is False


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_members_only():
    calls = 0

    @with_retry(max_attempts=3, base_delay=0)
    async def flaky():
        nonlocal calls
        calls += 1
        raise yt_dlp.utils.DownloadError(MEMBERS_ONLY_MSG)

    with pytest.raises(yt_dlp.utils.DownloadError):
        await flaky()
    assert calls == 1


def _ydl_raising(message):
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(message)
    return ydl


@pytest.mark.asyncio
async def test_fetch_metadata_raises_content_unavailable_for_members_only():
    plugin = YouTubePlugin()
    with patch("curator.plugins.youtube.yt_dlp.YoutubeDL", return_value=_ydl_raising(MEMBERS_LEVEL_MSG)):
        with pytest.raises(ContentUnavailableError) as exc_info:
            await plugin.fetch_metadata("https://youtube.com/watch?v=abcdefghijk")

    assert exc_info.value.content_id == "abcdefghijk"
    assert "members-only" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_metadata_returns_none_for_transient_error():
    plugin = YouTubePlugin()
    msg = "ERROR: [youtube] abcdefghijk: Unable to download webpage: HTTP Error 503"
    with patch("curator.plugins.youtube.yt_dlp.YoutubeDL", return_value=_ydl_raising(msg)):
        assert await plugin.fetch_metadata("https://youtube.com/watch?v=abcdefghijk") is None
