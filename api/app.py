"""Приложение FastAPI. Запуск: `uvicorn --factory api.app:create_app`."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from api.config import get_settings
from api.errors import install_error_handlers
from api.routers import auth, batches, system


def create_app() -> FastAPI:
    get_settings().check()
    app = FastAPI(
        title="Светофор рисков: API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(batches.router)
    api.include_router(system.router)
    app.include_router(api)
    return app
