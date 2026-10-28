"""Токен сессии (JWT, HS256) и cookie."""

from __future__ import annotations

import time

import jwt
from fastapi import Response

from api.config import Settings

ALGORITHM = "HS256"


def issue_token(settings: Settings, user_id: str, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = {"sub": user_id, "iat": issued, "exp": issued + int(settings.session_ttl_hours * 3600)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def read_token(settings: Settings, token: str) -> str | None:
    """Идентификатор пользователя или `None`, если токен подделан, просрочен или повреждён."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=int(settings.session_ttl_hours * 3600),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.cookie_name, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
    )
