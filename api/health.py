"""Проверка зависимостей для `GET /api/healthz` и healthcheck в docker-compose."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def check_db(s: Session) -> bool:
    try:
        s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — любая ошибка соединения значит «БД недоступна»
        log.warning("healthz: база данных недоступна", exc_info=True)
        s.rollback()
        return False


def check_redis(url: str) -> bool:
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        log.warning("healthz: Redis недоступен", exc_info=True)
        return False


def check_minio(endpoint: str, *, secure: bool = False) -> bool:
    """MinIO отвечает на `/minio/health/live` без ключей."""
    import urllib.request

    url = f"{'https' if secure else 'http'}://{endpoint}/minio/health/live"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 — адрес из настроек, не от пользователя
            return response.status == 200
    except Exception:  # noqa: BLE001
        log.warning("healthz: MinIO недоступен", exc_info=True)
        return False
