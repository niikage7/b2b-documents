"""Начальные данные при первом запуске: профиль «44-ФЗ, поставка товаров», правила R-01…R-08, демо-пользователи.

    python -m db.seed            # после `alembic upgrade head`; повтор ничего не дублирует

Пароль демо-пользователей — из `DEMO_PASSWORD` в `.env`; без него демо-пользователи не создаются.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import RuleProfile, User
from db.rules import import_rules
from db.users import create_user

log = logging.getLogger(__name__)

DEMO_USERS = (
    ("specialist", "Мария Волкова", "specialist"),
    ("lawyer", "Анна Смирнова", "lawyer"),
    ("methodologist", "Игорь Петров", "methodologist"),
)


def seed(s: Session, *, demo_password: str | None = None) -> dict:
    """Создаёт то, чего ещё нет. Возвращает, что добавлено."""
    from ml.checklist import load_default_checklist

    profile_data, rules = load_default_checklist()
    added = {"profile": False, "rules": 0, "users": 0}
    profile = s.scalar(select(RuleProfile).where(RuleProfile.is_default.is_(True)))
    if profile is None:
        profile = RuleProfile(name=profile_data["name"], short_name=profile_data["short_name"], is_default=True)
        s.add(profile)
        s.flush()
        added["profile"] = True
        added["rules"] = import_rules(s, profile.id, rules)
    password = demo_password if demo_password is not None else os.environ.get("DEMO_PASSWORD")
    if password:
        existing = set(s.scalars(select(User.login)))
        for login, name, role in DEMO_USERS:
            if login not in existing:
                create_user(s, login=login, password=password, full_name=name, role=role)
                added["users"] += 1
    elif not s.scalar(select(User.id).limit(1)):
        log.warning("seed: DEMO_PASSWORD не задан — демо-пользователи не созданы, войти будет некому")
    s.flush()
    return added


def main() -> None:
    from db.session import session_scope

    logging.basicConfig(level=logging.INFO)
    with session_scope() as s:
        added = seed(s)
    print(f"Профиль: {'создан' if added['profile'] else 'уже есть'}; правил добавлено: {added['rules']}; "
          f"пользователей: {added['users']}")


if __name__ == "__main__":
    main()
