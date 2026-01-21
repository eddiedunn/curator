# Curator

Content acquisition and curation service for the PAI ecosystem.

## Overview

Curator handles:
- Content ingestion from multiple sources (YouTube, RSS, podcasts)
- Subscription monitoring for new content
- Content chunking for semantic search
- Integration with Engram (embedding service) and Transcribe (transcription service)

## Architecture

```
curator/
├── src/curator/
│   ├── api.py              # FastAPI REST API
│   ├── cli.py              # Click CLI
│   ├── config.py           # Pydantic Settings
│   ├── storage.py          # SQLite subscription database
│   ├── daemon.py           # APScheduler background daemon
│   ├── orchestrator.py     # Ingestion workflow orchestrator
│   ├── chunking.py         # Text chunking (from pai-corpus)
│   ├── models.py           # Pydantic models
│   └── plugins/
│       ├── base.py         # BasePlugin interface
│       ├── youtube.py      # YouTube plugin (from pai-ingest)
│       ├── rss.py          # RSS/Atom plugin (planned)
│       └── podcast.py      # Podcast plugin (planned)
```

## Installation

```bash
cd ~/code/curator
pip install -e .
```

## Usage

### CLI

```bash
# Start API server
curator serve

# Ingest a single URL
curator ingest https://youtube.com/watch?v=VIDEO_ID

# Manage subscriptions
curator subscription list
curator subscription add "Channel Name" https://youtube.com/@channel
curator subscription remove 1

# List ingested items
curator items

# Run subscription daemon
curator daemon
```

### API

Start the server:
```bash
curator serve
```

API endpoints:
- `GET /health` - Health check
- `POST /subscriptions` - Create subscription
- `GET /subscriptions` - List subscriptions
- `GET /subscriptions/{id}` - Get subscription
- `PATCH /subscriptions/{id}` - Update subscription
- `DELETE /subscriptions/{id}` - Delete subscription
- `POST /ingest` - Ingest a URL
- `GET /ingest/{job_id}` - Get ingestion job status
- `GET /items` - List ingested items
- `GET /items/{id}` - Get ingested item

## Configuration

Environment variables (prefix with `CURATOR_`):

```bash
# Service
CURATOR_SERVICE_NAME=curator
CURATOR_ENVIRONMENT=development
CURATOR_DEBUG=false

# API
CURATOR_API_HOST=0.0.0.0
CURATOR_API_PORT=8003

# Database
CURATOR_DATABASE_URL=sqlite:///./curator.db

# Storage
CURATOR_DATA_DIR=./data

# External services
CURATOR_ENGRAM_URL=http://localhost:8001
CURATOR_TRANSCRIBE_URL=http://localhost:8002

# Plugin configuration
CURATOR_YOUTUBE_COOKIES_PATH=/path/to/cookies.txt

# Ingestion
CURATOR_DEFAULT_CHUNK_TOKENS=500
CURATOR_MAX_CONCURRENT_INGESTIONS=3
```

## Development

### Running tests

```bash
pytest tests/
```

### Code structure

- **api.py**: FastAPI REST endpoints
- **cli.py**: Click command-line interface
- **storage.py**: SQLite database operations
- **orchestrator.py**: Ingestion workflow coordination
- **daemon.py**: Background subscription monitoring
- **plugins/**: Content source plugins (YouTube, RSS, etc.)

## Deployment

See `deploy/` directory for Docker and Podman configurations.

## License

MIT
