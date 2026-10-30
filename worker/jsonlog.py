"""Журнал в JSON для API и воркеров: одна строка — одна запись, с `batch_id`, `document_id`, `export_id`.

    LOG_FORMAT=json   (в docker-compose) — JSON в stdout, его читает `docker compose logs` и сборщик логов
    LOG_FORMAT=text   (по умолчанию)     — обычный текст для локальной разработки и pytest
    LOG_LEVEL=INFO

Идентификаторы попадают в запись из контекста: задачи Celery получают их из своих аргументов
(`worker.app`, сигнал `task_prerun`), этап документа добавляет `batch_id` (`worker.tasks._run_stage`),
запрос API — из пути `/api/batches/{id}` и `/api/documents/{id}` (`api.app`). Код пишет в журнал как обычно:
`log.info(...)`; поля можно передать и явно — `log.info("...", extra={"batch_id": ...})`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_context: ContextVar[dict[str, str] | None] = ContextVar("b2b_log_context", default=None)

# Поля стандартной записи logging: всё остальное из `extra` идёт в JSON как есть
# (`color_message` — копия сообщения uvicorn с цветами терминала)
_RECORD_FIELDS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "color_message"}


def current() -> dict[str, str]:
    return dict(_context.get() or {})


def bind(**fields) -> None:
    """Добавить поля в контекст текущей задачи или запроса; `None` убирает поле."""
    merged = current()
    for key, value in fields.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    _context.set(merged)


def clear() -> None:
    _context.set(None)


@contextmanager
def bound(**fields) -> Iterator[None]:
    """Поля только на время блока: `with bound(batch_id=...)`."""
    token = _context.set(current())
    bind(**fields)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str | None = None) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.service:
            entry["service"] = self.service
        entry.update(current())
        for key, value in vars(record).items():
            if key not in _RECORD_FIELDS and not key.startswith("_") and value is not None:
                entry[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["exc"] = record.exc_text
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Текст для разработки: идентификаторы из контекста — в конце строки."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = {k: v for k, v in current().items() if k in ("batch_id", "document_id", "export_id")}
        if ctx:
            line += " [" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "]"
        return line


def log_format() -> str:
    return (os.environ.get("LOG_FORMAT") or "text").strip().lower()


def configure(service: str, *, level: str | int | None = None, loggers: tuple[str, ...] = ()) -> None:
    """Корневой логгер — в stdout в формате `LOG_FORMAT`; `loggers` (например, uvicorn) пишут через него же."""
    level = level or os.environ.get("LOG_LEVEL") or "INFO"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if log_format() == "json" else TextFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in loggers:
        logger = logging.getLogger(name)
        for old in list(logger.handlers):
            logger.removeHandler(old)
        logger.propagate = True
        logger.disabled = False
