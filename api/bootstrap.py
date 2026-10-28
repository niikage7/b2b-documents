"""Подготовка БД при старте контейнера API: миграции Alembic и сид. Повторный запуск безопасен.

    python -m api.bootstrap
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from alembic import command
from alembic.config import Config

import db
from db.config import database_url
from db.seed import seed
from db.session import session_scope

log = logging.getLogger("api.bootstrap")


def migrate(*, attempts: int = 30, delay: float = 2.0) -> None:
    """`alembic upgrade head`; ждёт БД, если она ещё поднимается."""
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url())
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("БД недоступна (%s), попытка %d из %d", exc.__class__.__name__, attempt, attempts)
            time.sleep(delay)
    engine.dispose()
    cfg = Config(str(Path(db.__file__).parent / "alembic.ini"))
    cfg.attributes["url"] = database_url()
    command.upgrade(cfg, "head")


def main() -> None:
    migrate()
    # Alembic перенастраивает логирование по alembic.ini и отключает уже созданные логгеры;
    # возвращаем своё и включаем логгер заново, иначе итог сида не виден
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    log.disabled = False
    with session_scope() as s:
        added = seed(s)
    log.info("Сид: профиль %s, правил добавлено %d, пользователей %d",
             "создан" if added["profile"] else "уже есть", added["rules"], added["users"])


if __name__ == "__main__":
    main()
