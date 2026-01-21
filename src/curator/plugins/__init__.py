"""Content ingestion plugins."""

from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate,
)
from curator.plugins.youtube import YouTubePlugin

__all__ = [
    "IngestionPlugin",
    "ContentMetadata",
    "ContentResult",
    "CostEstimate",
    "YouTubePlugin",
]
