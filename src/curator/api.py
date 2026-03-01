"""FastAPI REST API for Curator service."""

import time
import uuid
from typing import List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
import structlog

from curator.config import get_settings
from curator.storage import CuratorStorage
from curator.models import (
    SubscriptionCreate,
    SubscriptionUpdate,
    SubscriptionResponse,
    IngestedItemResponse,
    FetchJobRequest,
    FetchJobResponse,
    HealthResponse,
    StatusResponse,
    IngestionStatus,
)
from curator.orchestrator import IngestionOrchestrator

logger = structlog.get_logger()

# Service start time for uptime tracking
_start_time = time.time()

# Global instances
_storage: Optional[CuratorStorage] = None
_orchestrator: Optional[IngestionOrchestrator] = None

# API key security
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_storage() -> CuratorStorage:
    """Get storage instance (lazy initialization)."""
    global _storage
    if _storage is None:
        settings = get_settings()
        db_path = settings.database_url.replace("sqlite:///", "")
        _storage = CuratorStorage(db_path)
    return _storage


def get_orchestrator() -> IngestionOrchestrator:
    """Get orchestrator instance (lazy initialization)."""
    global _orchestrator
    if _orchestrator is None:
        settings = get_settings()
        storage = get_storage()
        _orchestrator = IngestionOrchestrator(storage, settings)
    return _orchestrator


async def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Verify API key if configured."""
    settings = get_settings()

    # If API key is not configured, allow all requests
    if not hasattr(settings, 'api_key') or not settings.api_key:
        return None

    # If API key is configured, require it
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    if api_key != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup/shutdown."""
    # Startup
    logger.info("Starting Curator API service")

    # Initialize storage and orchestrator
    storage = get_storage()
    orchestrator = get_orchestrator()

    logger.info("Curator API service started",
                database_connected=storage.health_check())

    yield

    # Shutdown
    logger.info("Shutting down Curator API service")

    # Cleanup if needed
    if _storage:
        # Storage cleanup (if any)
        pass

    if _orchestrator:
        # Orchestrator cleanup (if any)
        pass

    logger.info("Curator API service stopped")


# Initialize FastAPI app with lifespan
app = FastAPI(
    title="Curator API",
    description="Content acquisition and curation service",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health endpoints

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    storage = get_storage()
    settings = get_settings()

    return HealthResponse(
        status="healthy",
        version="0.1.0",
        uptime_seconds=time.time() - _start_time,
        database_connected=storage.health_check(),
        daemon_running=settings.daemon_enabled,
    )


@app.get("/api/v1/status", response_model=StatusResponse)
async def detailed_status(api_key: Optional[str] = Depends(verify_api_key)):
    """Detailed status endpoint."""
    storage = get_storage()
    settings = get_settings()

    # Get counts
    try:
        subscriptions = storage.list_subscriptions()
        items = storage.list_ingested_items(limit=1)  # Just to check if items exist

        # Count by status
        enabled_subs = sum(1 for s in subscriptions if s.get('enabled', False))

        return StatusResponse(
            status="healthy",
            version="0.1.0",
            uptime_seconds=time.time() - _start_time,
            database_connected=storage.health_check(),
            daemon_running=settings.daemon_enabled,
            total_subscriptions=len(subscriptions),
            enabled_subscriptions=enabled_subs,
            total_items=0,  # Would need a count method
        )
    except Exception as e:
        logger.error("Failed to get detailed status", error=str(e))
        return StatusResponse(
            status="degraded",
            version="0.1.0",
            uptime_seconds=time.time() - _start_time,
            database_connected=False,
            daemon_running=False,
            total_subscriptions=0,
            enabled_subscriptions=0,
            total_items=0,
        )


# Subscription endpoints

@app.post("/api/v1/subscriptions", response_model=SubscriptionResponse)
async def create_subscription(
    subscription: SubscriptionCreate,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Create a new subscription."""
    storage = get_storage()

    try:
        sub_id = storage.create_subscription(
            name=subscription.name,
            subscription_type=subscription.subscription_type,
            source_url=subscription.source_url,
            check_frequency_minutes=subscription.check_frequency_minutes,
            enabled=subscription.enabled,
            metadata=subscription.metadata,
        )

        # Fetch and return the created subscription
        sub_data = storage.get_subscription(sub_id)
        if not sub_data:
            raise HTTPException(status_code=500, detail="Failed to create subscription")

        return SubscriptionResponse(**sub_data)

    except Exception as e:
        logger.error("Failed to create subscription", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/subscriptions", response_model=List[SubscriptionResponse])
async def list_subscriptions(
    enabled_only: bool = False,
    subscription_type: Optional[str] = None,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """List all subscriptions."""
    storage = get_storage()

    try:
        from curator.models import SubscriptionType
        sub_type = SubscriptionType(subscription_type) if subscription_type else None

        subscriptions = storage.list_subscriptions(
            enabled_only=enabled_only,
            subscription_type=sub_type,
        )

        return [SubscriptionResponse(**sub) for sub in subscriptions]

    except Exception as e:
        logger.error("Failed to list subscriptions", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/subscriptions/{id}", response_model=SubscriptionResponse)
async def get_subscription(
    id: int,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Get a specific subscription."""
    storage = get_storage()

    sub_data = storage.get_subscription(id)
    if not sub_data:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return SubscriptionResponse(**sub_data)


@app.patch("/api/v1/subscriptions/{id}", response_model=SubscriptionResponse)
async def update_subscription(
    id: int,
    update: SubscriptionUpdate,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Update a subscription."""
    storage = get_storage()

    # Check subscription exists
    sub_data = storage.get_subscription(id)
    if not sub_data:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Build update dict from provided fields only
    update_data = update.model_dump(exclude_unset=True)

    # Update subscription
    success = storage.update_subscription(id, **update_data)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update subscription")

    # Return updated subscription
    sub_data = storage.get_subscription(id)
    return SubscriptionResponse(**sub_data)


@app.delete("/api/v1/subscriptions/{id}")
async def delete_subscription(
    id: int,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Delete a subscription."""
    storage = get_storage()

    success = storage.delete_subscription(id)
    if not success:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return {"message": "Subscription deleted successfully"}


# Fetching endpoints

@app.post("/api/v1/fetch", response_model=FetchJobResponse)
async def trigger_fetch(
    request: FetchJobRequest,
    background_tasks: BackgroundTasks,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Trigger a one-off fetch."""
    orchestrator = get_orchestrator()
    storage = get_storage()

    try:
        # Generate job ID
        job_id = str(uuid.uuid4())

        # Create fetch job
        storage.create_fetch_job(job_id, request.source_url)

        # Schedule background ingestion
        background_tasks.add_task(
            orchestrator.ingest_url,
            request.source_url,
            subscription_id=request.subscription_id,
            job_id=job_id,
        )

        return FetchJobResponse(
            job_id=job_id,
            source_url=request.source_url,
            status=IngestionStatus.PENDING,
            message="Fetch job queued",
        )

    except Exception as e:
        logger.error("Failed to queue fetch", error=str(e), url=request.source_url)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/fetch/{job_id}", response_model=FetchJobResponse)
async def get_fetch_job(
    job_id: str,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Get fetch job status."""
    storage = get_storage()

    job_data = storage.get_fetch_job(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")

    return FetchJobResponse(
        job_id=job_id,
        source_url=job_data["source_url"],
        status=IngestionStatus(job_data["status"]),
        message=job_data.get("error_message") or "",
    )


# Content endpoints

@app.get("/api/v1/ingested", response_model=List[IngestedItemResponse])
async def list_ingested_items(
    subscription_id: Optional[int] = None,
    source_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """List ingested items."""
    storage = get_storage()

    try:
        from curator.models import IngestionStatus
        status_enum = IngestionStatus(status) if status else None

        items = storage.list_ingested_items(
            subscription_id=subscription_id,
            source_type=source_type,
            status=status_enum,
            limit=limit,
            offset=offset,
        )

        return [IngestedItemResponse(**item) for item in items]

    except Exception as e:
        logger.error("Failed to list items", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/ingested/{id}", response_model=IngestedItemResponse)
async def get_ingested_item(
    id: int,
    api_key: Optional[str] = Depends(verify_api_key)
):
    """Get a specific ingested item."""
    storage = get_storage()

    item_data = storage.get_ingested_item(id)
    if not item_data:
        raise HTTPException(status_code=404, detail="Item not found")

    return IngestedItemResponse(**item_data)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "curator",
        "version": "0.1.0",
        "status": "running",
    }
