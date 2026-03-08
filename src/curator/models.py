"""Pydantic models for Curator service."""

from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


class SubscriptionStatus(str, Enum):
    """Subscription status enum."""
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class SubscriptionType(str, Enum):
    """Subscription type enum."""
    YOUTUBE_CHANNEL = "youtube_channel"
    RSS_FEED = "rss_feed"
    PODCAST = "podcast"


class IngestionStatus(str, Enum):
    """Ingestion job status enum."""
    PENDING = "pending"
    PROCESSING = "processing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SubscriptionCreate(BaseModel):
    """Model for creating a new subscription."""
    name: str = Field(..., description="Human-readable name for the subscription")
    subscription_type: SubscriptionType = Field(..., description="Type of subscription")
    source_url: str = Field(..., description="Source URL (channel, feed, etc.)")
    check_frequency_minutes: int = Field(60, description="How often to check for new content (minutes)")
    enabled: bool = Field(True, description="Whether subscription is enabled")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class SubscriptionResponse(BaseModel):
    """Model for subscription response."""
    id: int
    name: str
    subscription_type: SubscriptionType
    source_url: str
    check_frequency_minutes: int
    enabled: bool
    status: SubscriptionStatus
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


class IngestedItemResponse(BaseModel):
    """Model for ingested item response."""
    id: int
    subscription_id: Optional[int] = None
    source_type: str
    source_id: str
    source_url: str
    title: str
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    ingested_at: datetime
    chunk_count: int
    status: IngestionStatus
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SubscriptionUpdate(BaseModel):
    """Model for updating a subscription."""
    name: Optional[str] = Field(None, description="Human-readable name for the subscription")
    subscription_type: Optional[SubscriptionType] = Field(None, description="Type of subscription")
    source_url: Optional[str] = Field(None, description="Source URL (channel, feed, etc.)")
    check_frequency_minutes: Optional[int] = Field(None, description="How often to check for new content (minutes)")
    enabled: Optional[bool] = Field(None, description="Whether subscription is enabled")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")


class FetchJobRequest(BaseModel):
    """Model for requesting a fetch job."""
    source_url: str = Field(..., description="URL to fetch")
    subscription_id: Optional[int] = Field(None, description="Optional subscription ID")


class FetchJobResponse(BaseModel):
    """Model for fetch job response."""
    job_id: str
    source_url: str
    status: IngestionStatus
    message: str


class IngestionJobRequest(BaseModel):
    """Model for requesting ingestion of a single item."""
    source_url: str = Field(..., description="URL to ingest")
    subscription_id: Optional[int] = Field(None, description="Optional subscription ID")


class IngestionJobResponse(BaseModel):
    """Model for ingestion job response."""
    job_id: str
    source_url: str
    status: IngestionStatus
    message: str


class StatusResponse(BaseModel):
    """Model for detailed status response."""
    status: str
    version: str
    uptime_seconds: float
    database_connected: bool
    daemon_running: bool
    total_subscriptions: int
    enabled_subscriptions: int
    total_items: int


class HealthResponse(BaseModel):
    """Model for health check response."""
    status: str
    version: str
    uptime_seconds: float
    database_connected: bool
    daemon_running: bool
