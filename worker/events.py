"""События пакета для SSE и журнал dead-letter (FR-2.2, FR-2.3).

Каждое событие пакета записывается в Redis Stream `b2b:events:batch:{id}`. Stream одновременно даёт
живую доставку (`XREAD BLOCK`) и досылку пропущенного после переподключения: id записи Redis
становится `id:` события SSE, браузер возвращает его в `Last-Event-ID`. Отдельный pub/sub не нужен,
и между досылкой и подпиской нет окна, в котором событие теряется.

Публикуют задачи конвейера и API (ревью находки, повтор документа) — после фиксации транзакции,
чтобы клиент, перечитав пакет по событию, увидел те же данные. Сбой Redis не ломает конвейер:
событие пропадает, а интерфейс получит актуальное состояние в `batch.snapshot` при переподключении.

Типы событий — `BATCH_EVENT_TYPES` фронтенда (`frontend/src/api/types.ts`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)

EVENT_TYPES = ("batch.snapshot", "batch.progress", "batch.meta", "document.progress", "document.done",
               "document.error", "batch.done", "export.done")
# Сколько событий пакета хранится для досылки и как долго: пакет на 50+ страниц даёт несколько сотен
STREAM_MAXLEN = int(os.environ.get("EVENTS_STREAM_MAXLEN") or 5000)
STREAM_TTL_SECONDS = int(os.environ.get("EVENTS_STREAM_TTL_SECONDS") or 24 * 3600)
DEAD_LETTER_KEY = "b2b:dead-letter"
DEAD_LETTER_MAXLEN = 1000
_EVENT_ID = re.compile(r"^\d{1,20}-\d{1,20}$")

Event = tuple[str, str, Any]  # (id, тип, данные)


def stream_key(batch_id: str) -> str:
    return f"b2b:events:batch:{batch_id}"


def valid_event_id(value: str | None) -> bool:
    return bool(value) and bool(_EVENT_ID.match(value))


def _parse_id(event_id: str) -> tuple[int, int]:
    ms, seq = event_id.split("-")
    return int(ms), int(seq)


class EventReader(Protocol):
    async def read(self, after: str, timeout: float) -> list[Event]: ...


class EventBus(Protocol):
    """Шина событий пакетов: Redis в окружении, `MemoryEventBus` в тестах."""

    def publish(self, batch_id: str, type_: str, data: Any) -> str | None: ...

    def last_id(self, batch_id: str) -> str | None: ...

    def has(self, batch_id: str, event_id: str) -> bool: ...

    def reader(self, batch_id: str): ...

    def dead_letter(self, record: dict) -> None: ...

    def dead_letters(self, limit: int = 100) -> list[dict]: ...

    def throttle(self, key: str, seconds: float) -> bool: ...


class RedisEventBus:
    def __init__(self, url: str) -> None:
        import redis

        self.url = url
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5,
                                           health_check_interval=30)

    def publish(self, batch_id: str, type_: str, data: Any) -> str | None:
        key = stream_key(batch_id)
        payload = json.dumps(data, ensure_ascii=False, default=str)
        pipe = self.client.pipeline(transaction=False)
        pipe.xadd(key, {"type": type_, "data": payload}, maxlen=STREAM_MAXLEN, approximate=True)
        pipe.expire(key, STREAM_TTL_SECONDS)
        return pipe.execute()[0]

    def last_id(self, batch_id: str) -> str | None:
        last = self.client.xrevrange(stream_key(batch_id), count=1)
        return last[0][0] if last else None

    def has(self, batch_id: str, event_id: str) -> bool:
        return bool(self.client.xrange(stream_key(batch_id), min=event_id, max=event_id, count=1))

    @asynccontextmanager
    async def reader(self, batch_id: str) -> AsyncIterator[EventReader]:
        """Своё асинхронное соединение на подписчика: `XREAD BLOCK` держит его, пока ждёт событие."""
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5)
        try:
            yield _RedisReader(client, stream_key(batch_id))
        finally:
            await client.aclose()

    def dead_letter(self, record: dict) -> None:
        pipe = self.client.pipeline(transaction=False)
        pipe.lpush(DEAD_LETTER_KEY, json.dumps(record, ensure_ascii=False, default=str))
        pipe.ltrim(DEAD_LETTER_KEY, 0, DEAD_LETTER_MAXLEN - 1)
        pipe.execute()

    def dead_letters(self, limit: int = 100) -> list[dict]:
        return [json.loads(x) for x in self.client.lrange(DEAD_LETTER_KEY, 0, limit - 1)]

    def throttle(self, key: str, seconds: float) -> bool:
        """True не чаще раза в `seconds` на ключ — общий для всех процессов воркеров."""
        return bool(self.client.set(f"b2b:throttle:{key}", "1", nx=True, px=max(1, int(seconds * 1000))))


class _RedisReader:
    def __init__(self, client, key: str) -> None:
        self.client, self.key = client, key

    async def read(self, after: str, timeout: float) -> list[Event]:
        reply = await self.client.xread({self.key: after}, count=200, block=max(1, int(timeout * 1000)))
        out: list[Event] = []
        for _key, entries in reply or []:
            for event_id, fields in entries:
                out.append((event_id, fields.get("type", ""), json.loads(fields.get("data") or "null")))
        return out


class MemoryEventBus:
    """Шина в памяти процесса для тестов: те же id `мс-номер`, досылка и ожидание, что у Redis."""

    def __init__(self, maxlen: int = STREAM_MAXLEN) -> None:
        self.maxlen = maxlen
        self.streams: dict[str, list[Event]] = {}
        self.dead: list[dict] = []
        self.throttled: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last = (0, 0)

    def _next_id(self) -> str:
        ms = int(time.time() * 1000)
        self._last = (ms, 0) if ms > self._last[0] else (self._last[0], self._last[1] + 1)
        return f"{self._last[0]}-{self._last[1]}"

    def publish(self, batch_id: str, type_: str, data: Any) -> str:
        with self._lock:
            event_id = self._next_id()
            items = self.streams.setdefault(str(batch_id), [])
            items.append((event_id, type_, json.loads(json.dumps(data, default=str))))
            del items[:-self.maxlen]
            return event_id

    def events(self, batch_id: str) -> list[Event]:
        return list(self.streams.get(str(batch_id), []))

    def types(self, batch_id: str) -> list[str]:
        return [t for _, t, _ in self.events(batch_id)]

    def last_id(self, batch_id: str) -> str | None:
        items = self.streams.get(str(batch_id))
        return items[-1][0] if items else None

    def has(self, batch_id: str, event_id: str) -> bool:
        return any(i == event_id for i, _, _ in self.streams.get(str(batch_id), []))

    def after(self, batch_id: str, after: str) -> list[Event]:
        mark = _parse_id(after)
        return [e for e in self.streams.get(str(batch_id), []) if _parse_id(e[0]) > mark]

    @asynccontextmanager
    async def reader(self, batch_id: str) -> AsyncIterator[EventReader]:
        yield _MemoryReader(self, str(batch_id))

    def dead_letter(self, record: dict) -> None:
        self.dead.insert(0, json.loads(json.dumps(record, default=str)))

    def dead_letters(self, limit: int = 100) -> list[dict]:
        return self.dead[:limit]

    def throttle(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        if self.throttled.get(key, 0) > now:
            return False
        self.throttled[key] = now + seconds
        return True


class _MemoryReader:
    def __init__(self, bus: MemoryEventBus, batch_id: str) -> None:
        self.bus, self.batch_id = bus, batch_id

    async def read(self, after: str, timeout: float) -> list[Event]:
        deadline = time.monotonic() + timeout
        while True:
            found = self.bus.after(self.batch_id, after)
            if found or time.monotonic() >= deadline:
                return found[:200]
            await asyncio.sleep(0.02)


# --- Публикация ------------------------------------------------------------------------------


def publish(batch_id, type_: str, data: Any) -> str | None:
    """Событие пакета; сбой Redis пишется в журнал и не прерывает задачу или запрос."""
    from worker import runtime

    if type_ not in EVENT_TYPES:
        raise ValueError(f"неизвестный тип события {type_}")
    try:
        return runtime.get_events().publish(str(batch_id), type_, data)
    except Exception as exc:
        log.warning("Событие %s пакета %s не опубликовано: %s", type_, batch_id, exc)
        return None


def throttle(key: str, seconds: float) -> bool:
    from worker import runtime

    try:
        return runtime.get_events().throttle(key, seconds)
    except Exception:
        return True


def dead_letter(*, task: str, stage: str | None, batch_id=None, document_id=None, args: Any = None,
                error: str, attempts: int) -> None:
    """Задача исчерпала повторы или упала неожиданно: запись в `b2b:dead-letter` для разбора.

    Пользователь видит документ «Ошибка» с кнопкой «Повторить»; запись нужна команде: что, где и почему.
    """
    from worker import runtime

    record = {"task": task, "stage": stage, "batch_id": str(batch_id) if batch_id else None,
              "document_id": str(document_id) if document_id else None, "args": args, "error": error[:2000],
              "attempts": attempts, "at": datetime.now(UTC).isoformat()}
    log.error("dead-letter: %s", record)
    try:
        runtime.get_events().dead_letter(record)
    except Exception as exc:
        log.warning("dead-letter не записан в Redis: %s", exc)
