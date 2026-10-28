"""Вход и выход (FR-10.1)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from api.deps import CurrentUser, DbSession, SettingsDep
from api.security import clear_session_cookie, issue_token, set_session_cookie
from db import users

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


@router.post("/auth/login")
def login(body: LoginIn, response: Response, s: DbSession, settings: SettingsDep) -> dict:
    """Логин или email и пароль → объект `me` и cookie сессии.

    401 — неверная пара, 403 — учётная запись отключена, 429 — блокировка после пяти неудачных попыток
    (`details.retry_after_seconds`).
    """
    user = users.authenticate(s, body.login, body.password)
    set_session_cookie(response, settings, issue_token(settings, str(user.id)))
    return users.me(user)


@router.post("/auth/logout", status_code=204)
def logout(settings: SettingsDep) -> Response:
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.get("/me")
def me(user: CurrentUser) -> dict:
    return users.me(user)
