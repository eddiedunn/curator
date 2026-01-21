"""Tests for configuration and settings."""

import pytest
from pathlib import Path
import tempfile
import os
from curator.config import CuratorSettings, get_settings, init_directories


def test_default_settings():
    """Test default settings values."""
    settings = CuratorSettings()

    assert settings.data_dir == Path.home() / ".curator" / "data"
    assert settings.cache_dir == Path.home() / ".curator" / "cache"
    assert "curator.db" in settings.database_url
    assert settings.engram_api_url == "http://localhost:8800"
    assert settings.transcribe_service_url == "http://localhost:8720"
    assert settings.check_interval == 3600
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8950


def test_custom_settings_via_kwargs():
    """Test custom settings via constructor."""
    settings = CuratorSettings(
        api_port=9000,
        log_level="DEBUG",
        max_concurrent_jobs=5
    )

    assert settings.api_port == 9000
    assert settings.log_level == "DEBUG"
    assert settings.max_concurrent_jobs == 5


def test_settings_from_environment(monkeypatch):
    """Test settings loaded from environment variables."""
    monkeypatch.setenv("CURATOR_API_PORT", "9999")
    monkeypatch.setenv("CURATOR_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("CURATOR_ENGRAM_API_URL", "http://custom:8888")

    # Clear the lru_cache
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.api_port == 9999
    assert settings.log_level == "WARNING"
    assert settings.engram_api_url == "http://custom:8888"

    # Clean up
    get_settings.cache_clear()


def test_rate_limits():
    """Test rate limit settings."""
    settings = CuratorSettings()

    assert settings.youtube_rate_limit == 100
    assert settings.rss_rate_limit == 1000
    assert settings.podcast_rate_limit == 50


def test_processing_settings():
    """Test processing configuration."""
    settings = CuratorSettings()

    assert settings.max_concurrent_jobs == 3
    assert settings.job_timeout == 3600


def test_logging_settings():
    """Test logging configuration."""
    settings = CuratorSettings()

    assert settings.log_level == "INFO"
    assert settings.log_format == "json"


def test_custom_data_dir():
    """Test custom data directory."""
    custom_path = Path("/tmp/custom/curator")
    settings = CuratorSettings(data_dir=custom_path)

    assert settings.data_dir == custom_path


def test_custom_database_url():
    """Test custom database URL."""
    custom_url = "sqlite:///tmp/custom.db"
    settings = CuratorSettings(database_url=custom_url)

    assert settings.database_url == custom_url


def test_api_key_optional():
    """Test that API key is optional."""
    settings = CuratorSettings()
    assert settings.api_key is None

    settings_with_key = CuratorSettings(api_key="secret123")
    assert settings_with_key.api_key == "secret123"


def test_engram_api_key_optional():
    """Test that Engram API key is optional."""
    settings = CuratorSettings()
    assert settings.engram_api_key is None


def test_init_directories():
    """Test directory initialization."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        cache_dir = Path(tmpdir) / "cache"

        # Clear cache and set custom dirs
        get_settings.cache_clear()
        settings = CuratorSettings(
            data_dir=data_dir,
            cache_dir=cache_dir
        )

        # Mock get_settings to return our custom settings
        import curator.config
        original_get_settings = curator.config.get_settings
        curator.config.get_settings = lambda: settings

        try:
            # Directories should not exist yet
            assert not data_dir.exists()
            assert not cache_dir.exists()

            # Initialize directories
            init_directories()

            # Directories should now exist
            assert data_dir.exists()
            assert cache_dir.exists()
        finally:
            curator.config.get_settings = original_get_settings
            get_settings.cache_clear()


def test_settings_singleton():
    """Test that get_settings returns cached instance."""
    get_settings.cache_clear()

    settings1 = get_settings()
    settings2 = get_settings()

    assert settings1 is settings2

    get_settings.cache_clear()


def test_check_interval_customization():
    """Test customizing check interval."""
    settings = CuratorSettings(check_interval=7200)
    assert settings.check_interval == 7200


def test_env_prefix():
    """Test that environment variables use CURATOR_ prefix."""
    settings = CuratorSettings()
    assert settings.model_config["env_prefix"] == "CURATOR_"
