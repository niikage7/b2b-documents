"""Прогресс документа и события пакета (контракт 3, FR-2.2).

`report_progress(document_id, stage, percent, detail)` пишет этап и процент в `document` через
`pipeline.set_stage`, а после фиксации транзакции публикует `document.progress` и (не чаще раза в
секунду на пакет) `batch.progress`. Параллельные части одного этапа (группы страниц OCR, группы правил)
увеличивают общий счётчик документа под блокировкой его строки: «стр. 8 из 12», «Анализ по правилам, 3 из 8».

Здесь же собираются данные остальных событий конвейера — в формате `frontend/src/api/types.ts`:
`document.done`, `document.error`, `batch.meta`, `batch.done`, `batch.snapshot`.
"""

from __future__ import annotations

import logging

from db import batches, pipeline
from db.models import Document
from worker import events, runtime

log = logging.getLogger(__name__)

# batch.progress — не чаще раза в секунду на пакет: при OCR события документа идут на каждую страницу
BATCH_PROGRESS_INTERVAL = 1.0


def report_progress(document_id: str, stage: str, percent: int | None = None, detail: dict | None = None) -> dict:
    """Этап (`extracting`, `ocr`, `analysis`) и процент 0–100; `detail` — страницы OCR или группы правил."""
    with runtime.session_scope() as s:
        data = pipeline.set_stage(s, document_id, stage, progress=percent, stage_detail=detail)
        batch_id = str(s.get(Document, pipeline.as_uuid(document_id)).batch_id)
    publish_progress(batch_id, data)
    return data


def publish_progress(batch_id: str, data: dict, *, force_batch: bool = False, view: dict | None = None) -> None:
    """`document.progress` с данными `set_stage`, затем `batch.progress`."""
    if not data:
        return
    events.publish(batch_id, "document.progress", data)
    publish_batch_progress(batch_id, force=force_batch, view=view)


def publish_batch_progress(batch_id: str, *, force: bool = False, view: dict | None = None) -> None:
    """`batch.progress` из `batches.batch_view`. API передаёт `view`, посчитанный в своей транзакции:
    `runtime.session_scope` — сессия задач, в процессе API её нет.
    """
    if view is None and not force and not events.throttle(f"batch-progress:{batch_id}", BATCH_PROGRESS_INTERVAL):
        return
    if view is None:
        try:
            with runtime.session_scope() as s:
                view = batches.batch_view(s, batch_id)
        except Exception as exc:
            log.warning("batch.progress пакета %s не собран: %s", batch_id, exc)
            return
    events.publish(batch_id, "batch.progress", {"status": view["status"], "progress": view.get("progress"),
                                                "eta_seconds": view.get("eta_seconds")})


def publish_snapshot(batch_id: str) -> None:
    """Состав пакета изменился (распаковка создала документы): пакет целиком, как `GET /batches/{id}`."""
    try:
        with runtime.session_scope() as s:
            view = batches.batch_view(s, batch_id)
    except Exception as exc:
        log.warning("batch.snapshot пакета %s не собран: %s", batch_id, exc)
        return
    events.publish(batch_id, "batch.snapshot", view)


def document_done_event(s, document_id, recalc: dict) -> dict:
    """Данные `document.done` из пересчёта `recalc` (анализ закончен или юрист изменил находку)."""
    doc = s.get(Document, pipeline.as_uuid(document_id))
    return {
        "document_id": str(doc.id),
        "pages": doc.pages,
        "worst_level": recalc["document"]["worst_level"],
        "findings": recalc["document"]["findings_summary"],
        "review": recalc["document"]["review"],
        "ocr_low_quality_pages": list(doc.low_quality_pages or []),
    }


def publish_document_done(batch_id: str, event: dict, *, batch_progress: bool = True) -> None:
    events.publish(batch_id, "document.done", event)
    if batch_progress:
        publish_batch_progress(batch_id, force=True)


def publish_document_error(batch_id: str, failed: dict, *, retryable: bool) -> None:
    """`failed` — ответ `pipeline.fail_document`: `{document_id, error: {code, message}}`."""
    events.publish(batch_id, "document.error", {**failed, "retryable": retryable})
    publish_batch_progress(batch_id, force=True)


def publish_batch_meta(batch_id: str, meta: dict) -> None:
    """`meta` — ответ `pipeline.set_batch_meta` или `set_batch_passport`; интерфейсу нужен предмет закупки."""
    events.publish(batch_id, "batch.meta", {"subject": meta.get("subject")})


def publish_batch_finished(batch_id: str, result: dict) -> None:
    """`result` — ответ `pipeline.finish_batch`. Пакет ещё в работе (повтор документа) — только прогресс."""
    if result.get("status") in ("done", "partial_error"):
        events.publish(batch_id, "batch.done", {"status": result["status"], "worst_level": result["worst_level"],
                                                "counters": result["counters"], "review": result["review"]})
    else:
        publish_batch_progress(batch_id, force=True)


def ocr_page_done(document_id: str, pages_total: int, delta: int, *, start: int, span: int) -> dict:
    """Страница скана распознана: «Распознавание текста, стр. N из M»."""
    return _count_done(document_id, "ocr", "page", "pages", pages_total, delta, start=start, span=span)


def rules_done(document_id: str, rules_total: int, delta: int, *, start: int, span: int) -> dict:
    """Группа правил проверена: «Анализ по правилам, N из M»."""
    return _count_done(document_id, "analysis", "rules_done", "rules_total", rules_total, delta,
                       start=start, span=span)


def _count_done(document_id: str, stage: str, done_key: str, total_key: str, total: int, delta: int, *,
                start: int, span: int) -> dict:
    """Счётчик готовых частей этапа. Части идут параллельно в разных процессах, поэтому он
    увеличивается под блокировкой строки документа; повтор части после сбоя не уводит его за `total`.
    """
    with runtime.session_scope() as s:
        doc = s.get(Document, pipeline.as_uuid(document_id), with_for_update=True)
        if doc is None or doc.status == "error":
            return {}
        detail = doc.stage_detail or {}
        before = int(detail.get(done_key, 0)) if doc.stage == stage else 0
        done = min(total, before + delta)
        percent = start + span * done // max(1, total)
        data = pipeline.set_stage(s, document_id, stage, progress=percent,
                                  stage_detail={done_key: done, total_key: total})
        batch_id = str(doc.batch_id)
    publish_progress(batch_id, data)
    return data
