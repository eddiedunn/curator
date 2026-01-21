"""RSS/Atom feed plugin for Curator.

This plugin provides content ingestion from RSS and Atom feeds.
It handles:
- Feed parsing with feedparser
- Article extraction with readability-lxml
- Content deduplication
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional, List
from urllib.parse import urlparse

import feedparser
import httpx
from readability import Document

from curator.plugins.base import (
    IngestionPlugin,
    ContentMetadata,
    ContentResult,
    CostEstimate,
)

logger = logging.getLogger(__name__)


class RSSPlugin(IngestionPlugin):
    """Plugin for ingesting RSS/Atom feed content.

    This plugin fetches and parses RSS/Atom feeds, then extracts full article
    content using readability-lxml. It supports both individual article URLs
    and feed URLs.

    Supports:
    - RSS 2.0 feeds
    - Atom feeds
    - Individual article URLs (extracts content directly)
    - Automatic content cleaning with readability
    """

    def __init__(self, user_agent: Optional[str] = None):
        """Initialize RSS plugin.

        Args:
            user_agent: Optional custom user agent for HTTP requests
        """
        self.user_agent = user_agent or "Curator-RSS/1.0"

    @property
    def source_type(self) -> str:
        """Return unique source type identifier."""
        return "rss"

    @property
    def name(self) -> str:
        """Return human-readable plugin name."""
        return "RSS/Atom Feeds"

    def validate_url(self, url: str) -> bool:
        """Check if URL is valid.

        Args:
            url: URL to validate

        Returns:
            True if URL appears to be valid HTTP/HTTPS URL
        """
        try:
            result = urlparse(url)
            return result.scheme in ('http', 'https') and bool(result.netloc)
        except Exception:
            return False

    async def fetch_metadata(self, source_url: str) -> Optional[ContentMetadata]:
        """Fetch metadata about article/feed.

        For article URLs, fetches the page and extracts metadata.
        For feed URLs, returns metadata about the feed itself.

        Args:
            source_url: URL to article or feed

        Returns:
            ContentMetadata with article/feed information, or None if error
        """
        try:
            # Try to fetch the URL
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    source_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=30.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                content = response.text

            # Check if it's a feed or an article
            # feedparser handles both feeds and regular HTML gracefully
            feed = await asyncio.to_thread(feedparser.parse, content)

            # If it's a feed with entries, get metadata from the feed
            if feed.entries:
                # This is a feed URL - return feed metadata
                # For feeds, we'll use the feed URL as content_id
                content_id = self._generate_content_id(source_url)

                return ContentMetadata(
                    content_id=content_id,
                    title=feed.feed.get('title', 'RSS Feed'),
                    url=source_url,
                    description=feed.feed.get('description') or feed.feed.get('subtitle'),
                    author=feed.feed.get('author'),
                    published_at=self._parse_date(feed.feed.get('updated') or feed.feed.get('published')),
                    extra={
                        'feed_url': source_url,
                        'entry_count': len(feed.entries),
                        'is_feed': True,
                    }
                )
            else:
                # This is an article URL - extract article metadata
                doc = Document(content)

                # Generate content ID from URL
                content_id = self._generate_content_id(source_url)

                # Try to extract metadata from HTML
                title = doc.title() or "Unknown Article"

                return ContentMetadata(
                    content_id=content_id,
                    title=title,
                    url=source_url,
                    description=None,  # Could extract meta description if needed
                    author=None,  # readability doesn't extract author
                    published_at=None,  # Could extract from meta tags if needed
                    extra={
                        'is_feed': False,
                    }
                )

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching {source_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching metadata for {source_url}: {e}")
            return None

    async def fetch_content(self, metadata: ContentMetadata) -> Optional[ContentResult]:
        """Fetch the full content for an RSS article.

        Uses readability-lxml to extract clean article content.

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            ContentResult with article text, or None on error
        """
        url = metadata.url

        try:
            # Fetch the article page
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=30.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                html_content = response.text

            # If it's a feed, we can't fetch "content" directly
            # This should be used for individual articles
            if metadata.extra.get('is_feed'):
                logger.warning(f"Cannot fetch content for feed URL: {url}")
                return None

            # Extract article content with readability
            doc = await asyncio.to_thread(Document, html_content)
            article_text = doc.summary()

            # Convert HTML to plain text (basic cleaning)
            # Remove HTML tags
            text = await asyncio.to_thread(self._html_to_text, article_text)

            if not text or len(text.strip()) < 100:
                logger.warning(f"Extracted text too short for {url}")
                return None

            return ContentResult(
                text=text,
                segments=None,
                source="rss_readability",
                needs_transcription=False,
            )

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching content from {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching content from {url}: {e}")
            return None

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text.

        Simple HTML tag removal. For better results, could use
        html2text or similar library.

        Args:
            html: HTML content

        Returns:
            Plain text
        """
        # Remove script and style elements
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()

        return text

    def _generate_content_id(self, url: str) -> str:
        """Generate a stable content ID from URL.

        Args:
            url: Article or feed URL

        Returns:
            Hash-based content ID
        """
        # Use SHA256 hash of URL as content ID
        # This ensures same URL always gets same ID
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _parse_date(self, date_str: Optional[str]) -> Optional[str]:
        """Parse feed date to ISO format.

        Args:
            date_str: Date string from feed

        Returns:
            ISO format date string, or None if parsing fails
        """
        if not date_str:
            return None

        try:
            # feedparser provides parsed date tuple
            if isinstance(date_str, tuple):
                from datetime import datetime
                import time
                dt = datetime(*date_str[:6])
                return dt.isoformat()
            return str(date_str)
        except Exception:
            return None

    def estimate_cost(self, metadata: ContentMetadata) -> CostEstimate:
        """Estimate cost for RSS article ingestion.

        Costs:
        - HTTP fetches: $0 (free)
        - Readability extraction: $0 (free, local processing)
        - Embeddings: $0 (local BAAI model)

        Args:
            metadata: Metadata from fetch_metadata()

        Returns:
            CostEstimate with cost breakdown
        """
        # Estimate article length
        # Average article: ~1000 words = ~1300 tokens
        estimated_tokens = 1300

        # Estimate chunks
        chunk_size = 500  # tokens
        estimated_chunks = max(1, estimated_tokens // chunk_size)

        warnings = []

        # If it's a feed, warn that we can't ingest it directly
        if metadata.extra.get('is_feed'):
            warnings.append("This is a feed URL. Process individual articles instead.")

        return CostEstimate(
            api_calls=1,  # One HTTP fetch
            api_cost_usd=0.0,  # Free
            transcription_minutes=0.0,  # No transcription needed
            transcription_cost_usd=0.0,
            embedding_tokens=estimated_tokens,
            embedding_cost_usd=0.0,  # Local embeddings are free
            total_cost_usd=0.0,  # Everything is local/free
            warnings=warnings if warnings else None,
        )


def list_feed_entries(feed_url: str, max_entries: int = 10) -> List[dict]:
    """Utility function to list entries from an RSS/Atom feed.

    This is a helper function for discovering articles in a feed.
    Not part of the plugin interface, but useful for feed subscriptions.

    Args:
        feed_url: URL to RSS/Atom feed
        max_entries: Maximum number of entries to return

    Returns:
        List of entry dictionaries with title, link, published, etc.
    """
    try:
        feed = feedparser.parse(feed_url)

        entries = []
        for entry in feed.entries[:max_entries]:
            entries.append({
                'title': entry.get('title', 'No title'),
                'link': entry.get('link'),
                'published': entry.get('published') or entry.get('updated'),
                'summary': entry.get('summary'),
                'author': entry.get('author'),
            })

        return entries
    except Exception as e:
        logger.error(f"Error parsing feed {feed_url}: {e}")
        return []
