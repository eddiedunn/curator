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
