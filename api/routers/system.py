"""Служебные эндпоинты: настройки для интерфейса и проверка живости."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api import health
from api.deps import CurrentUser, DbSession, SettingsDep

router = APIRouter(tags=["system"])


@router.get("/config")
def app_config(_: CurrentUser, settings: SettingsDep) -> dict:
    """Настройки для интерфейса: допустимые форматы, лимит пакета, границы уверенности находки."""
    return {
        "upload": {
            "allowed_extensions": list(settings.allowed_extensions),
            "max_batch_bytes": settings.max_batch_bytes,
        },
        "confidence_thresholds": {"high": settings.confidence_high, "medium": settings.confidence_medium},
    }


@router.get("/healthz")
def healthz(s: DbSession, settings: SettingsDep) -> JSONResponse:
    """200, если доступны база данных, Redis и MinIO; иначе 503 с перечнем сбоев. Без авторизации."""
    checks = {
        "db": health.check_db(s),
        "redis": health.check_redis(settings.redis_url),
        "minio": health.check_minio(settings.minio_endpoint, secure=settings.minio_secure),
    }
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "unavailable", "checks": checks})
