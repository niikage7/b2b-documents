"""PostgreSQL для тестов слоя данных: `TEST_DATABASE_URL` или пропуск.

Каждый прогон создаёт свою базу, накатывает миграции Alembic (так проверяется и сама
миграция) и удаляет базу в конце. Тест работает в транзакции с откатом.

    docker run -d --name b2b-test-pg -e POSTGRES_USER=b2b -e POSTGRES_PASSWORD=b2b -p 55432:5432 pgvector/pgvector:pg17
    TEST_DATABASE_URL=postgresql+psycopg://b2b:b2b@localhost:55432/postgres pytest tests/db
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _admin_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_url():
    admin = _admin_url()
    if not admin:
        pytest.skip("нет TEST_DATABASE_URL: тесты слоя данных нужен PostgreSQL с pgvector")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"b2b_test_{uuid.uuid4().hex[:8]}"
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 — база недоступна: тесты пропускаются, а не падают
        pytest.skip(f"PostgreSQL недоступен: {exc}")
    url = make_url(admin).set(database=name).render_as_string(hide_password=False)
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "db" / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    yield url
    from db.session import get_engine

    get_engine.cache_clear()
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()


@pytest.fixture
def s(pg_url):
    """Сессия в транзакции с откатом: `commit` внутри функций слоя данных — точка сохранения."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(pg_url)
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield session
    session.close()
    trans.rollback()
    conn.close()
    engine.dispose()
