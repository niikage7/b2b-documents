"""Данные для экспорта (контракт 8): размеченный документ и сводный отчёт (FR-7.1, FR-7.2, FR-8.2).

`export_payload` — активные находки документа с картой исходных позиций блока: по ней экспорт
кладёт подсветку на runs DOCX или рамки слов PDF. `batch_summary` — три листа `Сводка.xlsx`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.errors import NotFound
from db.findings import _counts, batch_summary_counters, findings_payload, iso
from db.models import Batch, BlockRow, Document, Export, Finding, RuleProfile, User

EXPORT_TTL = timedelta(hours=24)
STATUS_LABELS = {"new": "Новая", "accepted": "Принята", "rejected": "Отклонена", "edited": "Изменена"}
LEVEL_LABELS = {"critical": "Критично", "warning": "Внимание", "ok": "Без рисков", "not_checked": "Не проверен"}


def _id(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --- Строки export ---------------------------------------------------------------------------


def create_export(s: Session, *, scope: str, target_id, actor_id, include_rejected: bool = False) -> dict:
    """`POST /api/documents/{id}/exports` и `/api/batches/{id}/exports`: формат — по исходнику."""
    if scope == "document":
        doc = s.get(Document, _id(target_id))
        if doc is None:
            raise NotFound("Документ не найден")
        fmt = "docx" if doc.kind in ("docx", "doc", "rtf") else "pdf"
    else:
        if s.get(Batch, _id(target_id)) is None:
            raise NotFound("Пакет не найден")
        fmt = "zip"
    export = Export(scope=scope, target_id=_id(target_id), format=fmt, include_rejected=include_rejected,
                    status="processing", created_by=_id(actor_id))
    s.add(export)
    s.flush()
    return {"export_id": str(export.id), "format": fmt}


def finish_export(s: Session, export_id, *, object_key: str | None = None, error: str | None = None) -> dict:
    """Задача экспорта закончила: ключ файла в MinIO или текст ошибки; данные события `export.done`."""
    export = s.get(Export, _id(export_id))
    if export is None:
        raise NotFound("Выгрузка не найдена")
    export.status = "error" if error else "done"
    export.object_key = object_key
    export.error_message = error
    export.expires_at = datetime.now(UTC) + EXPORT_TTL
    s.flush()
    return export_view(s, export.id)


def export_filename(s: Session, export: Export) -> str:
    if export.scope == "document":
        doc = s.get(Document, export.target_id)
        stem = doc.filename.rsplit(".", 1)[0] if doc else "Документ"
        return f"{stem}_разметка.{export.format}"
    batch = s.get(Batch, export.target_id)
    return f"{(batch.name if batch else '') or 'Пакет'}_разметка.zip"


def export_view(s: Session, export_id) -> dict:
    """`GET /api/exports/{id}` без ссылки: подписанную ссылку на `object_key` делает API."""
    export = s.get(Export, _id(export_id))
    if export is None:
        raise NotFound("Выгрузка не найдена")
    return {"export_id": str(export.id), "status": export.status, "object_key": export.object_key,
            "filename": export_filename(s, export), "error_message": export.error_message,
            "expires_at": iso(export.expires_at)}


# --- Данные для разметки ---------------------------------------------------------------------


def export_payload(s: Session, document_id, *, include_rejected: bool = False) -> dict:
    """Находки документа для наложения подсветки и комментариев.

    У каждой находки — `source_map` и `bbox` её блока: смещения `start`/`end` переводятся в
    runs DOCX или рамки слов PDF по контракту 2.
    """
    doc = s.get(Document, _id(document_id))
    if doc is None:
        raise NotFound("Документ не найден")
    q = select(Finding).where(Finding.document_id == doc.id)
    if not include_rejected:
        q = q.where(Finding.status != "rejected")
    findings = s.scalars(q).all()
    blocks = {b.id: b for b in s.scalars(select(BlockRow).where(BlockRow.id.in_({f.block_id for f in findings})))}
    items = []
    for f, item in zip(findings, findings_payload(s, findings), strict=True):
        block = blocks[f.block_id]
        items.append(item | {"block_order": block.order, "source_map": block.source_map, "bbox": block.bbox})
    items.sort(key=lambda x: (x["block_order"], x["start"]))
    return {
        "document": {"id": str(doc.id), "filename": doc.filename, "kind": doc.kind, "object_key": doc.object_key,
                     "normalized_key": doc.normalized_key, "pages": doc.pages, "worst_level": doc.worst_level},
        "findings": items,
    }


def batch_summary(s: Session, batch_id, *, include_rejected: bool = False) -> dict:
    """Данные `Сводка.xlsx`: «Итоги», «Документы», «Находки» — с подписями на русском."""
    batch = s.get(Batch, _id(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    profile = s.get(RuleProfile, batch.profile_id)
    author = s.get(User, batch.author_id)
    summary = batch_summary_counters(s, batch.id)
    docs = list(s.scalars(select(Document).where(Document.batch_id == batch.id).order_by(Document.original_path)))
    counts = _counts(s, batch_id=batch.id)
    q = select(Finding).where(Finding.batch_id == batch.id)
    if not include_rejected:
        q = q.where(Finding.status != "rejected")
    findings = s.scalars(q).all()
    by_doc = {d.id: d for d in docs}
    rows = []
    for f, item in zip(findings, findings_payload(s, findings), strict=True):
        doc = by_doc.get(f.document_id)
        rows.append({
            "document": doc.filename if doc else "", "original_path": doc.original_path if doc else "",
            "section_number": item["section_number"], "section_title": item["section_title"], "page": item["page"],
            "level": LEVEL_LABELS.get(f.level, f.level),
            "rule": f"{item['rule']['code']} {item['rule']['title']}" if item["rule"] else "Ручная пометка",
            "quote": f.quote, "explanation": f.explanation, "recommendation": f.recommendation,
            "status": STATUS_LABELS.get(f.status, f.status),
            "decided_by": (item["decided_by"] or {}).get("full_name"), "comment": f.comment,
            "order": item["order"] or 0,
        })
    rows.sort(key=lambda r: (r["original_path"], r["order"]))
    return {
        "totals": {
            "name": batch.name, "subject": batch.subject, "meta": batch.meta,
            "profile": profile.name if profile else None, "author": author.full_name if author else None,
            "created_at": iso(batch.created_at), "worst_level": LEVEL_LABELS.get(summary["worst_level"] or "", "—"),
            "counters": summary["counters"], "review": summary["review"], "findings": summary["findings"],
        },
        "documents": [
            {"filename": d.filename, "original_path": d.original_path, "pages": d.pages,
             "status": "Ошибка" if d.status == "error" else ("Готов" if d.status == "done" else "В работе"),
             "worst_level": LEVEL_LABELS.get(d.worst_level or "", "—"),
             "critical": counts.get(d.id, {}).get("critical", 0), "warning": counts.get(d.id, {}).get("warning", 0),
             "error": d.error_message}
            for d in docs
        ],
        "findings": rows,
    }
