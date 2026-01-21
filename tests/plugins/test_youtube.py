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
