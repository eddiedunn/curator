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
        self._task: asyncio.Task | None = None
        # Reset any items stuck in 'processing' from a previous crashed run
        self.storage._reset_stuck_visual_context_items()

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

    async def start(self):
        """Start the daemon as a background asyncio task (for use inside a running event loop).

        This method is designed to be called from an async context (e.g. FastAPI lifespan).
        It starts the APScheduler on the current event loop and records actual running state.
        """
        logger.info("Starting subscription daemon (async)")

        # Schedule subscription checks on the current event loop's scheduler
        self.scheduler.add_job(
            self._check_subscriptions,
            trigger=IntervalTrigger(seconds=self.settings.check_interval),
            id="check_subscriptions",
            name="Check subscriptions for new content",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._enrich_visual_context,
            trigger=IntervalTrigger(seconds=self.settings.visual_context_enrich_interval_seconds),
            id="enrich_visual_context",
            name="Enrich completed items with visual context",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._purge_expired_content,
            trigger=IntervalTrigger(hours=self.settings.purge_interval_hours),
            id="purge_expired_content",
            name="Delete expired content per subscription TTL",
            replace_existing=True,
        )

        self.scheduler.start()
        self.running = True

        logger.info(
            "Daemon started",
            check_interval_seconds=self.settings.check_interval,
            enrich_interval_seconds=self.settings.visual_context_enrich_interval_seconds,
            purge_interval_hours=self.settings.purge_interval_hours,
        )

    async def stop(self):
        """Stop the daemon gracefully (for use inside a running event loop)."""
        if self.running:
            logger.info("Stopping daemon")
            self.scheduler.shutdown(wait=False)
            self.running = False
            logger.info("Daemon stopped")

    def run(self):
        """Start the daemon in standalone mode (its own event loop).

        This is the entry point when running the daemon as a separate process.
        """
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

        self.scheduler.add_job(
            self._enrich_visual_context,
            trigger=IntervalTrigger(seconds=self.settings.visual_context_enrich_interval_seconds),
            id="enrich_visual_context",
            name="Enrich completed items with visual context",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._purge_expired_content,
            trigger=IntervalTrigger(hours=self.settings.purge_interval_hours),
            id="purge_expired_content",
            name="Delete expired content per subscription TTL",
            replace_existing=True,
        )

        logger.info(
            "Daemon configured",
            check_interval_seconds=self.settings.check_interval,
            enrich_interval_seconds=self.settings.visual_context_enrich_interval_seconds,
            purge_interval_hours=self.settings.purge_interval_hours,
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

    async def _enrich_visual_context(self):
        """Background job: enrich completed YouTube items with VLM visual context."""
        import httpx
        from curator import glimpse_client

        logger.debug("Running visual context enrichment job")

        try:
            items = self.storage.get_items_pending_visual_context(
                max_attempts=self.settings.glimpse_max_attempts,
                limit=self.settings.visual_context_batch_size,
            )
        except Exception as exc:
            logger.error("Failed to fetch visual context queue", error=str(exc))
            return

        if not items:
            logger.debug("No items pending visual context enrichment")
            return

        logger.info("Visual context enrichment starting", count=len(items))

        for item in items:
            item_id = item["id"]
            source_id = item["source_id"]  # YouTube video ID
            attempts = item.get("visual_context_attempts", 0) + 1

            self.storage.update_visual_context_status(item_id, "processing", attempts)

            log = logger.bind(item_id=item_id, video_id=source_id, attempt=attempts)

            try:
                # Fetch the Engram record to get duration + transcript segments
                async with httpx.AsyncClient(timeout=10.0) as client:
                    engram_url = self.settings.engram_api_url
                    r = await client.get(
                        f"{engram_url}/api/v1/content/{source_id}"
                    )
                    if r.status_code == 404:
                        log.warning("engram_content_not_found")
                        self.storage.update_visual_context_status(item_id, "failed", attempts)
                        continue
                    r.raise_for_status()
                    engram_data = r.json()

                metadata = engram_data.get("metadata") or {}
                duration = float(metadata.get("duration_seconds", 0))
                segments = metadata.get("segments") or []
                segment_starts = [float(s["start"]) for s in segments if "start" in s]

                # Select frames via Glimpse
                selected = await glimpse_client.select_frames(
                    video_id=source_id,
                    duration_seconds=duration,
                    segment_timestamps=segment_starts,
                    glimpse_url=self.settings.glimpse_service_url,
                    max_frames=self.settings.glimpse_max_frames,
                    scene_threshold=self.settings.glimpse_scene_threshold,
                    proximity_seconds=self.settings.glimpse_proximity_seconds,
                    timeout_seconds=self.settings.glimpse_select_timeout_seconds,
                    fallback_interval_seconds=self.settings.glimpse_frame_interval_seconds,
                )

                if not selected:
                    log.info("no_frames_selected_skipping")
                    self.storage.update_visual_context_status(item_id, "complete", attempts)
                    continue

                # Collect captions / OCR for each selected frame
                frames = await glimpse_client.collect_visual_context(
                    video_id=source_id,
                    selected=selected,
                    glimpse_url=self.settings.glimpse_service_url,
                    timeout_seconds=self.settings.glimpse_timeout_seconds,
                )

                # PATCH Engram metadata
                from datetime import timezone
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                patch_payload = {
                    "visual_context_generated_at": now_iso,
                    "visual_context": {
                        "model": "glimpse",
                        "frame_count": len(frames),
                        "selection": {
                            "strategy": "segment+scene_change",
                            "scene_threshold": self.settings.glimpse_scene_threshold,
                            "proximity_seconds": self.settings.glimpse_proximity_seconds,
                        },
                        "frames": frames,
                    },
                }

                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.patch(
                        f"{self.settings.engram_api_url}/api/v1/content/{source_id}",
                        json={"metadata": patch_payload},
                    )
                    r.raise_for_status()

                self.storage.update_visual_context_status(item_id, "complete", attempts)
                log.info("visual_context_enrichment_complete", frame_count=len(frames))

            except Exception as exc:
                log.error("visual_context_enrichment_failed", error=str(exc))
                status = "failed" if attempts >= self.settings.glimpse_max_attempts else "failed"
                self.storage.update_visual_context_status(item_id, status, attempts)

    async def _purge_expired_content(self):
        """Delete completed items that have exceeded their subscription's content_ttl_days."""
        logger.debug("Running content expiration purge")
        try:
            deleted = self.storage.delete_expired_content()
            if deleted:
                logger.info("Purged expired content items", deleted=deleted)
            else:
                logger.debug("No expired content to purge")
        except Exception as exc:
            logger.error("Content expiration purge failed", error=str(exc))
