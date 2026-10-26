"""Пользователи и вход (FR-10.1): хеш argon2, блокировка после пяти неудачных попыток.

Сессию и cookie делает API; здесь — проверка пароля и данные для `GET /api/me`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db.derived import initials
from db.errors import AuthError, DataError, NotFound, ValidationFailed
from db.models import User

ROLES = {"specialist": "Специалист", "lawyer": "Юрист", "methodologist": "Методолог"}
MAX_FAILED = 5
LOCK = timedelta(minutes=5)

_hasher = PasswordHasher()


class TooManyAttempts(DataError):
    status = 429
    code = "too_many_attempts"
    message = "Слишком много попыток входа. Попробуйте через несколько минут"


class UserDisabled(DataError):
    status = 403
    code = "user_disabled"
    message = "Учётная запись отключена. Обратитесь к администратору"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def me(user: User) -> dict:
    """Объект `me`: боковая панель и видимость экрана «Правила»."""
    return {"id": str(user.id), "full_name": user.full_name, "initials": initials(user.full_name),
            "role": user.role, "role_label": ROLES.get(user.role, user.role), "email": user.email}


def create_user(s: Session, *, login: str, password: str, full_name: str, role: str,
                email: str | None = None) -> User:
    if role not in ROLES:
        raise ValidationFailed({"role": "Роль — специалист, юрист или методолог"})
    user = User(login=login.strip(), email=email, full_name=full_name.strip(), role=role,
                password_hash=hash_password(password))
    s.add(user)
    s.flush()
    return user


def get_user(s: Session, user_id) -> User:
    user = s.get(User, user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)))
    if user is None or not user.is_active:
        raise NotFound("Пользователь не найден")
    return user


def authenticate(s: Session, login: str, password: str) -> User:
    """Логин или email и пароль → пользователь. Ошибки — 401, 403 и 429 из «Требований к бэкенду».

    Неудачная попытка сохраняется сразу (`commit`); пять подряд блокируют вход на 5 минут.
    """
    key = login.strip().lower()
    user = s.scalar(select(User).where(or_(func.lower(User.login) == key, func.lower(User.email) == key))
                    .with_for_update())
    now = datetime.now(UTC)
    if user is None:
        raise AuthError()
    if user.locked_until and user.locked_until > now:
        raise TooManyAttempts(details={"retry_after_seconds": int((user.locked_until - now).total_seconds()) + 1})
    try:
        _hasher.verify(user.password_hash, password)
    except (VerificationError, InvalidHashError):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED:
            user.failed_logins = 0
            user.locked_until = now + LOCK
        # Единственное место слоя данных с commit: исключение откатит транзакцию вызывающего,
        # а неудачная попытка должна сохраниться, иначе блокировка не сработает
        s.commit()
        raise AuthError() from None
    if not user.is_active:
        raise UserDisabled()
    user.failed_logins = 0
    user.locked_until = None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    s.flush()
    return user
