"""`GET /api/config`, `GET /api/healthz`, запуск без секрета."""

from __future__ import annotations

import pytest

from api import health
from tests.api.conftest import login


def test_config_matches_frontend_type(client, users):
    assert client.get("/api/config").status_code == 401
    login(client, "specialist")
    body = client.get("/api/config").json()
    assert body == {
        "upload": {"allowed_extensions": ["zip", "pdf", "docx", "doc", "rtf"], "max_batch_bytes": 100 * 1024 * 1024},
        "confidence_thresholds": {"high": 0.8, "medium": 0.5},
    }


@pytest.fixture
def deps_up(monkeypatch):
    monkeypatch.setattr(health, "check_redis", lambda url: True)
    monkeypatch.setattr(health, "check_minio", lambda endpoint, secure=False: True)


def test_healthz_ok_without_auth(client, deps_up):
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"db": True, "redis": True, "minio": True}}


def test_healthz_reports_redis_down(client, deps_up, monkeypatch):
    monkeypatch.setattr(health, "check_redis", lambda url: False)
    r = client.get("/api/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "checks": {"db": True, "redis": False, "minio": True}}


def test_healthz_reports_minio_down(client, deps_up, monkeypatch):
    monkeypatch.setattr(health, "check_minio", lambda endpoint, secure=False: False)
    r = client.get("/api/healthz")
    assert r.status_code == 503 and r.json()["checks"]["minio"] is False


def test_healthz_reports_db_down(client, deps_up, monkeypatch):
    monkeypatch.setattr(health, "check_db", lambda s: False)
    assert client.get("/api/healthz").status_code == 503


def test_checks_of_unreachable_services_are_false():
    assert health.check_redis("redis://127.0.0.1:1/0") is False
    assert health.check_minio("127.0.0.1:1") is False


def test_app_refuses_to_start_without_secret(monkeypatch):
    from api.app import create_app
    from api.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "short")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app()
    get_settings.cache_clear()


def test_openapi_is_served(client):
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    assert "/api/auth/login" in r.json()["paths"]
