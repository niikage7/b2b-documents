"""Пакеты: черновик и файлы, старт, список, экран пакета (FR-1.1, FR-1.3, FR-10.2).

Ответы — в формате «Требований к бэкенду», API отдаёт их как есть.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import Select, and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from db import derived
from db.errors import Conflict, DataError, NotFound, ValidationFailed
from db.findings import batch_summary_counters, iso
from db.models import Batch, Document, Finding, Rule, RuleProfile, UploadedFile, User
from db.pipeline import NEXT_STAGE_LABELS, pin_batch_rules, stage_label
from db.rules import default_profile_id

MAX_BATCH_BYTES = 100 * 1024 * 1024
DRAFT_TTL = timedelta(hours=24)
# Оценка оставшегося времени: OCR около 1,7 с на страницу при трёх группах, анализ — около 3 с
OCR_SECONDS_PER_PAGE = 1.7
ANALYSIS_SECONDS_PER_PAGE = 3.0


class BatchTooLarge(DataError):
    status = 413
    code = "batch_too_large"
    message = "Пакет больше 100 МБ"


def _id(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _batch(s: Session, batch_id, *, lock: bool = False) -> Batch:
    q = select(Batch).where(Batch.id == _id(batch_id), Batch.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    batch = s.scalar(q)
    if batch is None:
        raise NotFound("Пакет не найден")
    return batch


# --- Черновик -------------------------------------------------------------------------------


def _file_view(f: UploadedFile) -> dict:
    return {"id": str(f.id), "filename": f.filename, "type_label": PurePosixPath(f.filename).suffix.lstrip(".").upper(),
            "size_bytes": f.size_bytes, "check": f.check, "message": f.message, "archive_entries": f.archive_entries}


def _files(s: Session, batch_id: uuid.UUID) -> list[UploadedFile]:
    return list(s.scalars(select(UploadedFile).where(UploadedFile.batch_id == batch_id)
                          .order_by(UploadedFile.created_at, UploadedFile.filename)))


def create_draft(s: Session, *, author_id) -> dict:
    """`POST /api/batches`: черновик с профилем по умолчанию."""
    batch = Batch(author_id=_id(author_id), profile_id=default_profile_id(s), status="draft")
    s.add(batch)
    s.flush()
    return {"id": str(batch.id), "status": "draft", "name": "", "total_bytes": 0,
            "max_batch_bytes": MAX_BATCH_BYTES, "files": []}


def add_files(s: Session, batch_id, files: list[dict]) -> dict:
    """`POST /api/batches/{id}/files`: строки `uploaded_file` после записи в MinIO и `check_file`.

    `files` — словари с `filename`, `size_bytes`, `mime`, `object_key`, `check`, `message`,
    `archive_entries`. Превышение 100 МБ — 413, файлы запроса не сохраняются.
    """
    batch = _batch(s, batch_id, lock=True)
    if batch.status != "draft":
        raise Conflict("Пакет уже запущен, файлы добавлять нельзя")
    added = sum(int(f["size_bytes"]) for f in files)
    if batch.total_bytes + added > MAX_BATCH_BYTES:
        raise BatchTooLarge(details={"total_bytes": batch.total_bytes, "max_batch_bytes": MAX_BATCH_BYTES})
    for f in files:
        s.add(UploadedFile(batch_id=batch.id, filename=f["filename"], size_bytes=int(f["size_bytes"]),
                           mime=f.get("mime") or "application/octet-stream", object_key=f["object_key"],
                           check=f.get("check", "ok"), message=f.get("message"),
                           archive_entries=f.get("archive_entries")))
        if not batch.name and f["filename"].lower().endswith(".zip"):
            batch.name = PurePosixPath(f["filename"]).stem  # первый zip задаёт название по умолчанию
    batch.total_bytes += added
    s.flush()
    return {"files": [_file_view(f) for f in _files(s, batch.id)], "total_bytes": batch.total_bytes}


def remove_file(s: Session, batch_id, file_id) -> dict:
    batch = _batch(s, batch_id, lock=True)
    if batch.status != "draft":
        raise Conflict("Пакет уже запущен, файлы удалять нельзя")
    f = s.get(UploadedFile, _id(file_id))
    if f is None or f.batch_id != batch.id:
        raise NotFound("Файл не найден")
    batch.total_bytes = max(0, batch.total_bytes - f.size_bytes)
    s.delete(f)
    s.flush()
    return {"files": [_file_view(x) for x in _files(s, batch.id)], "total_bytes": batch.total_bytes}


def start_batch(s: Session, batch_id, *, name: str, profile_id=None) -> dict:
    """`POST /api/batches/{id}/start`: фиксирует название, профиль и версии правил; дальше — `unpack_batch`."""
    batch = _batch(s, batch_id, lock=True)
    if batch.status != "draft":
        raise Conflict("Пакет уже запущен")
    checkable = [f for f in _files(s, batch.id) if f.check in ("ok", "encrypted")
                 or any(e.get("check") == "ok" for e in f.archive_entries or [])]
    if not checkable:
        raise ValidationFailed({"files": "Нет ни одного файла, который можно проверить"}, code="nothing_to_check")
    if not name.strip():
        raise ValidationFailed({"name": "Введите название пакета"})
    if profile_id and s.get(RuleProfile, _id(profile_id)) is None:
        raise ValidationFailed({"profile_id": "Профиль правил не найден"})
    batch.name = name.strip()
    batch.profile_id = _id(profile_id) if profile_id else batch.profile_id
    batch.status = "queued"
    batch.started_at = datetime.now(UTC)
    pin_batch_rules(s, batch.id)
    s.flush()
    return {"id": str(batch.id), "status": batch.status}


def checkable_files(s: Session, batch_id) -> list[dict]:
    """Файлы пакета для `unpack_batch`: что распаковать и для чего создать документы."""
    return [_file_view(f) | {"object_key": f.object_key, "mime": f.mime} for f in _files(s, _id(batch_id))]


def rename_batch(s: Session, batch_id, *, name: str | None = None, subject: str | None = None) -> dict:
    """`PATCH /api/batches/{id}`: название; предмет закупки, если его не удалось извлечь (вопрос 9)."""
    batch = _batch(s, batch_id)
    if name is not None:
        if not name.strip():
            raise ValidationFailed({"name": "Введите название пакета"})
        batch.name = name.strip()
    if subject is not None:
        batch.subject = subject.strip() or None
    s.flush()
    return batch_view(s, batch.id)


def delete_batch(s: Session, batch_id) -> list[str]:
    """`DELETE /api/batches/{id}`: черновик — сразу, запущенный — мягко. Возвращает ключи MinIO черновика."""
    batch = _batch(s, batch_id, lock=True)
    if batch.status == "draft":
        keys = [f.object_key for f in _files(s, batch.id)]
        s.delete(batch)
        s.flush()
        return keys
    batch.deleted_at = datetime.now(UTC)
    s.flush()
    return []


def rerun_batch(s: Session, batch_id) -> list[str]:
    """`POST /api/batches/{id}/rerun`: актуальные версии правил, документы — снова в очередь анализа.

    Находки ИИ перезапишет `merge_findings`, ручные пометки остаются. Возвращает id документов
    без ошибки извлечения — их нужно отправить на анализ заново.
    """
    batch = _batch(s, batch_id, lock=True)
    if batch.status in ("draft", "queued", "processing"):
        raise Conflict("Пакет ещё обрабатывается")
    pin_batch_rules(s, batch.id)
    docs = s.scalars(select(Document).where(Document.batch_id == batch.id, Document.status == "done")).all()
    for doc in docs:
        doc.status, doc.stage, doc.progress, doc.worst_level = "queued", "queued", 0, None
    batch.status = "queued"
    batch.finished_at = None
    s.flush()
    return [str(d.id) for d in docs]


def expired_drafts(s: Session, *, now: datetime | None = None) -> list[tuple[str, list[str]]]:
    """Черновики старше 24 часов и ключи их файлов: для фоновой очистки."""
    border = (now or datetime.now(UTC)) - DRAFT_TTL
    out = []
    for batch in s.scalars(select(Batch).where(Batch.status == "draft", Batch.created_at < border)):
        out.append((str(batch.id), [f.object_key for f in _files(s, batch.id)]))
    return out


# --- Список пакетов --------------------------------------------------------------------------


def _filtered(q: Select, *, user_id, search: str | None, status: str, period: str, owner: str) -> Select:
    q = q.where(Batch.deleted_at.is_(None), Batch.status != "draft")
    if owner == "me" and user_id:
        q = q.where(Batch.author_id == _id(user_id))
    if period in ("7d", "30d"):
        q = q.where(Batch.created_at >= datetime.now(UTC) - timedelta(days=7 if period == "7d" else 30))
    if status == "processing":
        q = q.where(Batch.status.in_(("queued", "processing")))
    elif status == "done":
        q = q.where(Batch.status == "done")
    elif status == "error":
        q = q.where(Batch.status == "partial_error")
    if search and search.strip():
        # autoescape: «%» и «_» из строки поиска — обычные символы, а не шаблон LIKE
        text = search.strip()
        q = q.where(or_(Batch.name.icontains(text, autoescape=True),
                        exists().where(and_(Document.batch_id == Batch.id,
                                            Document.filename.icontains(text, autoescape=True)))))
    return q


def list_batches(s: Session, *, user_id=None, search: str | None = None, status: str = "all", level: str = "any",
                 period: str = "30d", owner: str = "all", page: int = 1, page_size: int = 20) -> dict:
    """`GET /api/batches`: экран «Все пакеты»."""
    page_size = max(1, min(100, page_size))
    page = max(1, page)
    batches = s.scalars(_filtered(select(Batch), user_id=user_id, search=search, status=status, period=period,
                                  owner=owner).order_by(Batch.created_at.desc())).all()
    items = []
    for batch in batches:
        summary = batch_summary_counters(s, batch.id)
        if level != "any" and summary["worst_level"] != level:
            continue
        items.append((batch, summary))
    total = len(items)
    processing = s.scalar(_filtered(select(func.count()).select_from(Batch), user_id=user_id, search=None,
                                    status="processing", period=period, owner=owner))
    authors = {u.id: u for u in s.scalars(select(User).where(User.id.in_({b.author_id for b, _ in items})))}
    profiles = {p.id: p for p in s.scalars(select(RuleProfile).where(RuleProfile.id.in_({b.profile_id
                                                                                         for b, _ in items})))}
    out = []
    for batch, summary in items[(page - 1) * page_size: page * page_size]:
        docs = s.execute(select(Document.status).where(Document.batch_id == batch.id)).scalars().all()
        author = authors.get(batch.author_id)
        profile = profiles.get(batch.profile_id)
        out.append({
            "id": str(batch.id), "name": batch.name, "subject": batch.subject, "created_at": iso(batch.created_at),
            "author": {"id": str(author.id), "full_name": author.full_name} if author else None,
            "profile": {"id": str(profile.id), "short_name": profile.short_name} if profile else None,
            "documents_count": len(docs), "status": batch.status,
            "error_documents_count": sum(1 for st in docs if st == "error"),
            "worst_level": summary["worst_level"], "findings": summary["findings"],
        })
    return {"items": out, "total": total, "processing_count": processing or 0, "page": page, "page_size": page_size}


# --- Экран пакета ----------------------------------------------------------------------------


def _eta(docs: list[Document]) -> int | None:
    remaining = 0.0
    for doc in docs:
        if doc.status in ("done", "error"):
            continue
        pages = doc.pages or max(1, doc.size_bytes // 60_000)
        left = pages * (1 - (doc.progress or 0) / 100)
        remaining += left * ANALYSIS_SECONDS_PER_PAGE + (left * OCR_SECONDS_PER_PAGE if doc.kind == "scan" else 0)
    # Документы обрабатываются параллельно: оценка по самому долгому, а не по сумме
    return int(remaining / max(1, min(len(docs), 4))) if remaining else None


def document_card(doc: Document, counts: dict) -> dict:
    return {
        "id": str(doc.id), "filename": doc.filename, "original_path": doc.original_path, "kind": doc.kind,
        "pages": doc.pages, "size_bytes": doc.size_bytes, "status": doc.status, "stage": doc.stage,
        "stage_label": stage_label(doc.stage, doc.stage_detail), "stage_detail": doc.stage_detail,
        "next_stage_label": NEXT_STAGE_LABELS.get(doc.stage) if doc.status == "processing" else None,
        "progress": doc.progress, "worst_level": doc.worst_level,
        "findings": {"critical": counts.get("critical", 0), "warning": counts.get("warning", 0)},
        "review": {"checked": counts.get("checked", 0), "total": counts.get("total", 0)},
        "ocr_confidence": doc.ocr_confidence, "ocr_low_quality_pages": list(doc.low_quality_pages or []),
        "error": {"code": doc.error_code, "message": doc.error_message} if doc.status == "error" else None,
    }


def batch_view(s: Session, batch_id) -> dict:
    """`GET /api/batches/{id}` и событие `batch.snapshot`: обработка, разбор пакета, левая панель редактора."""
    from db.findings import _counts

    batch = _batch(s, batch_id)
    author = s.get(User, batch.author_id)
    profile = s.get(RuleProfile, batch.profile_id)
    base = {
        "id": str(batch.id), "name": batch.name, "subject": batch.subject, "meta": batch.meta,
        "status": batch.status, "created_at": iso(batch.created_at),
        "author": {"id": str(author.id), "full_name": author.full_name} if author else None,
        "profile": {"id": str(profile.id), "name": profile.name, "short_name": profile.short_name} if profile else None,
    }
    if batch.status == "draft":
        return base | {"files": [_file_view(f) for f in _files(s, batch.id)], "total_bytes": batch.total_bytes,
                       "max_batch_bytes": MAX_BATCH_BYTES, "documents": []}
    docs = list(s.scalars(select(Document).where(Document.batch_id == batch.id)))
    counts = _counts(s, batch_id=batch.id)
    summary = batch_summary_counters(s, batch.id)
    done = sum(1 for d in docs if d.status in ("done", "error"))
    cards = sorted((document_card(d, counts.get(d.id, {})) for d in docs),
                   key=lambda c: (derived.document_sort_key(c["worst_level"]), c["filename"]))
    triggered = s.execute(
        select(Rule.id, Rule.code, Rule.title, Rule.level, func.count())
        .join(Finding, Finding.rule_id == Rule.id)
        .where(Finding.batch_id == batch.id, Finding.status != "rejected")
        .group_by(Rule.id, Rule.code, Rule.title, Rule.level).order_by(Rule.code)
    ).all()
    # Проверки пакета — рядом с правилами: критичное противоречие не должно быть видно только в списке находок
    checks = s.execute(
        select(Finding.check_code, func.min(Finding.check_title), Finding.original_level, func.count())
        .where(Finding.batch_id == batch.id, Finding.status != "rejected", Finding.check_code.is_not(None))
        .group_by(Finding.check_code, Finding.original_level).order_by(Finding.check_code)
    ).all()
    return base | {
        "progress": {"done": done, "total": len(docs), "errors": summary["counters"]["errors"],
                     "percent": int(sum(d.progress for d in docs) / len(docs)) if docs else 0},
        "eta_seconds": _eta(docs) if batch.status in ("queued", "processing") else None,
        "worst_level": summary["worst_level"], "counters": summary["counters"], "review": summary["review"],
        "findings": summary["findings"], "documents": cards,
        "rules_triggered": [{"rule_id": str(rid), "code": code, "title": title, "level": lvl, "count": n}
                            for rid, code, title, lvl, n in triggered]
        + [{"rule_id": f"{CHECK_FILTER}{code}", "code": code, "title": (title or code).split(":")[0], "level": lvl, "count": n}
           for code, title, lvl, n in checks],
    }


CHECK_FILTER = "check:"
"""`rule_id` фильтра находок для встроенной проверки пакета: у неё нет id правила."""


def mark_processing(s: Session, batch_id) -> None:
    """Первый документ пакета пошёл в работу: `queued` → `processing`."""
    s.execute(update(Batch).where(Batch.id == _id(batch_id), Batch.status == "queued").values(status="processing"))

