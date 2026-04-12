"""SQLite storage layer for Curator service."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import json
import structlog

from curator.models import (
    SubscriptionStatus,
    SubscriptionType,
    IngestionStatus,
)

logger = structlog.get_logger()


class CuratorStorage:
    """SQLite storage for Curator service.

    Manages:
    - Subscriptions (channels, feeds to monitor)
    - Ingested items (content that's been processed)
    - Fetch jobs (tracking ingestion progress)
    """

    def __init__(self, database_path: str = "./curator.db"):
        """Initialize storage with database path."""
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Create database tables if they don't exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Subscriptions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    subscription_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    check_frequency_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_checked_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT DEFAULT '{}',
                    visual_context_enabled BOOLEAN NOT NULL DEFAULT 0
                )
            """)

            # Ingested items table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ingested_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    published_at TIMESTAMP,
                    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    metadata TEXT DEFAULT '{}',
                    visual_context_status TEXT,
                    visual_context_attempts INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
                    UNIQUE(source_type, source_id)
                )
            """)

            # Fetch jobs table (for tracking ingestion progress)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fetch_jobs (
                    id TEXT PRIMARY KEY,
                    item_id INTEGER,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    error_message TEXT,
                    content_id TEXT,
                    FOREIGN KEY (item_id) REFERENCES ingested_items(id) ON DELETE CASCADE
                )
            """)

            # Run migrations for existing databases
            self._migrate_schema(cursor)

            # Indexes for common queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_subscriptions_enabled
                ON subscriptions(enabled, status)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_items_source
                ON ingested_items(source_type, source_id)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_items_subscription
                ON ingested_items(subscription_id)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_items_visual_ctx
                ON ingested_items(visual_context_status, status)
            """)

            conn.commit()

    def _migrate_schema(self, cursor):
        """Apply schema migrations for existing databases."""
        # fetch_jobs: content_id column
        cursor.execute("PRAGMA table_info(fetch_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        if 'content_id' not in columns:
            logger.info("Adding content_id column to fetch_jobs table")
            cursor.execute("ALTER TABLE fetch_jobs ADD COLUMN content_id TEXT")

        # subscriptions: visual_context_enabled column
        cursor.execute("PRAGMA table_info(subscriptions)")
        sub_cols = {row[1] for row in cursor.fetchall()}
        if 'visual_context_enabled' not in sub_cols:
            logger.info("Adding visual_context_enabled column to subscriptions table")
            cursor.execute(
                "ALTER TABLE subscriptions ADD COLUMN "
                "visual_context_enabled BOOLEAN NOT NULL DEFAULT 0"
            )

        # ingested_items: visual_context columns
        cursor.execute("PRAGMA table_info(ingested_items)")
        item_cols = {row[1] for row in cursor.fetchall()}
        if 'visual_context_status' not in item_cols:
            logger.info("Adding visual_context_status column to ingested_items table")
            cursor.execute("ALTER TABLE ingested_items ADD COLUMN visual_context_status TEXT")
        if 'visual_context_attempts' not in item_cols:
            logger.info("Adding visual_context_attempts column to ingested_items table")
            cursor.execute(
                "ALTER TABLE ingested_items ADD COLUMN "
                "visual_context_attempts INTEGER NOT NULL DEFAULT 0"
            )

    @contextmanager
    def _get_connection(self):
        """Get database connection context manager."""
        conn = sqlite3.connect(str(self.database_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # Subscription methods

    def create_subscription(
        self,
        name: str,
        subscription_type: SubscriptionType,
        source_url: str,
        check_frequency_minutes: int = 60,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        visual_context_enabled: bool = False,
    ) -> int:
        """Create a new subscription."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO subscriptions
                (name, subscription_type, source_url, check_frequency_minutes, enabled, metadata,
                 visual_context_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                subscription_type.value,
                source_url,
                check_frequency_minutes,
                enabled,
                json.dumps(metadata or {}),
                visual_context_enabled,
            ))
            conn.commit()
            return cursor.lastrowid

    def get_subscription(self, subscription_id: int) -> Optional[Dict[str, Any]]:
        """Get subscription by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM subscriptions WHERE id = ?
            """, (subscription_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def list_subscriptions(
        self,
        enabled_only: bool = False,
        subscription_type: Optional[SubscriptionType] = None,
    ) -> List[Dict[str, Any]]:
        """List subscriptions with optional filters."""
        query = "SELECT * FROM subscriptions WHERE 1=1"
        params = []

        if enabled_only:
            query += " AND enabled = 1"

        if subscription_type:
            query += " AND subscription_type = ?"
            params.append(subscription_type.value)

        query += " ORDER BY created_at DESC"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def update_subscription(
        self,
        subscription_id: int,
        **kwargs,
    ) -> bool:
        """Update subscription fields."""
        if not kwargs:
            return False

        # Handle metadata separately (needs JSON encoding)
        if 'metadata' in kwargs:
            kwargs['metadata'] = json.dumps(kwargs['metadata'])

        # Always update updated_at
        kwargs['updated_at'] = datetime.now().isoformat()

        set_clause = ", ".join(f"{key} = ?" for key in kwargs.keys())
        values = list(kwargs.values()) + [subscription_id]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE subscriptions SET {set_clause}
                WHERE id = ?
            """, values)
            conn.commit()
            return cursor.rowcount > 0

    def delete_subscription(self, subscription_id: int) -> bool:
        """Delete a subscription."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_subscriptions_due_for_check(self) -> List[Dict[str, Any]]:
        """Get subscriptions that are due for checking."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM subscriptions
                WHERE enabled = 1
                AND status = 'active'
                AND (
                    last_checked_at IS NULL
                    OR datetime(last_checked_at, '+' || check_frequency_minutes || ' minutes') <= datetime('now')
                )
                ORDER BY last_checked_at ASC NULLS FIRST
            """)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    # Ingested items methods

    def create_ingested_item(
        self,
        source_type: str,
        source_id: str,
        source_url: str,
        title: str,
        author: Optional[str] = None,
        published_at: Optional[datetime] = None,
        subscription_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Create a new ingested item. Returns None if already exists."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Handle published_at: accept both datetime objects and ISO strings
                published_at_value = None
                if published_at:
                    if isinstance(published_at, str):
                        published_at_value = published_at
                    else:
                        published_at_value = published_at.isoformat()

                cursor.execute("""
                    INSERT INTO ingested_items
                    (subscription_id, source_type, source_id, source_url, title, author, published_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    subscription_id,
                    source_type,
                    source_id,
                    source_url,
                    title,
                    author,
                    published_at_value,
                    json.dumps(metadata or {}),
                ))
                conn.commit()
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Item already exists
            return None

    def get_ingested_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Get ingested item by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ingested_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def get_ingested_item_by_source(
        self,
        source_type: str,
        source_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get ingested item by source type and ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM ingested_items
                WHERE source_type = ? AND source_id = ?
            """, (source_type, source_id))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def list_ingested_items(
        self,
        subscription_id: Optional[int] = None,
        source_type: Optional[str] = None,
        status: Optional[IngestionStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List ingested items with optional filters."""
        query = "SELECT * FROM ingested_items WHERE 1=1"
        params = []

        if subscription_id is not None:
            query += " AND subscription_id = ?"
            params.append(subscription_id)

        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)

        if status:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY ingested_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count_ingested_items(self) -> int:
        """Count total ingested items."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ingested_items")
            return cursor.fetchone()[0]

    def get_ingested_item_counts_by_status(self) -> dict:
        """Return a dict of {status: count} for all ingested item statuses."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, COUNT(*) as count
                FROM ingested_items
                GROUP BY status
            """)
            return {row["status"]: row["count"] for row in cursor.fetchall()}

    def update_ingested_item(
        self,
        item_id: int,
        **kwargs,
    ) -> bool:
        """Update ingested item fields."""
        if not kwargs:
            return False

        # Handle metadata separately
        if 'metadata' in kwargs:
            kwargs['metadata'] = json.dumps(kwargs['metadata'])

        set_clause = ", ".join(f"{key} = ?" for key in kwargs.keys())
        values = list(kwargs.values()) + [item_id]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE ingested_items SET {set_clause}
                WHERE id = ?
            """, values)
            conn.commit()
            return cursor.rowcount > 0

    # Fetch job methods

    def create_fetch_job(self, job_id: str, source_url: str, item_id: Optional[int] = None) -> bool:
        """Create a new fetch job."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO fetch_jobs (id, item_id, source_url, started_at)
                VALUES (?, ?, ?, ?)
            """, (job_id, item_id, source_url, datetime.now().isoformat()))
            conn.commit()
            return True

    def get_fetch_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get fetch job by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM fetch_jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def update_fetch_job(self, job_id: str, **kwargs) -> bool:
        """Update fetch job fields."""
        if not kwargs:
            return False

        set_clause = ", ".join(f"{key} = ?" for key in kwargs.keys())
        values = list(kwargs.values()) + [job_id]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE fetch_jobs SET {set_clause}
                WHERE id = ?
            """, values)
            conn.commit()
            return cursor.rowcount > 0

    # Visual context methods

    def get_items_pending_visual_context(
        self, max_attempts: int, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Return completed YouTube items eligible for visual context enrichment."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT i.*
                FROM ingested_items i
                JOIN subscriptions s ON s.id = i.subscription_id
                WHERE s.visual_context_enabled = 1
                  AND i.source_type = 'youtube'
                  AND i.status = 'completed'
                  AND (
                    i.visual_context_status IS NULL
                    OR (i.visual_context_status = 'failed' AND i.visual_context_attempts < ?)
                  )
                ORDER BY i.ingested_at ASC
                LIMIT ?
            """, (max_attempts, limit))
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def update_visual_context_status(
        self, item_id: int, status: str, attempts: int
    ) -> bool:
        """Update visual_context_status and visual_context_attempts for an item."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ingested_items
                SET visual_context_status = ?, visual_context_attempts = ?
                WHERE id = ?
            """, (status, attempts, item_id))
            conn.commit()
            return cursor.rowcount > 0

    def _reset_stuck_visual_context_items(self) -> None:
        """Reset any items left in 'processing' state (e.g. from a crashed daemon)."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE ingested_items SET visual_context_status = NULL "
                "WHERE visual_context_status = 'processing'"
            )
            conn.commit()

    def reset_visual_context_items(
        self,
        subscription_id: int | None = None,
        all_failed: bool = False,
    ) -> int:
        """Reset visual_context_status and attempts for failed items.

        Returns the count of reset items.
        """
        if not all_failed and subscription_id is None:
            raise ValueError("Must specify subscription_id or all_failed=True")

        query = """
            UPDATE ingested_items
            SET visual_context_status = NULL, visual_context_attempts = 0
            WHERE visual_context_status = 'failed'
        """
        params: list = []
        if subscription_id is not None:
            query += " AND subscription_id = ?"
            params.append(subscription_id)

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount

    # Utility methods

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert SQLite row to dictionary."""
        result = dict(row)
        # Parse JSON fields
        if 'metadata' in result and result['metadata']:
            try:
                result['metadata'] = json.loads(result['metadata'])
            except json.JSONDecodeError:
                result['metadata'] = {}
        return result

    def health_check(self) -> bool:
        """Check if database is accessible."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False
