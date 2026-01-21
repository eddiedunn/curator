"""Tests for storage layer."""

import pytest
from pathlib import Path
import tempfile

from curator.storage import CuratorStorage
from curator.models import SubscriptionType, SubscriptionStatus


@pytest.fixture
def storage():
    """Create a temporary storage instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield CuratorStorage(str(db_path))


def test_create_subscription(storage):
    """Test creating a subscription."""
    sub_id = storage.create_subscription(
        name="Test Channel",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://youtube.com/@test",
        check_frequency_minutes=60,
    )

    assert sub_id > 0

    # Verify subscription was created
    sub = storage.get_subscription(sub_id)
    assert sub is not None
    assert sub["name"] == "Test Channel"
    assert sub["subscription_type"] == "youtube_channel"


def test_list_subscriptions(storage):
    """Test listing subscriptions."""
    # Create multiple subscriptions
    storage.create_subscription(
        name="Channel 1",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://youtube.com/@test1",
    )
    storage.create_subscription(
        name="Channel 2",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://youtube.com/@test2",
    )

    subs = storage.list_subscriptions()
    assert len(subs) == 2


def test_update_subscription(storage):
    """Test updating a subscription."""
    sub_id = storage.create_subscription(
        name="Test",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://youtube.com/@test",
    )

    # Update subscription
    success = storage.update_subscription(sub_id, name="Updated Name")
    assert success

    # Verify update
    sub = storage.get_subscription(sub_id)
    assert sub["name"] == "Updated Name"


def test_delete_subscription(storage):
    """Test deleting a subscription."""
    sub_id = storage.create_subscription(
        name="Test",
        subscription_type=SubscriptionType.YOUTUBE_CHANNEL,
        source_url="https://youtube.com/@test",
    )

    # Delete subscription
    success = storage.delete_subscription(sub_id)
    assert success

    # Verify deletion
    sub = storage.get_subscription(sub_id)
    assert sub is None


def test_create_ingested_item(storage):
    """Test creating an ingested item."""
    item_id = storage.create_ingested_item(
        source_type="youtube",
        source_id="test123",
        source_url="https://youtube.com/watch?v=test123",
        title="Test Video",
        author="Test Author",
    )

    assert item_id is not None

    # Verify item was created
    item = storage.get_ingested_item(item_id)
    assert item is not None
    assert item["title"] == "Test Video"


def test_duplicate_ingested_item(storage):
    """Test that duplicate items are not created."""
    # Create first item
    item_id1 = storage.create_ingested_item(
        source_type="youtube",
        source_id="test123",
        source_url="https://youtube.com/watch?v=test123",
        title="Test Video",
    )

    # Try to create duplicate
    item_id2 = storage.create_ingested_item(
        source_type="youtube",
        source_id="test123",
        source_url="https://youtube.com/watch?v=test123",
        title="Test Video",
    )

    assert item_id1 is not None
    assert item_id2 is None


def test_health_check(storage):
    """Test database health check."""
    assert storage.health_check() is True
