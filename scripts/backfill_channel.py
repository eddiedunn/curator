#!/usr/bin/env python3
"""Backfill all videos from a YouTube channel into Curator.

This script:
1. Extracts all video URLs from a YouTube channel using yt-dlp
2. Queues fetch jobs for each video via the Curator API
3. Monitors progress and reports results

Usage:
    python backfill_channel.py https://www.youtube.com/@indydevdan
    python backfill_channel.py https://www.youtube.com/@indydevdan --limit 10
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Dict, Any
import httpx
import yt_dlp
import structlog

logger = structlog.get_logger()


class ChannelBackfiller:
    """Backfill all videos from a YouTube channel."""

    def __init__(self, curator_api_url: str = "http://localhost:8950"):
        """Initialize backfiller with Curator API URL."""
        self.curator_api_url = curator_api_url

    def get_channel_videos(self, channel_url: str, limit: int = None) -> List[Dict[str, Any]]:
        """Get all video URLs from a YouTube channel.

        Args:
            channel_url: YouTube channel URL (@username or /c/channel format)
            limit: Optional limit on number of videos to fetch

        Returns:
            List of video info dicts with 'id', 'title', 'url', 'upload_date'
        """
        logger.info("Fetching channel videos", channel_url=channel_url, limit=limit)

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,  # Don't download, just get metadata
            'playlistend': limit,  # Limit number of videos
        }

        videos = []

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract channel info
                info = ydl.extract_info(channel_url, download=False)

                if not info:
                    logger.error("Failed to extract channel info")
                    return []

                # Get video entries
                entries = info.get('entries', [])

                for entry in entries:
                    if not entry:
                        continue

                    video_id = entry.get('id')
                    if not video_id:
                        continue

                    videos.append({
                        'id': video_id,
                        'title': entry.get('title', 'Unknown'),
                        'url': f"https://www.youtube.com/watch?v={video_id}",
                        'upload_date': entry.get('upload_date'),
                    })

                logger.info("Found videos", count=len(videos))
                return videos

        except Exception as e:
            logger.error("Error fetching channel videos", error=str(e))
            return []

    async def queue_fetch_job(self, video_url: str) -> Dict[str, Any]:
        """Queue a fetch job for a single video.

        Args:
            video_url: YouTube video URL

        Returns:
            Fetch job response dict
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.curator_api_url}/api/v1/fetch",
                json={"source_url": video_url}
            )
            response.raise_for_status()
            return response.json()

    async def check_job_status(self, job_id: str) -> Dict[str, Any]:
        """Check status of a fetch job.

        Args:
            job_id: Job ID to check

        Returns:
            Job status response dict
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self.curator_api_url}/api/v1/fetch/{job_id}"
            )
            response.raise_for_status()
            return response.json()

    async def backfill_channel(
        self,
        channel_url: str,
        limit: int = None,
        batch_size: int = 5,
        wait_for_completion: bool = False
    ) -> Dict[str, Any]:
        """Backfill all videos from a channel.

        Args:
            channel_url: YouTube channel URL
            limit: Optional limit on number of videos
            batch_size: Number of concurrent fetch jobs
            wait_for_completion: If True, wait for all jobs to complete

        Returns:
            Dict with summary statistics
        """
        # Get all videos from channel
        videos = self.get_channel_videos(channel_url, limit=limit)

        if not videos:
            logger.warning("No videos found")
            return {
                'total_videos': 0,
                'queued': 0,
                'failed': 0,
                'completed': 0,
            }

        logger.info(f"Starting backfill for {len(videos)} videos")

        # Queue fetch jobs in batches
        job_ids = []
        failed_videos = []

        for i in range(0, len(videos), batch_size):
            batch = videos[i:i + batch_size]

            logger.info(
                f"Queueing batch {i // batch_size + 1}/{(len(videos) + batch_size - 1) // batch_size}",
                batch_size=len(batch)
            )

            # Queue jobs concurrently within batch
            tasks = [
                self.queue_fetch_job(video['url'])
                for video in batch
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for video, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.error(
                        "Failed to queue job",
                        video_id=video['id'],
                        title=video['title'],
                        error=str(result)
                    )
                    failed_videos.append(video)
                else:
                    job_ids.append(result['job_id'])
                    logger.info(
                        "Queued job",
                        video_id=video['id'],
                        title=video['title'],
                        job_id=result['job_id']
                    )

            # Small delay between batches to avoid overwhelming the API
            if i + batch_size < len(videos):
                await asyncio.sleep(1)

        summary = {
            'total_videos': len(videos),
            'queued': len(job_ids),
            'failed': len(failed_videos),
        }

        # Optionally wait for all jobs to complete
        if wait_for_completion and job_ids:
            logger.info("Waiting for jobs to complete", total_jobs=len(job_ids))
            completed = await self._wait_for_jobs(job_ids)
            summary['completed'] = completed

        return summary

    async def _wait_for_jobs(
        self,
        job_ids: List[str],
        poll_interval: int = 5,
        max_wait: int = 3600
    ) -> int:
        """Wait for all jobs to complete.

        Args:
            job_ids: List of job IDs to monitor
            poll_interval: Seconds between status checks
            max_wait: Maximum seconds to wait

        Returns:
            Number of completed jobs
        """
        pending_jobs = set(job_ids)
        completed_count = 0
        failed_count = 0
        elapsed = 0

        while pending_jobs and elapsed < max_wait:
            # Check status of all pending jobs
            for job_id in list(pending_jobs):
                try:
                    status = await self.check_job_status(job_id)

                    if status['status'] == 'completed':
                        pending_jobs.remove(job_id)
                        completed_count += 1
                        logger.info(
                            "Job completed",
                            job_id=job_id,
                            completed=completed_count,
                            remaining=len(pending_jobs)
                        )
                    elif status['status'] == 'failed':
                        pending_jobs.remove(job_id)
                        failed_count += 1
                        logger.error(
                            "Job failed",
                            job_id=job_id,
                            error=status.get('message', 'Unknown error')
                        )

                except Exception as e:
                    logger.error("Error checking job status", job_id=job_id, error=str(e))

            if pending_jobs:
                logger.info(
                    "Waiting for jobs",
                    completed=completed_count,
                    failed=failed_count,
                    pending=len(pending_jobs)
                )
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        if pending_jobs:
            logger.warning(
                "Timeout waiting for jobs",
                still_pending=len(pending_jobs)
            )

        return completed_count


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill all videos from a YouTube channel into Curator"
    )
    parser.add_argument(
        "channel_url",
        help="YouTube channel URL (e.g., https://www.youtube.com/@indydevdan)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of videos to backfill"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of concurrent fetch jobs (default: 5)"
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for all jobs to complete before exiting"
    )
    parser.add_argument(
        "--curator-url",
        default="http://localhost:8950",
        help="Curator API URL (default: http://localhost:8950)"
    )

    args = parser.parse_args()

    # Configure logging
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
        ]
    )

    backfiller = ChannelBackfiller(curator_api_url=args.curator_url)

    try:
        summary = await backfiller.backfill_channel(
            channel_url=args.channel_url,
            limit=args.limit,
            batch_size=args.batch_size,
            wait_for_completion=args.wait
        )

        logger.info("Backfill complete", **summary)

        print("\n" + "="*60)
        print("BACKFILL SUMMARY")
        print("="*60)
        print(f"Total videos found:  {summary['total_videos']}")
        print(f"Jobs queued:         {summary['queued']}")
        print(f"Failed to queue:     {summary['failed']}")
        if 'completed' in summary:
            print(f"Jobs completed:      {summary['completed']}")
        print("="*60)

        # Exit with error code if any jobs failed to queue
        if summary['failed'] > 0:
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error("Backfill failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
