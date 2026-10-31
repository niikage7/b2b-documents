"""Вход, выход и сессия (FR-10.1)."""

from __future__ import annotations

import time

import jwt

from tests.api.conftest import DEMO_PASSWORD, JWT_SECRET, login


def test_login_returns_me_and_sets_httponly_cookie(client, users):
    r = login(client, "specialist")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "specialist" and body["role_label"] == "Специалист"
    assert body["email"] == "specialist@example.test" and body["initials"]
    cookie = r.headers["set-cookie"].lower()
    assert "b2b_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie.replace("samesite", "")


def test_login_by_email_and_case_insensitive(client, users):
    assert login(client, "Lawyer@Example.test").status_code == 200


def test_me_after_login(client, users):
    login(client, "lawyer")
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["role"] == "lawyer"


def test_me_without_cookie_is_401(client, users):
    r = client.get("/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.json()["error"]["message"] == "Сессия завершена, войдите снова"


def test_wrong_password_is_401_in_error_format(client, users):
    r = login(client, "specialist", "wrong")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "invalid_credentials", "message": "Неверный логин или пароль", "details": {}}}
    assert "set-cookie" not in r.headers


def test_unknown_login_is_same_401(client, users):
    r = login(client, "nobody")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_disabled_user_is_403(client, users):
    r = login(client, "fired")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "user_disabled"


def test_lock_after_five_failures_is_429_with_retry_after(client, users):
    for _ in range(5):
        assert login(client, "methodologist", "bad").status_code == 401
    r = login(client, "methodologist", DEMO_PASSWORD)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "too_many_attempts"
    assert 0 < err["details"]["retry_after_seconds"] <= 301
    assert r.headers["retry-after"] == str(err["details"]["retry_after_seconds"])


def test_logout_clears_cookie(client, users):
    login(client, "specialist")
    r = client.post("/api/auth/logout")
    assert r.status_code == 204 and not r.content
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/api/me").status_code == 401


def test_logout_without_session_is_ok(client):
    assert client.post("/api/auth/logout").status_code == 204


def test_forged_token_rejected(client, users):
    forged = jwt.encode(
        {"sub": str(users["specialist"].id), "exp": int(time.time()) + 600},
        "other-secret-other-secret-other-secret",
        algorithm="HS256",
    )
    client.cookies.set("b2b_session", forged)
    assert client.get("/api/me").status_code == 401


def test_expired_token_rejected(client, users):
    old = jwt.encode({"sub": str(users["specialist"].id), "exp": int(time.time()) - 5}, JWT_SECRET, algorithm="HS256")
    client.cookies.set("b2b_session", old)
    assert client.get("/api/me").status_code == 401


def test_token_of_deactivated_user_rejected(client, users):
    login(client, "specialist")
    users["specialist"].is_active = False
    assert client.get("/api/me").status_code == 401


def test_garbage_token_rejected(client):
    client.cookies.set("b2b_session", "not-a-jwt")
    assert client.get("/api/me").status_code == 401


def test_secure_flag_follows_setting(app, users, monkeypatch):
    from fastapi.testclient import TestClient

    from api.config import get_settings

    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    with TestClient(app, base_url="https://testserver") as c:
        r = login(c, "specialist")
    assert "secure" in r.headers["set-cookie"].lower().replace("samesite", "")
