# Curator Plugin System

## Overview

Curator uses a plugin architecture to support ingestion from multiple content sources. Each plugin implements a standard interface defined by `IngestionPlugin` and handles source-specific details like authentication, API calls, and content extraction.

## Plugin Interface

All plugins must inherit from `IngestionPlugin` and implement the following methods:

```python
from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate
)

class MyPlugin(IngestionPlugin):
    @property
    def source_type(self) -> str:
        """Unique identifier for this content source."""
        return "my_source"

    @property
    def name(self) -> str:
        """Human-readable plugin name."""
        return "My Content Source"

    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata without downloading content."""
        ...

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        """Download or extract the actual content."""
        ...

    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        """Calculate expected processing costs."""
        ...

    def chunk_content(self, content: ContentResult, metadata: ContentMetadata,
                     target_tokens: int = 500) -> List[Dict]:
        """Chunk content for embedding (optional override)."""
        ...

    def validate_url(self, url: str) -> bool:
        """Check if URL is valid for this plugin (optional override)."""
        ...
```

## Data Structures

### ContentMetadata

Metadata about content without downloading the full content:

```python
@dataclass
class ContentMetadata:
    content_id: str              # Unique ID within source (e.g., YouTube video ID)
    title: str                   # Content title
    url: str                     # Full URL
    description: Optional[str]   # Content description
    author: Optional[str]        # Creator/author name
    published_at: Optional[str]  # Publication date (ISO 8601)
    duration_seconds: Optional[int]  # Duration for time-based content
    extra: Dict[str, Any]        # Source-specific metadata
```

### ContentResult

The actual content text or audio reference:

```python
@dataclass
class ContentResult:
    text: str                    # Full text or path to audio file
    segments: Optional[List[Dict]]  # Timestamped segments
    source: str                  # How content was obtained
    needs_transcription: bool    # If true, text is audio file path
```

**Segment Format:**
```python
{
    "start": 0.0,    # Start time in seconds
    "end": 2.5,      # End time in seconds
    "text": "Hello"  # Text for this segment
}
```

### CostEstimate

Expected costs for processing:

```python
@dataclass
class CostEstimate:
    api_calls: int = 0
    api_cost_usd: float = 0.0
    transcription_minutes: float = 0.0
    transcription_cost_usd: float = 0.0
    embedding_tokens: int = 0
    embedding_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    warnings: Optional[List[str]] = None
```

## Implementation Guide

### Step 1: Create Plugin File

Create a new file in `src/curator/plugins/`:

```bash
touch src/curator/plugins/my_source.py
```

### Step 2: Implement the Interface

```python
# src/curator/plugins/my_source.py
import logging
from typing import Optional, List, Dict
from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate
)

logger = logging.getLogger(__name__)

class MySourcePlugin(IngestionPlugin):
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    @property
    def source_type(self) -> str:
        return "my_source"

    @property
    def name(self) -> str:
        return "My Content Source"

    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        try:
            # Extract content ID from URL
            content_id = self._extract_id(source_url)
            if not content_id:
                return None

            # Call API to get metadata
            info = await self._api_call(f"/content/{content_id}")

            return ContentMetadata(
                content_id=content_id,
                title=info["title"],
                url=source_url,
                description=info.get("description"),
                author=info.get("author"),
                published_at=info.get("published_at"),
                extra={
                    "views": info.get("view_count"),
                    "tags": info.get("tags", [])
                }
            )

        except Exception as e:
            logger.error(f"Failed to fetch metadata: {e}")
            return None

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        try:
            # Fetch the actual content
            content = await self._api_call(f"/content/{metadata.content_id}/text")

            return ContentResult(
                text=content["text"],
                source="api",
                needs_transcription=False
            )

        except Exception as e:
            logger.error(f"Failed to fetch content: {e}")
            return None

    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        estimate = CostEstimate()

        # API call cost
        estimate.api_calls = 2  # metadata + content
        estimate.api_cost_usd = 0.002

        # Estimate embedding cost
        # Rough estimate: ~100 tokens per minute of content
        est_tokens = (metadata.duration_seconds or 0) * 100 / 60
        estimate.embedding_tokens = int(est_tokens)
        estimate.embedding_cost_usd = (est_tokens / 1000) * 0.0001

        estimate.total_cost_usd = estimate.api_cost_usd + estimate.embedding_cost_usd

        return estimate

    def _extract_id(self, url: str) -> Optional[str]:
        # URL parsing logic
        ...

    async def _api_call(self, endpoint: str) -> dict:
        # API call implementation
        ...
```

### Step 3: Register the Plugin

Update `src/curator/plugins/__init__.py`:

```python
from curator.plugins.base import IngestionPlugin
from curator.plugins.youtube import YouTubePlugin
from curator.plugins.my_source import MySourcePlugin

PLUGINS = {
    "youtube": YouTubePlugin,
    "my_source": MySourcePlugin,
}

def get_plugin(source_type: str) -> IngestionPlugin:
    plugin_class = PLUGINS.get(source_type)
    if not plugin_class:
        raise ValueError(f"Unknown source type: {source_type}")
    return plugin_class()
```

### Step 4: Add Tests

Create `tests/plugins/test_my_source.py`:

```python
import pytest
from curator.plugins.my_source import MySourcePlugin

@pytest.fixture
def plugin():
    return MySourcePlugin()

@pytest.mark.asyncio
async def test_fetch_metadata(plugin):
    url = "https://example.com/content/123"
    metadata = await plugin.fetch_metadata(url)

    assert metadata is not None
    assert metadata.content_id == "123"
    assert metadata.title

@pytest.mark.asyncio
async def test_fetch_content(plugin):
    metadata = ContentMetadata(
        content_id="123",
        title="Test",
        url="https://example.com/content/123"
    )
    content = await plugin.fetch_content(metadata)

    assert content is not None
    assert content.text
    assert not content.needs_transcription
```

## Built-in Plugins

### YouTube Plugin

**Source Type:** `youtube`

**Supported URLs:**
- `https://youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID`
- `https://youtube.com/watch?v=VIDEO_ID&t=123s`

**Features:**
- Metadata extraction via yt-dlp
- Audio download via yt-dlp (always — no subtitles)
- Diarized transcription via diarized-transcriber (Whisper + pyannote)
- Retry with exponential backoff
- Cookie support for age-restricted videos

**Configuration:**
```python
CURATOR_YOUTUBE_COOKIES_PATH=/path/to/cookies.txt
```

**Cost Estimation:**
- API calls: Free (yt-dlp uses public API)
- Transcription: Always (diarized-transcriber, never subtitles)
- Estimates ~3 tokens/second of speech

**Implementation Details:**

```python
class YouTubePlugin(IngestionPlugin):
    def __init__(self, cookies_path: Optional[Path] = None):
        self.cookies_path = cookies_path
        self.ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        if cookies_path:
            self.ydl_opts['cookiefile'] = str(cookies_path)

    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        # Extract video ID
        video_id = extract_video_id(source_url)

        # Use yt-dlp to get info
        with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)

        return ContentMetadata(
            content_id=video_id,
            title=info['title'],
            url=source_url,
            description=info.get('description'),
            author=info.get('uploader'),
            published_at=info.get('upload_date'),
            duration_seconds=info.get('duration'),
            extra={
                'view_count': info.get('view_count'),
                'like_count': info.get('like_count'),
                'channel_id': info.get('channel_id'),
            }
        )

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        # Always download audio — subtitles are never used (no speaker info)
        audio_path = await self._download_audio(metadata.content_id)
        return ContentResult(
            text=str(audio_path),
            source='diarized_transcriber',
            needs_transcription=True  # Always True
        )
```

### RSS Plugin (Planned)

**Source Type:** `rss`

**Supported URLs:**
- RSS 2.0 feeds
- Atom feeds

**Features:**
- Feed parsing
- Item metadata extraction
- HTML content extraction
- Subscription support (list new items since last check)

### Podcast Plugin (Planned)

**Source Type:** `podcast`

**Features:**
- Podcast RSS feed parsing
- Episode metadata extraction
- MP3 download
- Integration with transcription service

## Advanced Features

### Custom Chunking

Override `chunk_content()` for source-specific chunking:

```python
def chunk_content(self, content: ContentResult, metadata: ContentMetadata,
                 target_tokens: int = 500) -> List[Dict]:
    """Custom chunking that preserves video timestamps."""
    chunks = []

    for segment in content.segments:
        # Each segment becomes its own chunk
        chunk = {
            'text': segment['text'],
            'metadata': {
                'start': segment['start'],
                'end': segment['end'],
                'video_id': metadata.content_id,
                'video_url': metadata.url,
                'video_title': metadata.title,
            }
        }
        chunks.append(chunk)

    return chunks
```

### URL Validation

Override `validate_url()` to pre-filter invalid URLs:

```python
def validate_url(self, url: str) -> bool:
    """Check if URL matches expected pattern."""
    import re
    patterns = [
        r'https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'https?://youtu\.be/[\w-]+',
    ]
    return any(re.match(p, url) for p in patterns)
```

### Async Operations

All I/O operations should be async:

```python
import httpx

async def _api_call(self, endpoint: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{self.base_url}{endpoint}")
        response.raise_for_status()
        return response.json()
```

### Error Handling

Return `None` for failures, log errors:

```python
async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
    try:
        # ... implementation ...
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.warning(f"Content not found: {source_url}")
        else:
            logger.error(f"HTTP error fetching metadata: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return None
```

### Retry Logic

Use retry decorator for transient failures:

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10)
)
async def _api_call(self, endpoint: str) -> dict:
    # ... implementation ...
```

## Plugin Discovery (Future)

Future versions will support dynamic plugin loading:

```python
# Load plugins from directory
plugin_dir = Path("~/.curator/plugins")
for plugin_file in plugin_dir.glob("*.py"):
    spec = importlib.util.spec_from_file_location(plugin_file.stem, plugin_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Find IngestionPlugin subclasses
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, IngestionPlugin) and obj != IngestionPlugin:
            plugin = obj()
            PLUGINS[plugin.source_type] = obj
```

## Best Practices

1. **Idempotency**: Multiple calls with same URL should produce same result
2. **Efficiency**: Fetch metadata should be lightweight (no downloads)
3. **Error Messages**: Provide clear error messages for common failures
4. **Logging**: Use structured logging with context
5. **Testing**: Test both happy path and error cases
6. **Documentation**: Document URL patterns and configuration options
7. **Cost Awareness**: Accurately estimate costs before processing
8. **Async**: Use async/await for all I/O operations
9. **Cleanup**: Delete temporary files in finally blocks
10. **Security**: Validate and sanitize all inputs

## Plugin Checklist

- [ ] Inherits from `IngestionPlugin`
- [ ] Implements all abstract methods
- [ ] Returns `None` on errors (doesn't raise)
- [ ] Uses async for I/O operations
- [ ] Includes retry logic for transient failures
- [ ] Provides accurate cost estimates
- [ ] Validates URLs before processing
- [ ] Uses structured logging
- [ ] Cleans up temporary files
- [ ] Includes unit tests
- [ ] Documents configuration options
- [ ] Handles rate limiting
- [ ] Supports both text and audio content

## Troubleshooting

### Plugin Not Found

```python
ValueError: Unknown source type: my_source
```

**Solution:** Ensure plugin is registered in `PLUGINS` dict in `__init__.py`

### Import Errors

```python
ImportError: cannot import name 'MySourcePlugin'
```

**Solution:** Check plugin file is in correct directory and class is exported

### Metadata Fetch Fails

```python
# Returns None
metadata = await plugin.fetch_metadata(url)
```

**Solution:** Check logs for error details, validate URL format

### Content Fetch Times Out

**Solution:** Increase timeout in plugin configuration or implement streaming download

## Example: Complete Blog Plugin

See `examples/blog_plugin.py` for a complete example of a blog/article plugin that:
- Extracts article content from HTML
- Handles multiple URL patterns
- Uses readability for content extraction
- Supports RSS feed discovery
- Estimates reading time
