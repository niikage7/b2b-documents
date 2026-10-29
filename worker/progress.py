"""Прогресс документа (контракт 3): `report_progress(document_id, stage, percent, detail)`.

Пишет этап и процент в `document` через `pipeline.set_stage` и возвращает данные события
`document.progress`. Публикацию события в Redis для SSE добавит этап 6 — здесь, в одном месте.
Параллельные части одного этапа (группы страниц OCR, группы правил) увеличивают общий счётчик
документа под блокировкой его строки: «стр. 8 из 12», «Анализ по правилам, 3 из 8».
"""

from __future__ import annotations

import logging

from db import pipeline
from db.models import Document
from worker import runtime

log = logging.getLogger(__name__)


def report_progress(document_id: str, stage: str, percent: int | None = None, detail: dict | None = None) -> dict:
    """Этап (`extracting`, `ocr`, `analysis`) и процент 0–100; `detail` — страницы OCR или группы правил."""
    with runtime.session_scope() as s:
        return pipeline.set_stage(s, document_id, stage, progress=percent, stage_detail=detail)


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
        return pipeline.set_stage(s, document_id, stage, progress=percent,
                                  stage_detail={done_key: done, total_key: total})
