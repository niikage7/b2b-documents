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
from worker import jsonlog

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
    # Настройки из db/alembic.ini, но без его секции журнала: env.py вызывает fileConfig только при
    # config_file_name, а журнал контейнера API уже настроен (`worker.jsonlog`, LOG_FORMAT=json)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(db.__file__).parent / "migrations"))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["url"] = database_url()
    command.upgrade(cfg, "head")


def main() -> None:
    jsonlog.configure("api")
    migrate()
    with session_scope() as s:
        added = seed(s)
    log.info("Сид: профиль %s, правил добавлено %d, пользователей %d",
             "создан" if added["profile"] else "уже есть", added["rules"], added["users"])


if __name__ == "__main__":
    main()
