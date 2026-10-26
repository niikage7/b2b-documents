"""Подключение к PostgreSQL: один движок на процесс, сессии через `session_scope`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.config import database_url


@lru_cache(maxsize=4)
def get_engine(url: str | None = None) -> Engine:
    # pool_pre_ping: воркер Celery живёт долго, соединение могло закрыться на стороне БД
    return create_engine(url or database_url(), pool_pre_ping=True, future=True)


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Транзакция: `commit` при выходе без исключения, иначе `rollback`."""
    session = session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
