# Curator Architecture

## Overview

Curator is a content acquisition and curation service designed for ingesting content from multiple sources (YouTube, RSS, podcasts) and integrating with the PAI ecosystem's GPU services.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Curator                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐         ┌──────────────┐                  │
│  │     CLI      │         │     API      │                  │
│  │   (Click)    │         │  (FastAPI)   │                  │
│  └──────┬───────┘         └──────┬───────┘                  │
│         │                        │                          │
│         └────────────┬───────────┘                          │
│                      │                                      │
│         ┌────────────▼──────────────┐                       │
│         │   IngestionOrchestrator   │                       │
│         └────────────┬──────────────┘                       │
│                      │                                      │
│         ┌────────────▼──────────────┐                       │
│         │    Plugin Registry        │                       │
│         └────────────┬──────────────┘                       │
│                      │                                      │
│         ┌────────────▼──────────────┐                       │
│         │    Storage (SQLite)       │                       │
│         │  - Subscriptions          │                       │
│         │  - Ingestion jobs         │                       │
│         │  - Item metadata          │                       │
│         └────────────┬──────────────┘                       │
│                      │                                      │
│         ┌────────────▼──────────────┐                       │
│         │     Daemon (APScheduler)  │                       │
│         │  - Subscription polling   │                       │
│         │  - Scheduled ingestion    │                       │
│         └───────────────────────────┘                       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
         │                            │
         ▼                            ▼
┌─────────────────┐        ┌─────────────────┐
│   Transcribe    │        │     Engram      │
│   (Audio→Text)  │        │  (Storage +     │
│    :8720        │        │   Search)       │
└─────────────────┘        │    :8800        │
                          └─────────────────┘
```

## Core Components

### 1. API Layer (`api.py`)

FastAPI-based REST API providing HTTP endpoints for:
- Subscription management (CRUD operations)
- Content ingestion (sync and async)
- Job status queries
- Item retrieval

**Key Features:**
- OpenAPI/Swagger documentation auto-generation
- Pydantic request/response validation
- Health check endpoint
- CORS support for web clients

### 2. CLI (`cli.py`)

Click-based command-line interface providing:
- `serve` - Start API server
- `ingest` - Ingest a single URL
- `subscription` - Manage subscriptions (list, add, remove)
- `items` - List ingested items
- `daemon` - Run subscription polling daemon

**Design Pattern:**
- Commands are thin wrappers around core logic
- Shares the same business logic as API
- Structured logging to stdout/stderr

### 3. Ingestion Orchestrator (`orchestrator.py`)

Central coordinator for content ingestion workflow:

```python
class IngestionOrchestrator:
    def ingest(url: str, plugin: IngestionPlugin) -> str:
        # 1. Fetch metadata via plugin
        metadata = plugin.fetch_metadata(url)

        # 2. Check if content exists in Engram
        if engram.exists(metadata.content_id):
            return metadata.content_id

        # 3. Fetch content via plugin
        content = plugin.fetch_content(metadata)

        # 4. Transcribe if needed
        if content.needs_transcription:
            result = transcribe_service.transcribe(content.audio_path)
            content.text = result["text"]
            content.segments = result["segments"]

        # 5. Store in Engram (which handles embedding)
        engram.store(metadata, content)

        return metadata.content_id
```

**Responsibilities:**
- Plugin selection and validation
- Duplicate detection
- Transcription coordination
- Integration with external services
- Error handling and retries

### 4. Storage Layer (`storage.py`)

SQLite-based storage for curator-specific data:

```sql
-- Subscriptions table
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    last_checked TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Ingestion jobs table (optional, for async tracking)
CREATE TABLE ingestion_jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending, processing, completed, failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT
);

-- Items table (lightweight metadata cache)
CREATE TABLE items (
    content_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Design Decisions:**
- SQLite for simplicity (no external DB required)
- Lightweight storage (full content lives in Engram)
- Foreign key constraints disabled for flexibility
- Migration support via Alembic (future)

### 5. Daemon (`daemon.py`)

Background service using APScheduler for:
- Periodic subscription polling (configurable interval)
- Automatic content discovery and ingestion
- Error recovery and retry logic

```python
class SubscriptionDaemon:
    def __init__(self, storage, orchestrator):
        self.scheduler = AsyncIOScheduler()

    async def start(self):
        # Add job to check subscriptions every N minutes
        self.scheduler.add_job(
            self.check_subscriptions,
            'interval',
            minutes=30,
            id='subscription_check'
        )
        self.scheduler.start()

    async def check_subscriptions(self):
        for sub in storage.get_enabled_subscriptions():
            plugin = get_plugin(sub.source_type)
            new_items = plugin.list_new_content(sub.url, sub.last_checked)
            for item_url in new_items:
                orchestrator.ingest(item_url, plugin)
            storage.update_last_checked(sub.id)
```

### 6. Plugin System (`plugins/`)

Extensible plugin architecture for content sources.

**Directory Structure:**
```
plugins/
├── base.py           # Abstract base classes
├── youtube.py        # YouTube implementation
├── rss.py            # RSS/Atom feeds
├── podcast.py        # Podcast feeds
└── youtube_utils.py  # YouTube-specific utilities
```

See [PLUGINS.md](PLUGINS.md) for detailed plugin documentation.

## Data Flow

### Single URL Ingestion

```
User Request
    │
    ▼
┌─────────────────────┐
│   API/CLI           │
│   ingest(url)       │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Orchestrator      │
│   1. Select plugin  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Plugin            │
│   fetch_metadata()  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Engram            │
│   check_exists()    │
└──────────┬──────────┘
           │
     ┌─────┴──────┐
     │ Exists?    │
     └─────┬──────┘
           │ No
           ▼
┌─────────────────────┐
│   Plugin            │
│   fetch_content()   │
└──────────┬──────────┘
           │
     ┌─────┴──────┐
     │ Audio?     │
     └─────┬──────┘
           │ Yes
           ▼
┌─────────────────────┐
│   Transcribe        │
│   transcribe()      │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Engram            │
│   store()           │
│   (→ Embed service) │
└──────────┬──────────┘
           │
           ▼
      Return content_id
```

### Subscription Monitoring

```
Daemon (every 30 min)
    │
    ▼
┌─────────────────────┐
│   Get enabled       │
│   subscriptions     │
└──────────┬──────────┘
           │
           ▼ (for each)
┌─────────────────────┐
│   Plugin            │
│   list_new_content()│
└──────────┬──────────┘
           │
           ▼ (for each new item)
┌─────────────────────┐
│   Orchestrator      │
│   ingest(url)       │
└──────────┬──────────┘
           │
           ▼
    (same as above)
```

## Configuration

### Settings (`config.py`)

```python
from pydantic_settings import BaseSettings

class CuratorSettings(BaseSettings):
    # Service
    service_name: str = "curator"
    environment: str = "development"
    debug: bool = False

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8003

    # Database
    database_url: str = "sqlite:///./curator.db"

    # Storage
    data_dir: Path = Path("./data")

    # External services
    engram_url: str = "http://localhost:8001"
    transcribe_url: str = "http://localhost:8002"

    # Plugin configuration
    youtube_cookies_path: Optional[Path] = None

    # Ingestion
    default_chunk_tokens: int = 500
    max_concurrent_ingestions: int = 3

    # Daemon
    subscription_check_interval: int = 30  # minutes

    class Config:
        env_prefix = "CURATOR_"
```

## Error Handling

### Retry Strategy

- **Network errors**: Exponential backoff with jitter
- **API rate limits**: Respect Retry-After headers
- **Transient failures**: 3 retries with 1s, 5s, 15s delays
- **Permanent failures**: Log and mark job as failed

### Error Categories

1. **Plugin errors** (404, invalid URL): Don't retry
2. **Service errors** (Engram down): Retry with backoff
3. **Resource errors** (disk full): Alert and halt
4. **Rate limits**: Queue and retry after delay

## Performance Considerations

### Concurrency

- **API**: Async endpoints using `httpx.AsyncClient`
- **CLI**: Synchronous for simplicity
- **Daemon**: Async with max concurrent ingestions limit
- **Plugin I/O**: All network ops are async

### Resource Limits

- **Max batch size**: 100 URLs per ingestion batch
- **Concurrent ingestions**: 3 (configurable)
- **Request timeout**: 60s (configurable per endpoint)
- **Database connection pool**: 5 connections

### Caching

- **Plugin registry**: Singleton pattern
- **HTTP client**: Reuse connections via `httpx` session
- **Database queries**: Prepared statements

## Security

### Input Validation

- URL validation via `pydantic.HttpUrl`
- File path sanitization
- SQL injection prevention (parameterized queries)

### Authentication (Future)

- API key authentication for REST API
- JWT tokens for user sessions
- Service-to-service mTLS

### Data Privacy

- No sensitive data in logs
- Secure cookie storage for YouTube
- Encrypted database (future)

## Monitoring and Observability

### Logging

```python
import structlog

logger = structlog.get_logger()

# Structured logging
logger.info("content_ingested",
    content_id=content_id,
    source_type=plugin.source_type,
    duration_sec=duration
)
```

### Metrics (Future)

- Ingestion rate (items/hour)
- Error rate by source type
- Processing duration (p50, p95, p99)
- Queue depth

### Health Checks

```
GET /health
{
  "status": "healthy",
  "database": "connected",
  "engram": "reachable",
  "transcribe": "reachable"
}
```

## Testing Strategy

### Unit Tests

- Plugin implementations
- Storage operations
- Configuration validation

### Integration Tests

- End-to-end ingestion flow
- External service mocking
- Database migrations

### E2E Tests

- Full workflow with real services
- Subscription daemon operation
- Error recovery scenarios

## Future Enhancements

1. **Async job queue**: Replace in-memory queue with Redis/Celery
2. **Plugin marketplace**: Dynamic plugin loading
3. **Content versioning**: Track updates to ingested content
4. **Advanced scheduling**: Cron-like subscription schedules
5. **Multi-tenancy**: User accounts and permissions
6. **Analytics**: Content discovery and recommendation
7. **Webhooks**: Real-time notifications on new content
8. **Distributed deployment**: Multi-instance coordination
