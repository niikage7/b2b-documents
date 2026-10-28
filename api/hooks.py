"""Точки подключения конвейера: API ставит задачи через эти функции, воркеры появляются на этапе 4.

Вызываются после фиксации транзакции (через `BackgroundTasks`), поэтому воркер сразу видит данные.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def enqueue_batch(batch_id: str) -> None:
    """Пакет запущен: поставить `process_batch`. Пока задач нет — только запись в журнал."""
    log.info("Пакет %s поставлен в очередь (конвейер подключается на этапе 4)", batch_id)


def enqueue_rerun(batch_id: str, document_ids: list[str]) -> None:
    """Повторный анализ документов пакета: поставить `analyze_document` для каждого."""
    log.info("Пакет %s: повторный анализ %d документов (конвейер подключается на этапе 4)", batch_id, len(document_ids))
