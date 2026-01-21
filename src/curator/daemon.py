"""Background daemon for subscription monitoring."""

import asyncio
import fcntl
import signal
import sys
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import structlog

from curator.storage import CuratorStorage
from curator.config import CuratorSettings
from curator.orchestrator import IngestionOrchestrator
from curator.models import SubscriptionStatus

logger = structlog.get_logger()


class SubscriptionDaemon:
    """Daemon that monitors subscriptions and triggers ingestion for new content."""

    def __init__(self, storage: CuratorStorage, settings: CuratorSettings):
        """Initialize daemon with storage and settings."""
        self.storage = storage
        self.settings = settings
        self.orchestrator = IngestionOrchestrator(storage, settings)
        self.scheduler = AsyncIOScheduler()
        self.running = False
        self._lock_file = None

    def _acquire_lock(self):
        """Acquire file lock to ensure single instance."""
        lock_path = Path(self.settings.data_dir) / "daemon.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock_file = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            logger.info("Acquired daemon lock", lock_path=str(lock_path))
        except BlockingIOError:
            raise RuntimeError("Another daemon instance is already running")

    def run(self):
        """Start the daemon."""
        logger.info("Starting subscription daemon")

        # Acquire lock to ensure single instance
        self._acquire_lock()

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Schedule subscription checks
        self.scheduler.add_job(
            self._check_subscriptions,
            trigger=IntervalTrigger(seconds=self.settings.check_interval),
            id="check_subscriptions",
            name="Check subscriptions for new content",
            replace_existing=True,
        )

        logger.info(
            "Daemon configured",
            check_interval_seconds=self.settings.check_interval,
        )

        # Start scheduler and event loop
        # AsyncIOScheduler needs a running event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Define startup callback to start scheduler once loop is running
        def start_scheduler():
            self.scheduler.start()
            self.running = True
            logger.info("Daemon started")

        # Schedule startup callback
        loop.call_soon(start_scheduler)

        # Keep running until interrupted
        try:
            loop.run_forever()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Daemon shutting down")
            self.shutdown()
        finally:
            loop.close()

    def shutdown(self):
        """Shutdown the daemon."""
        if self.running:
            logger.info("Shutting down daemon")
            self.scheduler.shutdown(wait=True)
            self.running = False

        # Release lock file
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
                logger.info("Released daemon lock")
            except Exception as e:
                logger.warning("Error releasing lock", error=str(e))

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Received signal", signal=signum)
        self.shutdown()
        sys.exit(0)

    async def _check_subscriptions(self):
        """Check all subscriptions for new content."""
        logger.debug("Checking subscriptions")

        try:
            # Get subscriptions due for checking
            subscriptions = self.storage.get_subscriptions_due_for_check()

            if not subscriptions:
                logger.debug("No subscriptions due for check")
                return

            logger.info("Found subscriptions to check", count=len(subscriptions))

            # Process each subscription
            for sub in subscriptions:
                await self._process_subscription(sub)

        except Exception as e:
            logger.error("Error checking subscriptions", error=str(e))

    async def _process_subscription(self, subscription: dict):
        """Process a single subscription to check for new content.

        Args:
            subscription: Subscription dictionary from storage
        """
        sub_id = subscription["id"]
        sub_name = subscription["name"]
        sub_type = subscription["subscription_type"]
        source_url = subscription["source_url"]

        logger.info("Processing subscription", subscription_id=sub_id, name=sub_name, type=sub_type)

        try:
            # Update last_checked_at
            self.storage.update_subscription(
                sub_id,
                last_checked_at=datetime.now().isoformat(),
            )

            # Get plugin for this subscription
            plugin = self.orchestrator._get_plugin_for_url(source_url)
            if not plugin:
                error_msg = f"No plugin found for URL: {source_url}"
                logger.error(error_msg, subscription_id=sub_id)
                self.storage.update_subscription(
                    sub_id,
                    status=SubscriptionStatus.ERROR.value,
                    last_error=error_msg,
                )
                return

            # Handle YouTube channels differently - fetch all videos from channel
            if sub_type == "youtube_channel":
                await self._process_youtube_channel(sub_id, source_url, plugin)
            else:
                # For single-item subscriptions (like individual videos, RSS items)
                # Check if this content is already ingested
                metadata = await plugin.fetch_metadata(source_url)
                if not metadata:
                    logger.warning("Failed to fetch metadata", subscription_id=sub_id)
                    return

                existing_item = self.storage.get_ingested_item_by_source(
                    plugin.source_type,
                    metadata.content_id,
                )

                if existing_item:
                    logger.debug(
                        "Content already ingested",
                        subscription_id=sub_id,
                        content_id=metadata.content_id,
                    )
                    return

                # New content - trigger ingestion
                logger.info(
                    "New content found, triggering ingestion",
                    subscription_id=sub_id,
                    content_id=metadata.content_id,
                    title=metadata.title,
                )

                await self.orchestrator.ingest_url(
                    source_url,
                    subscription_id=sub_id,
                )

            # Update subscription status
            self.storage.update_subscription(
                sub_id,
                status=SubscriptionStatus.ACTIVE.value,
                last_error=None,
            )

        except Exception as e:
            error_msg = f"Error processing subscription: {str(e)}"
            logger.error(error_msg, subscription_id=sub_id)
            self.storage.update_subscription(
                sub_id,
                status=SubscriptionStatus.ERROR.value,
                last_error=error_msg,
            )

    async def _process_youtube_channel(self, sub_id: int, channel_url: str, plugin):
        """Process a YouTube channel subscription - fetch all videos from channel.

        Args:
            sub_id: Subscription ID
            channel_url: YouTube channel URL
            plugin: YouTube plugin instance
        """
        logger.info("Fetching videos from YouTube channel", subscription_id=sub_id, url=channel_url)

        # Fetch video IDs from channel
        video_ids = await plugin.fetch_channel_videos(channel_url, max_videos=200)

        if not video_ids:
            logger.warning("No videos found in channel", subscription_id=sub_id)
            return

        logger.info(
            f"Found {len(video_ids)} videos in channel",
            subscription_id=sub_id,
            total_videos=len(video_ids),
        )

        # Process each video
        new_videos = 0
        for video_id in video_ids:
            # Check if already ingested
            existing_item = self.storage.get_ingested_item_by_source(
                plugin.source_type,
                video_id,
            )

            if existing_item:
                logger.debug(
                    "Video already ingested, skipping",
                    subscription_id=sub_id,
                    video_id=video_id,
                )
                continue

            # New video - trigger ingestion
            video_url = f"https://youtube.com/watch?v={video_id}"

            logger.info(
                "New video found, triggering ingestion",
                subscription_id=sub_id,
                video_id=video_id,
            )

            try:
                await self.orchestrator.ingest_url(
                    video_url,
                    subscription_id=sub_id,
                )
                new_videos += 1

            except Exception as e:
                logger.error(
                    f"Error ingesting video {video_id}: {e}",
                    subscription_id=sub_id,
                    video_id=video_id,
                )
                # Continue with other videos even if one fails

        logger.info(
            f"Processed YouTube channel",
            subscription_id=sub_id,
            total_videos=len(video_ids),
            new_videos=new_videos,
            skipped_videos=len(video_ids) - new_videos,
        )
