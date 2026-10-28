"""Тесты API: настоящий PostgreSQL (`TEST_DATABASE_URL`, см. tests/db/conftest.py), без сети и LLM.

Каждый тест работает в транзакции с откатом: сессия БД подставляется вместо `get_session`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.db.conftest import pg_url, s  # noqa: F401 — фикстуры БД используются и здесь

DEMO_PASSWORD = "demo-pass-1"
JWT_SECRET = "test-secret-test-secret-test-secret"


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("SESSION_TTL_HOURS", "12")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    from api.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(s, settings_env):  # noqa: F811
    from api.app import create_app
    from api.deps import get_session

    application = create_app()
    application.dependency_overrides[get_session] = lambda: s
    return application


@pytest.fixture
def client(app):
    # base_url по http: cookie без Secure сохраняется и отправляется обратно
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def users(s):  # noqa: F811
    """Три роли и отключённый пользователь; пароль у всех один."""
    from db import users as users_db

    made = {}
    for login, role in (("specialist", "specialist"), ("lawyer", "lawyer"), ("methodologist", "methodologist")):
        made[login] = users_db.create_user(s, login=login, password=DEMO_PASSWORD, full_name=f"Тест {role}", role=role,
                                           email=f"{login}@example.test")
    off = users_db.create_user(s, login="fired", password=DEMO_PASSWORD, full_name="Бывший Сотрудник", role="specialist")
    off.is_active = False
    s.flush()
    made["fired"] = off
    return made


def login(client, name: str, password: str = DEMO_PASSWORD):
    return client.post("/api/auth/login", json={"login": name, "password": password})
