"""Точки подключения конвейера: API ставит задачи Celery через эти функции.

Вызываются после фиксации транзакции (через `BackgroundTasks`), поэтому воркер сразу видит данные.
Ошибка постановки (Redis недоступен) пишется в журнал и не ломает ответ: запрос уже выполнен.
"""

from __future__ import annotations

import logging
import uuid

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


def enqueue_retry(batch_id: str, document_id: str, stage: str, progress: dict, view: dict) -> None:
    """Повтор упавшего документа (FR-2.3): цепочка с этапа `stage`, затем анализ и `finalize_batch`.

    `progress` — данные `document.progress` документа в очереди, `view` — пакет после повтора
    (`batches.batch_view`): интерфейс сразу показывает «В очереди» и пакет «в работе».
    """
    from worker import tasks
    from worker.progress import publish_progress

    try:
        tasks.retry_document(batch_id, document_id, stage)
    except Exception:
        log.exception("Документ %s: не удалось поставить повтор в очередь", document_id)
        return
    publish_progress(batch_id, progress, view=view)


def recalc_event(s, recalc: dict) -> tuple[str, dict] | None:
    """Данные `document.done` после правки находки; документ ещё в обработке — событие не нужно.

    Считается в транзакции запроса, публикуется после неё (`publish_recalc` в `BackgroundTasks`).
    """
    from db.models import Document
    from worker.progress import document_done_event

    doc = s.get(Document, uuid.UUID(recalc["document"]["id"]))
    if doc is None or doc.status != "done":
        return None
    return str(doc.batch_id), document_done_event(s, doc.id, recalc)


def publish_recalc(event: tuple[str, dict] | None) -> None:
    """Находку изменили: подписчикам пакета — `document.done` с новым цветом и счётчиками (FR-2.2).

    Автор правки уже обновил экран по ответу API; событие нужно остальным открытым вкладкам.
    """
    if event is None:
        return
    from worker.progress import publish_document_done

    batch_id, data = event
    # batch.progress не нужен: по document.done интерфейс сам перечитывает пакет
    publish_document_done(batch_id, data, batch_progress=False)


def run_rule_test(payload: dict, *, timeout: float) -> dict:
    """Тест правила (FR-9.3) в очереди analysis: образ API лёгкий, а поиску нужна модель эмбеддингов.

    Ждёт ответ воркера не дольше `timeout` секунд; возвращает `{"result": ...}` или `{"error": ...}`
    из задачи `rule_test`. Очередь недоступна или ответа нет — исключение ML с текстом для пользователя.
    """
    from celery.exceptions import TimeoutError as CeleryTimeout

    from ml.errors import LLMError, RuleTestTimeoutError
    from worker import tasks

    try:
        async_result = tasks.rule_test.apply_async((payload,))
    except Exception as exc:
        log.exception("Тест правила: не удалось поставить задачу в очередь")
        raise LLMError("Проверка правила сейчас недоступна — повторите через минуту") from exc
    try:
        return async_result.get(timeout=timeout)
    except CeleryTimeout as exc:
        raise RuleTestTimeoutError() from exc
    finally:
        async_result.forget()
