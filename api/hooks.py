"""Точки подключения конвейера: API ставит задачи Celery через эти функции.

Вызываются после фиксации транзакции (через `BackgroundTasks`), поэтому воркер сразу видит данные.
Ошибка постановки (Redis недоступен) пишется в журнал и не ломает ответ: запрос уже выполнен.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def enqueue_batch(batch_id: str) -> None:
    """Пакет запущен: `unpack_batch` → chord по документам → `finalize_batch` (FR-2.1)."""
    from worker import tasks

    try:
        tasks.process_batch(batch_id)
    except Exception:
        log.exception("Пакет %s: не удалось поставить конвейер в очередь", batch_id)
        return
    log.info("Пакет %s поставлен в очередь", batch_id)


def enqueue_rerun(batch_id: str, document_ids: list[str]) -> None:
    """Повторный анализ документов пакета: `analyze_document` для каждого, затем `finalize_batch`."""
    from worker import tasks

    try:
        tasks.rerun_batch(batch_id, document_ids)
    except Exception:
        log.exception("Пакет %s: не удалось поставить повторный анализ в очередь", batch_id)
        return
    log.info("Пакет %s: повторный анализ %d документов поставлен в очередь", batch_id, len(document_ids))


def publish_recalc(recalc: dict) -> None:
    """Находку изменили: разослать подписчикам пакета `document.done` с пересчётом цвета и счётчиков.

    Точка подключения этапа 6 (SSE через Redis). Вызывается после фиксации транзакции.
    """
    log.debug("Пересчёт документа %s: публикация событий подключается на этапе 6", recalc["document"]["id"])
