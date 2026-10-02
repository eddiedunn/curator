"""Tests for API endpoints."""

import pytest
from fastapi.testclient import TestClient

from curator.api import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data


def test_root(client):
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "curator"


def test_list_items_and_status_include_skipped(tmp_path, monkeypatch):
    """A 'skipped' item must not break item listing, and is counted in /api/v1/status."""
    from curator import api
    from curator.storage import CuratorStorage

    storage = CuratorStorage(str(tmp_path / "test.db"))
    item_id = storage.create_ingested_item(
        source_type="youtube", source_id="abcdefghijk",
        source_url="https://youtube.com/watch?v=abcdefghijk", title="abcdefghijk",
    )
    storage.update_ingested_item(item_id, status="skipped", error_message="members-only")
    monkeypatch.setattr(api, "get_storage", lambda: storage)
    monkeypatch.setattr(api, "_storage", storage, raising=False)

    client = TestClient(app)
    items = client.get("/api/v1/ingested")
    assert items.status_code == 200
    assert items.json()[0]["status"] == "skipped"

    status = client.get("/api/v1/status").json()
    assert status["skipped_items"] == 1


def _api_with_storage(tmp_path, monkeypatch):
    from curator import api
    from curator.storage import CuratorStorage

    storage = CuratorStorage(str(tmp_path / "test.db"))
    monkeypatch.setattr(api, "get_storage", lambda: storage)
    monkeypatch.setattr(api, "_storage", storage, raising=False)
    return storage, TestClient(app)


def test_fetch_job_status_while_processing(tmp_path, monkeypatch):
    """GET /api/v1/fetch/{id} answers 200 while the orchestrator has the job 'processing'."""
    storage, client = _api_with_storage(tmp_path, monkeypatch)
    storage.create_fetch_job("job-1", "https://youtube.com/watch?v=abcdefghijk")
    storage.update_fetch_job("job-1", status="processing")

    response = client.get("/api/v1/fetch/job-1")

    assert response.status_code == 200
    assert response.json()["status"] == "processing"


def test_reset_visual_context_failed_items(tmp_path, monkeypatch):
    from curator.models import SubscriptionType

    storage, client = _api_with_storage(tmp_path, monkeypatch)
    sub_a = storage.create_subscription("A", SubscriptionType.YOUTUBE_CHANNEL, "https://youtube.com/@a")
    sub_b = storage.create_subscription("B", SubscriptionType.YOUTUBE_CHANNEL, "https://youtube.com/@b")

    def item(video_id, sub_id, vc_status, attempts):
        item_id = storage.create_ingested_item(
            source_type="youtube", source_id=video_id,
            source_url=f"https://youtube.com/watch?v={video_id}", title=video_id,
            subscription_id=sub_id,
        )
        storage.update_ingested_item(item_id, status="completed")
        storage.update_visual_context_status(item_id, vc_status, attempts)
        return item_id

    a_failed = item("aaaaaaaaaa1", sub_a, "failed", 3)
    a_complete = item("aaaaaaaaaa2", sub_a, "complete", 1)
    b_failed = item("bbbbbbbbbb1", sub_b, "failed", 3)

    assert client.post("/api/v1/visual-context/reset").status_code == 400

    response = client.post(f"/api/v1/visual-context/reset?subscription_id={sub_a}")
    assert response.status_code == 200
    assert response.json() == {"reset_count": 1}
    assert storage.get_ingested_item(a_failed)["visual_context_status"] is None
    assert storage.get_ingested_item(a_failed)["visual_context_attempts"] == 0
    assert storage.get_ingested_item(a_complete)["visual_context_status"] == "complete"
    assert storage.get_ingested_item(b_failed)["visual_context_status"] == "failed"

    # Reset items re-enter the enrichment queue (subscription A has it enabled).
    storage.update_subscription(sub_a, visual_context_enabled=True)
    queued = storage.get_items_pending_visual_context(max_attempts=3)
    assert [i["id"] for i in queued] == [a_failed]

    response = client.post("/api/v1/visual-context/reset?all_failed=true")
    assert response.json() == {"reset_count": 1}
    assert storage.get_ingested_item(b_failed)["visual_context_status"] is None
