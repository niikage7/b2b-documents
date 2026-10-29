"""Прогресс документа (контракт 3): `report_progress(document_id, stage, percent, detail)`.

Пишет этап и процент в `document` через `pipeline.set_stage` и возвращает данные события
`document.progress`. Публикацию события в Redis для SSE добавит этап 6 — здесь, в одном месте.
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
    """Страница скана распознана: «Распознавание текста, стр. N из M».

    Группы страниц идут параллельно в разных процессах, поэтому счётчик увеличивается под блокировкой
    строки документа. Повтор группы после сбоя не уводит счётчик за число страниц.
    """
    with runtime.session_scope() as s:
        doc = s.get(Document, pipeline.as_uuid(document_id), with_for_update=True)
        if doc is None or doc.status == "error":
            return {}
        done = min(pages_total, int((doc.stage_detail or {}).get("page", 0)) + delta)
        percent = start + span * done // max(1, pages_total)
        return pipeline.set_stage(s, document_id, "ocr", progress=percent,
                                  stage_detail={"page": done, "pages": pages_total})
