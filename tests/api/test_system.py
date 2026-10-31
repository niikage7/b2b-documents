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


def test_log_context_from_request_path():
    """Запись журнала внутри запроса `/api/batches/{id}/…` несёт `batch_id`, после запроса контекст пуст."""
    import asyncio
    import uuid

    from api.app import LogContextMiddleware
    from worker import jsonlog

    seen = []

    async def app(scope, receive, send):
        seen.append(jsonlog.current())

    batch_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    mw = LogContextMiddleware(app)
    for path in (f"/api/batches/{batch_id}/events", f"/api/documents/{doc_id}", "/api/rules"):
        asyncio.run(mw({"type": "http", "path": path}, None, None))
    assert seen == [{"batch_id": batch_id}, {"document_id": doc_id}, {}]
    assert jsonlog.current() == {}


def test_healthz_is_not_in_access_log():
    import logging

    from api.app import _SkipHealthz

    def record(path):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", "GET", path, "1.1", 200), None)

    assert not _SkipHealthz().filter(record("/api/healthz"))
    assert _SkipHealthz().filter(record("/api/batches"))
