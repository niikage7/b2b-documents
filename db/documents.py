"""Документ для редактора: блоки, находки, подгрузка страниц, поиск по тексту (FR-5.1–5.5)."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.batches import document_card
from db.errors import Conflict, NotFound, ValidationFailed
from db.findings import _counts, findings_payload
from db.models import Batch, BlockRow, Document, Finding
from db.pipeline import failed_stage

LONG_DOCUMENT_PAGES = 100
FIRST_PAGES = 20
MAX_PAGE_RANGE = 20
MAX_MATCHES = 200


def _id(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def block_view(b: BlockRow) -> dict:
    out = {"id": str(b.id), "order": b.order, "kind": b.kind, "level": b.level, "number": b.number, "text": b.text,
           "page": b.page, "low_ocr_quality": b.low_ocr_quality}
    if b.section_block_id:
        out["section_block_id"] = str(b.section_block_id)
    if b.section_number:
        out["section_number"] = b.section_number
    if b.table_id is not None:
        out |= {"table_id": b.table_id, "row": b.row, "col": b.col}
    if b.align:
        out["align"] = b.align
    if b.bold:
        out["bold"] = True
    return out


def _document(s: Session, document_id) -> Document:
    doc = s.get(Document, _id(document_id))
    if doc is None:
        raise NotFound("Документ не найден")
    return doc


def document_view(s: Session, document_id, *, viewer_id=None) -> dict:
    """`GET /api/documents/{id}`: документ, блоки и все находки, включая отклонённые.

    Документ длиннее 100 страниц отдаёт блоки первых 20 страниц и `blocks_complete = false`;
    документ в работе — пустые `blocks` и `findings`.
    """
    doc = _document(s, document_id)
    batch = s.get(Batch, doc.batch_id)
    counts = _counts(s, batch_id=doc.batch_id).get(doc.id, {})
    card = document_card(doc, counts)
    out = {
        "id": str(doc.id), "batch": {"id": str(batch.id), "name": batch.name, "subject": batch.subject},
        "filename": doc.filename, "kind": doc.kind, "pages": doc.pages, "status": doc.status,
        "stage": doc.stage, "stage_label": card["stage_label"], "progress": doc.progress,
        "worst_level": doc.worst_level, "findings_summary": card["findings"], "review": card["review"],
        "ocr_confidence": doc.ocr_confidence, "ocr_low_quality_pages": card["ocr_low_quality_pages"],
        "error": card["error"], "blocks_complete": True, "blocks": [], "findings": [],
    }
    if doc.status != "done":
        return out
    q = select(BlockRow).where(BlockRow.document_id == doc.id)
    if (doc.pages or 0) > LONG_DOCUMENT_PAGES:
        q = q.where(BlockRow.page <= FIRST_PAGES)
        out["blocks_complete"] = False
    out["blocks"] = [block_view(b) for b in s.scalars(q.order_by(BlockRow.order))]
    findings = s.scalars(select(Finding).where(Finding.document_id == doc.id)).all()
    payload = findings_payload(s, findings, viewer_id=viewer_id)
    out["findings"] = sorted(payload, key=lambda f: (f["order"] is None, f["order"] or 0, f["page"] or 0))
    return out


def document_blocks(s: Session, document_id, *, from_page: int, to_page: int) -> dict:
    """`GET /api/documents/{id}/blocks?from_page=&to_page=`: не больше 20 страниц за раз."""
    if to_page < from_page or to_page - from_page + 1 > MAX_PAGE_RANGE:
        raise ValidationFailed({"to_page": f"Диапазон — от 1 до {MAX_PAGE_RANGE} страниц"})
    doc = _document(s, document_id)
    rows = s.scalars(select(BlockRow).where(BlockRow.document_id == doc.id, BlockRow.page >= from_page,
                                            BlockRow.page <= to_page).order_by(BlockRow.order))
    return {"blocks": [block_view(b) for b in rows]}


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def search_document(s: Session, document_id, q: str) -> dict:
    """`GET /api/documents/{id}/search?q=`: совпадения подстроки без учёта регистра и «ё»."""
    query = _fold(q.strip())
    if len(query) < 2:
        raise ValidationFailed({"q": "Введите не меньше двух символов"})
    doc = _document(s, document_id)
    matches = []
    pattern = re.compile(re.escape(query))
    rows = s.execute(select(BlockRow.id, BlockRow.text, BlockRow.page)
                     .where(BlockRow.document_id == doc.id, func.lower(BlockRow.text).contains(q.strip().lower(), autoescape=True)
                            | func.replace(func.lower(BlockRow.text), "ё", "е").contains(query, autoescape=True))
                     .order_by(BlockRow.order)).all()
    for block_id, text, page in rows:
        for m in pattern.finditer(_fold(text)):
            matches.append({"block_id": str(block_id), "start": m.start(), "end": m.end(), "page": page})
            if len(matches) >= MAX_MATCHES:
                return {"matches": matches}
    return {"matches": matches}


def recent_documents(s: Session, *, limit: int = 20) -> list[dict]:
    """Список «Проверить на документе» на экране правила: последние готовые документы."""
    rows = s.execute(
        select(Document.id, Document.filename, Batch.id, Batch.name).join(Batch, Batch.id == Document.batch_id)
        .where(Document.status == "done", Batch.deleted_at.is_(None))
        .order_by(Document.finished_at.desc().nulls_last()).limit(limit)
    ).all()
    return [{"id": str(d), "filename": f, "batch": {"id": str(b), "name": n}} for d, f, b, n in rows]


def retry_document(s: Session, document_id) -> str:
    """`POST /api/documents/{id}/retry`: этап, с которого перезапускать; документ — снова в очередь."""
    doc = _document(s, document_id)
    if doc.status != "error":
        raise Conflict("Повторить можно только документ с ошибкой")
    stage = failed_stage(s, doc.id) or "extracting"
    doc.status, doc.stage, doc.error_code, doc.error_message, doc.worst_level = "queued", "queued", None, None, None
    s.flush()
    return stage


def batch_findings(s: Session, batch_id, *, level: str | None = None, rule_id=None, status: str | None = None,
                   document_id=None, page: int = 1, page_size: int = 20, viewer_id=None) -> dict:
    """`GET /api/batches/{id}/findings`: находки пакета с документом, цитата до 200 символов."""
    q = select(Finding).where(Finding.batch_id == _id(batch_id))
    if level:
        q = q.where(Finding.level == level)
    if rule_id:
        q = q.where(Finding.rule_id == _id(rule_id))
    if status:
        q = q.where(Finding.status == status)
    if document_id:
        q = q.where(Finding.document_id == _id(document_id))
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    page_size = max(1, min(100, page_size))
    rows = s.scalars(q.order_by(Finding.document_id, Finding.created_at)
                     .offset((max(1, page) - 1) * page_size).limit(page_size)).all()
    docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_({f.document_id for f in rows})))}
    items = []
    for item, f in zip(findings_payload(s, rows, viewer_id=viewer_id), rows, strict=True):
        doc = docs.get(f.document_id)
        quote = item["quote"]
        item["quote"] = quote if len(quote) <= 200 else quote[:199] + "…"
        item["document"] = {"id": str(doc.id), "filename": doc.filename} if doc else None
        items.append(item)
    return {"items": items, "total": total or 0, "page": max(1, page), "page_size": page_size}
