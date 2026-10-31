"""Настройки слоя данных из переменных окружения (`.env`)."""

from __future__ import annotations

import os


def database_url() -> str:
    """Строка подключения SQLAlchemy с драйвером psycopg 3."""
    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://b2b:b2b@localhost:5432/b2b")
    # Строку вида postgresql://… из docker-compose приводим к драйверу psycopg 3
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def embedding_dim() -> int:
    """Размерность `vector(N)` в `chunk` и `rule_version`: должна совпадать с моделью эмбеддингов."""
    return int(os.environ.get("EMBEDDING_DIM", "768"))
