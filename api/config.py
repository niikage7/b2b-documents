"""Настройки API из переменных окружения (`.env`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from db.batches import MAX_BATCH_BYTES

# Форматы, которые принимает загрузка (FR-1.1); архив разбирается при распаковке пакета
ALLOWED_EXTENSIONS = ["zip", "pdf", "docx", "doc", "rtf"]


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Сессия (FR-10.1): JWT в httpOnly cookie
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", ""))
    session_ttl_hours: float = field(default_factory=lambda: _env_float("SESSION_TTL_HOURS", 12))
    cookie_name: str = field(default_factory=lambda: _env("SESSION_COOKIE_NAME", "b2b_session"))
    # Secure — только при доступе по https; в docker-compose на http://localhost включать нельзя
    cookie_secure: bool = field(default_factory=lambda: _env_bool("COOKIE_SECURE", False))

    # Хранилища
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    minio_endpoint: str = field(default_factory=lambda: _env("MINIO_ENDPOINT", "localhost:9000"))
    minio_access_key: str = field(default_factory=lambda: _env("MINIO_ACCESS_KEY", "b2b-minio"))
    minio_secret_key: str = field(default_factory=lambda: _env("MINIO_SECRET_KEY", "b2b-minio-secret"))
    minio_bucket: str = field(default_factory=lambda: _env("MINIO_BUCKET", "b2b-documents"))
    minio_secure: bool = field(default_factory=lambda: _env_bool("MINIO_SECURE", False))

    # Загрузка
    max_batch_bytes: int = MAX_BATCH_BYTES
    allowed_extensions: tuple[str, ...] = tuple(ALLOWED_EXTENSIONS)

    # Подсказки интерфейсу: границы уверенности находки ИИ
    confidence_high: float = 0.8
    confidence_medium: float = 0.5

    def check(self) -> None:
        """Падаем при старте, а не при первом входе: без секрета сессии подделываются."""
        if len(self.jwt_secret) < 16:
            raise RuntimeError("JWT_SECRET не задан или короче 16 символов: укажите длинную случайную строку в .env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
