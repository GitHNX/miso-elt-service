"""
Unit tests for the FastAPI reporting API.

Uses FastAPI dependency_overrides to inject mock DB sessions — no live
Postgres required. The engines are lazily connected so they don't fail at
import time; we override get_db before any request is made.
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import os
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("DB_PASSWORD", "x")
os.environ.setdefault("DB_READONLY_PASSWORD", "x")
os.environ.setdefault("ENVIRONMENT", "development")

from src.api.app import app, get_db
from src.models.orm import DimFuelCategory, FactFuelMix, IngestionRun

AUTH = {"Authorization": "Bearer test-api-key"}
BAD_AUTH = {"Authorization": "Bearer wrong-key"}
INTERVAL = datetime(2026, 6, 21, 7, 10, tzinfo=timezone.utc)


def _make_fact(category_name: str, act_mw: float, is_renewable: bool = False):
    dim = DimFuelCategory()
    dim.id = 1
    dim.category_name = category_name
    dim.is_renewable = is_renewable

    fact = FactFuelMix()
    fact.id = 1
    fact.interval_est_utc = INTERVAL
    fact.fuel_category_id = dim.id
    fact.act_mw = Decimal(str(act_mw))
    fact.total_mw = Decimal("64739")
    fact.raw_ref_id = "21-Jun-2026 - Interval 02:10 EST"
    fact.ingested_at = INTERVAL
    return fact, dim


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def client(mock_db):
    """TestClient with get_db overridden to return the mock session."""
    app.dependency_overrides[get_db] = lambda: mock_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_no_auth_required(self):
        # /health doesn't use get_db — plain client, no override needed
        with TestClient(app) as c, \
             patch("src.api.app.check_db_connectivity", return_value=True):
            resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["db_connected"] is True

    def test_health_degraded_when_db_down(self):
        with TestClient(app) as c, \
             patch("src.api.app.check_db_connectivity", return_value=False):
            resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


# ── Authentication ────────────────────────────────────────────────────────────

class TestAuth:
    def test_missing_auth_returns_403(self, client):
        resp = client.get("/api/v1/fuel-mix/latest")
        assert resp.status_code == 403

    def test_wrong_api_key_returns_401(self, client):
        resp = client.get("/api/v1/fuel-mix/latest", headers=BAD_AUTH)
        assert resp.status_code == 401

    def test_valid_api_key_accepted(self, client, mock_db):
        mock_db.query.return_value.scalar.return_value = None
        resp = client.get("/api/v1/fuel-mix/latest", headers=AUTH)
        # 404 because no data — but definitely not 401/403
        assert resp.status_code not in (401, 403)


# ── /api/v1/fuel-mix/latest ───────────────────────────────────────────────────

class TestFuelMixLatest:
    def test_returns_404_when_no_data(self, client, mock_db):
        mock_db.query.return_value.scalar.return_value = None
        resp = client.get("/api/v1/fuel-mix/latest", headers=AUTH)
        assert resp.status_code == 404

    def test_returns_snapshot_structure(self, client, mock_db):
        fact, dim = _make_fact("Wind", 8587.0, is_renewable=True)
        mock_db.query.return_value.scalar.return_value = INTERVAL
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = [(fact, dim)]

        resp = client.get("/api/v1/fuel-mix/latest", headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert "interval_utc" in body
        assert "readings" in body
        assert body["readings"][0]["category"] == "Wind"
        assert body["readings"][0]["act_mw"] == 8587.0
        assert body["readings"][0]["is_renewable"] is True


# ── /api/v1/fuel-mix/history ──────────────────────────────────────────────────

class TestFuelMixHistory:
    def test_pagination_defaults(self, client, mock_db):
        mock_db.query.return_value.join.return_value.count.return_value = 0
        mock_db.query.return_value.join.return_value.filter.return_value.count.return_value = 0
        (mock_db.query.return_value.join.return_value
             .order_by.return_value.offset.return_value.limit.return_value.all
             .return_value) = []

        resp = client.get("/api/v1/fuel-mix/history", headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 100
        assert body["data"] == []

    def test_page_size_capped_at_1000(self, client):
        resp = client.get("/api/v1/fuel-mix/history?page_size=9999", headers=AUTH)
        assert resp.status_code == 422   # FastAPI query validation rejects > 1000


# ── Security: no stack traces exposed ────────────────────────────────────────

class TestSecurityBehavior:
    def test_internal_errors_do_not_leak_stack_trace(self, mock_db):
        """
        FastAPI must return a generic 500 without leaking internal exception
        text. TestClient raises by default; raise_server_exceptions=False
        lets us inspect the actual HTTP response body.
        """
        mock_db.query.side_effect = RuntimeError("pg error with credentials in it")
        app.dependency_overrides[get_db] = lambda: mock_db
        safe_client = TestClient(app, raise_server_exceptions=False)

        resp = safe_client.get("/api/v1/fuel-mix/latest", headers=AUTH)

        app.dependency_overrides.clear()
        assert resp.status_code == 500
        # FastAPI default: {"detail": "Internal Server Error"} — no raw error
        assert "pg error with credentials in it" not in resp.text
        assert "Traceback" not in resp.text
