"""Зависимости эндпоинтов: сессия БД, текущий пользователь, роль."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.errors import Unauthorized
from api.security import read_token
from db import users
from db.errors import Forbidden, NotFound
from db.models import User
from db.session import session_factory


def get_session() -> Iterator[Session]:
    """Транзакция запроса: `commit` после успешного обработчика, `rollback` при ошибке.

    Подставляется в тестах через `app.dependency_overrides`.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# scope="function": коммит выполняется до отправки ответа, иначе клиент получил бы 200 при сбое коммита
DbSession = Annotated[Session, Depends(get_session, scope="function")]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def current_user(request: Request, s: DbSession, settings: SettingsDep) -> User:
    token = request.cookies.get(settings.cookie_name)
    user_id = read_token(settings, token) if token else None
    if user_id is None:
        raise Unauthorized()
    try:
        return users.get_user(s, uuid.UUID(user_id))
    except (NotFound, ValueError):
        # Пользователя отключили или удалили, пока сессия была жива
        raise Unauthorized() from None


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(*roles: str, message: str | None = None) -> Callable[..., User]:
    """Зависимость: пускает только перечисленные роли, иначе 403 `forbidden`."""

    def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise Forbidden(message or "Недостаточно прав для этого действия")
        return user

    return dependency
