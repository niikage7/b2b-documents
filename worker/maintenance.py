"""Периодическая очистка: черновики старше суток и файлы просроченных выгрузок (очередь `ingest`).

Задачу ставит Celery beat (в docker-compose он работает внутри сервиса `worker`, `--beat`) раз в
`CLEANUP_INTERVAL_MINUTES` минут, по умолчанию 60. Повтор безопасен: удалённое второй раз не находится.

- Черновик (FR-1.1), который так и не запустили за 24 часа (`db.batches.expired_drafts`), удаляется
  вместе с загруженными файлами в MinIO — так же, как кнопкой «Удалить» (`batches.delete_batch`).
- Выгрузка (FR-7.1, FR-7.2) хранится сутки (`db.exports.EXPORT_TTL`): после `expires_at` API уже отвечает
  404, здесь удаляется сам файл в MinIO, а `object_key` строки обнуляется, чтобы не искать его снова.

Строки удаляются в транзакции, файлы — после неё: сбой MinIO оставит лишний объект, но не строку без файла.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from db import batches
from db.models import Export
from worker import runtime
from worker.app import celery_app

log = logging.getLogger(__name__)


def expired_exports(s, *, now: datetime) -> list[Export]:
    """Готовые выгрузки с истёкшим сроком, у которых файл ещё лежит в MinIO."""
    q = select(Export).where(Export.expires_at < now, Export.object_key.is_not(None)).with_for_update(skip_locked=True)
    return list(s.scalars(q))


def cleanup(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    keys: list[str] = []
    with runtime.session_scope() as s:
        drafts = batches.expired_drafts(s, now=now)
        for batch_id, files in drafts:
            keys.extend(batches.delete_batch(s, batch_id) or files)
        exports = expired_exports(s, now=now)
        for export in exports:
            keys.append(export.object_key)
            export.object_key = None
        s.flush()
    if keys:
        runtime.get_storage().delete(keys)
    result = {"drafts": len(drafts), "exports": len(exports), "objects": len(keys)}
    if drafts or exports:
        log.info("Очистка: черновиков %d, выгрузок %d, объектов MinIO %d", *result.values(), extra=result)
    return result


@celery_app.task(name="worker.maintenance.cleanup_expired")
def cleanup_expired() -> dict:
    return cleanup()
