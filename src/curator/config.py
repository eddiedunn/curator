from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path
from functools import lru_cache

class CuratorSettings(BaseSettings):
    # Paths
    data_dir: Path = Path.home() / ".curator" / "data"
    cache_dir: Path = Path.home() / ".curator" / "cache"
    database_url: str = Field(
        default_factory=lambda: "sqlite:///" + str(Path.home() / ".curator" / "curator.db"),
        description="SQLite database URL"
    )

    # Service endpoints
    engram_api_url: str = "http://localhost:8800"
    engram_api_key: str | None = None
    transcribe_service_url: str = "http://localhost:8720"
    glimpse_service_url: str = "http://localhost:8730"

    # Daemon
    daemon_enabled: bool = False
    check_interval: int = 3600  # seconds

    # Glimpse visual context enrichment
    glimpse_timeout_seconds: float = 60.0
    glimpse_select_timeout_seconds: float = 180.0
    glimpse_max_frames: int = 5
    glimpse_scene_threshold: float = 0.3
    glimpse_proximity_seconds: float = 5.0
    glimpse_frame_interval_seconds: int = 60
    glimpse_max_attempts: int = 3
    visual_context_enrich_interval_seconds: int = 300
    visual_context_batch_size: int = 10

    # Item expiration / purge
    failed_item_ttl_days: int = 30    # delete failed items older than N days (0 = never)
    pending_item_ttl_days: int = 0    # delete stuck pending items older than N days (0 = never)
    completed_item_ttl_days: int = 0  # delete completed items older than N days (0 = never)
    purge_interval_hours: int = 24    # how often the purge job runs

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8950
    api_key: str | None = None

    # Rate limits (per hour)
    youtube_rate_limit: int = 100
    rss_rate_limit: int = 1000
    podcast_rate_limit: int = 50

    # Processing
    max_concurrent_jobs: int = 3
    job_timeout: int = 3600

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    model_config = {
        "env_prefix": "CURATOR_",
        "env_file": ".env",
    }

@lru_cache
def get_settings() -> CuratorSettings:
    return CuratorSettings()

def init_directories():
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
