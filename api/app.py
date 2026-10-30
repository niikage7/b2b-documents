"""Приложение FastAPI. Запуск: `uvicorn --factory api.app:create_app`."""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, FastAPI

from api.config import get_settings
from api.errors import install_error_handlers
from api.routers import auth, batches, documents, exports, rules, system
from worker import jsonlog

# Идентификаторы для журнала берутся из пути запроса
_PATH_IDS = re.compile(r"^/api/(batches|documents|exports|findings)/([0-9a-fA-F-]{36})(?:/|$)")
_PATH_FIELDS = {"batches": "batch_id", "documents": "document_id", "exports": "export_id", "findings": "finding_id"}


class LogContextMiddleware:
    """Запрос `/api/batches/{id}/…` пишет в журнал с `batch_id`, `/api/documents/{id}/…` — с `document_id`."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        match = _PATH_IDS.match(scope.get("path", ""))
        fields = {_PATH_FIELDS[match[1]]: match[2]} if match else {}
        with jsonlog.bound(**fields):
            return await self.app(scope, receive, send)


class _SkipHealthz(logging.Filter):
    """Healthcheck docker-compose опрашивает `/api/healthz` каждые 10 с — в журнал запросов он не идёт."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(isinstance(a, str) and a.startswith("/api/healthz") for a in (record.args or ()))


def setup_logging() -> None:
    """`LOG_FORMAT` задан (в docker-compose — json): журнал uvicorn и приложения идёт через `worker.jsonlog`.

    Без переменной журнал не трогаем: pytest и `uvicorn --reload` при разработке пишут как обычно.
    """
    if not os.environ.get("LOG_FORMAT"):
        return
    jsonlog.configure("api", loggers=("uvicorn", "uvicorn.error", "uvicorn.access"))
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _SkipHealthz) for f in access.filters):
        access.addFilter(_SkipHealthz())


def create_app() -> FastAPI:
    get_settings().check()
    setup_logging()
    app = FastAPI(
        title="Светофор рисков: API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(LogContextMiddleware)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(batches.router)
    api.include_router(documents.router)
    api.include_router(exports.router)
    api.include_router(rules.router)
    api.include_router(system.router)
    app.include_router(api)
    return app
