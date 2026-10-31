"""Поток событий пакета `GET /api/batches/{id}/events` (SSE, FR-2.2).

События берутся из Redis Stream пакета (`worker.events`). У каждого события `id:` — id записи Redis:
браузер (EventSource) после обрыва переподключается сам и присылает его в `Last-Event-ID`, поток
продолжается с места обрыва. Если такого события уже нет (пакет старый, поток обрезан) или заголовка
нет, первым идёт `batch.snapshot` — пакет целиком, как `GET /api/batches/{id}`.

Чтобы между снимком и подпиской не потерять событие, позиция в потоке запоминается до чтения снимка:
события после неё дойдут, даже если уже учтены в снимке (интерфейс применяет их повторно без вреда).

Пока событий нет, раз в `SSE_HEARTBEAT_SECONDS` идёт комментарий `: ping` — прокси не закрывают
соединение. Через `SSE_MAX_SECONDS` поток закрывается сам, раньше таймаута nginx, и браузер
переподключается. Redis недоступен — клиент получает только снимок и переподключается через `retry`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from db import batches
from worker.events import EventBus, valid_event_id

log = logging.getLogger(__name__)

RETRY_MS = 3000
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def format_event(type_: str, data: Any, event_id: str | None = None) -> str:
    """Событие SSE: JSON в одну строку `data:` — переводы строк внутри JSON экранированы."""
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {type_}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@dataclass(frozen=True)
class Start:
    cursor: str | None  # id, после которого читать поток; None — Redis недоступен, только снимок
    snapshot: dict | None  # None — досылка по Last-Event-ID, снимок не нужен
    snapshot_id: str | None


def start(s: Session, bus: EventBus, batch_id: str, last_event_id: str | None) -> Start:
    """Откуда начинать поток. 404 для несуществующего пакета — из `batches.batch_view`."""
    cursor: str | None = None
    replay = False
    try:
        if valid_event_id(last_event_id) and bus.has(batch_id, last_event_id):
            cursor, replay = last_event_id, True
        else:
            cursor = bus.last_id(batch_id) or "0-0"
    except Exception as exc:
        log.warning("SSE пакета %s: Redis недоступен, отдаём только снимок: %s", batch_id, exc)
        cursor = None
    view = batches.batch_view(s, batch_id)
    if replay:
        return Start(cursor=cursor, snapshot=None, snapshot_id=None)
    return Start(cursor=cursor, snapshot=view, snapshot_id=cursor if cursor and cursor != "0-0" else None)


async def stream(bus: EventBus, batch_id: str, begin: Start, *, heartbeat: float,
                 max_seconds: float) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    if begin.snapshot is not None:
        yield format_event("batch.snapshot", begin.snapshot, begin.snapshot_id)
    if begin.cursor is None:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    cursor = begin.cursor
    try:
        async with bus.reader(batch_id) as reader:
            while (left := deadline - loop.time()) > 0:
                found = await reader.read(cursor, min(heartbeat, left))
                if not found:
                    yield ": ping\n\n"
                    continue
                for event_id, type_, data in found:
                    cursor = event_id
                    yield format_event(type_, data, event_id)
    except Exception as exc:
        # Обрыв связи с Redis: закрываем поток, браузер переподключится с последним полученным id
        log.warning("SSE пакета %s: поток прерван: %s", batch_id, exc)
