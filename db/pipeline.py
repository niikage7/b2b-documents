"""Функции для задач Celery: документы, этапы, блоки, фрагменты, правила пакета, находки ИИ.

Каждая функция, которая пишет результат этапа, перезаписывает его целиком по документу:
повтор задачи не создаёт дублей (ключ `(document_id, stage)`).

Порядок вызовов в конвейере:

    create_document → start_stage("extracting") → save_extraction_info → save_blocks →
    save_chunks → set_batch_meta → batch_rules → merge_findings → finish_batch
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from contracts.analysis import FindingCandidate, RuleGroupResult, RuleSpec, RuleVariable
from contracts.document import Block, ChunkRecord
from contracts.extraction import BBox, ExtractionResult, SourceMap
from db import derived
from db.errors import NotFound
from db.findings import recalc
from db.models import (
    Batch,
    BatchRule,
    BlockRow,
    ChunkRow,
    Document,
    Finding,
    FindingEvent,
    Rule,
    RuleVersion,
    TaskRun,
)

STAGES = ("queued", "extracting", "ocr", "analysis", "done", "error")
STAGE_LABELS = {
    "queued": "В очереди",
    "extracting": "Извлечение текста",
    "ocr": "Распознавание текста",
    "analysis": "Анализ по правилам",
    "done": "Готово",
    "error": "Ошибка",
}
NEXT_STAGE_LABELS = {
    "queued": "Далее: извлечение текста",
    "extracting": "Далее: анализ по правилам",
    "ocr": "Далее: анализ по правилам",
    "analysis": None,
    "done": None,
    "error": None,
}


def as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def now() -> datetime:
    return datetime.now(UTC)


def _document(s: Session, document_id) -> Document:
    doc = s.get(Document, as_uuid(document_id))
    if doc is None:
        raise NotFound("Документ не найден")
    return doc


# --- Документ и этапы ------------------------------------------------------------------------


def create_document(s: Session, *, batch_id, uploaded_file_id, filename: str, original_path: str, kind: str,
                    size_bytes: int, object_key: str, document_id=None) -> Document:
    doc = Document(id=as_uuid(document_id) if document_id else uuid.uuid4(), batch_id=as_uuid(batch_id),
                   uploaded_file_id=as_uuid(uploaded_file_id), filename=filename, original_path=original_path,
                   kind=kind, size_bytes=size_bytes, object_key=object_key)
    s.add(doc)
    s.flush()
    return doc


def stage_label(stage: str, detail: dict | None = None) -> str:
    """Подпись этапа для экрана: «Распознавание текста, стр. 8 из 12», «Анализ по правилам, 3 из 8»."""
    label = STAGE_LABELS.get(stage, stage)
    if detail and stage == "ocr" and detail.get("pages"):
        return f"{label}, стр. {detail.get('page', 0)} из {detail['pages']}"
    if detail and stage == "analysis" and detail.get("rules_total"):
        return f"{label}, {detail.get('rules_done', 0)} из {detail['rules_total']}"
    return label


def set_stage(s: Session, document_id, stage: str, *, progress: int | None = None,
              stage_detail: dict | None = None) -> dict:
    """Этап и прогресс документа; возвращает данные события `document.progress` для SSE."""
    doc = _document(s, document_id)
    doc.stage = stage
    doc.status = "processing" if stage not in ("queued", "done", "error") else doc.status
    doc.stage_detail = stage_detail
    if progress is not None:
        doc.progress = max(0, min(100, progress))
    s.flush()
    return {
        "document_id": str(doc.id),
        "status": doc.status,
        "stage": doc.stage,
        "stage_label": stage_label(stage, stage_detail),
        "stage_detail": stage_detail,
        "next_stage_label": NEXT_STAGE_LABELS.get(stage),
        "progress": doc.progress,
    }


def fail_document(s: Session, document_id, code: str, message: str) -> dict:
    """Документ упал после всех повторов: статус `error`, причина на русском.

    Счётчики пакета хранить не нужно: `batch_summary_counters` считает их по документам при чтении,
    статус пакета выставляет `finish_batch`.
    """
    doc = _document(s, document_id)
    doc.status = "error"
    doc.stage = "error"
    doc.error_code = code
    doc.error_message = message
    doc.worst_level = "not_checked"
    doc.finished_at = now()
    s.flush()
    return {"document_id": str(doc.id), "error": {"code": code, "message": message}}


def start_task_run(s: Session, *, batch_id, stage: str, document_id=None) -> TaskRun:
    """Запись журнала этапа; номер попытки считается и для этапов пакета (`unpack_batch`, `finalize_batch`)."""
    doc_filter = TaskRun.document_id == as_uuid(document_id) if document_id else TaskRun.document_id.is_(None)
    # max, а не последняя по started_at: в одной транзакции now() у всех строк одинаковый
    attempt = s.scalar(select(func.max(TaskRun.attempt))
                       .where(TaskRun.batch_id == as_uuid(batch_id), doc_filter, TaskRun.stage == stage))
    run = TaskRun(batch_id=as_uuid(batch_id), document_id=as_uuid(document_id) if document_id else None,
                  stage=stage, status="running", attempt=(attempt or 0) + 1)
    s.add(run)
    s.flush()
    return run


def finish_task_run(s: Session, run_id, *, error: str | None = None) -> None:
    s.execute(update(TaskRun).where(TaskRun.id == as_uuid(run_id))
              .values(status="error" if error else "done", error=error, finished_at=now()))


def failed_stage(s: Session, document_id) -> str | None:
    """Этап, с которого перезапускать документ (`POST /api/documents/{id}/retry`)."""
    last = s.execute(
        select(TaskRun.stage, TaskRun.status).where(TaskRun.document_id == as_uuid(document_id))
        .order_by(TaskRun.started_at.desc()).limit(1)
    ).first()
    return last.stage if last and last.status == "error" else None


# --- Текст документа -------------------------------------------------------------------------


def save_extraction_info(s: Session, document_id, result: ExtractionResult) -> None:
    """Страницы, вид документа (скан или нет) и уверенность OCR (FR-3.5)."""
    doc = _document(s, document_id)
    doc.pages = result.pages
    if result.kind == "scan":
        doc.kind = "scan"
    doc.ocr_confidence = result.ocr_confidence
    doc.low_quality_pages = list(result.low_quality_pages)
    s.flush()


def _block_values(document_id: uuid.UUID, b: Block) -> dict:
    return {
        "id": as_uuid(b.id), "document_id": document_id, "order": b.order, "kind": b.kind, "number": b.number,
        "text": b.text, "page": b.page, "page_end": b.page_end, "level": b.level, "align": b.align,
        "bold": b.bold, "stream": b.stream,
        "section_block_id": as_uuid(b.section_block_id) if b.section_block_id else None,
        "section_number": b.section_number, "section_title": b.section_title, "table_id": b.table_id,
        "row": b.row, "col": b.col, "bbox": b.bbox.model_dump() if b.bbox else None,
        "low_ocr_quality": b.low_ocr_quality,
        "source_map": b.source_map.model_dump(exclude_defaults=True) if b.source_map else None,
    }


def save_blocks(s: Session, document_id, blocks: Sequence[Block]) -> int:
    """Этап `build_structure`: блоки документа. id детерминированы — ручные пометки на месте.

    Блоки, которых больше нет (документ разобран иначе), удаляются вместе с их находками.
    """
    doc_id = as_uuid(document_id)
    rows = [_block_values(doc_id, b) for b in blocks]
    keep = [r["id"] for r in rows]
    s.execute(delete(BlockRow).where(BlockRow.document_id == doc_id, BlockRow.id.not_in(keep)))
    for start in range(0, len(rows), 500):
        part = rows[start:start + 500]
        stmt = insert(BlockRow).values(part)
        stmt = stmt.on_conflict_do_update(
            index_elements=[BlockRow.id],
            set_={c: stmt.excluded[c] for c in part[0] if c not in ("id", "document_id")},
        )
        s.execute(stmt)
    s.flush()
    return len(rows)


def row_to_block(row: BlockRow) -> Block:
    return Block(
        id=str(row.id), document_id=str(row.document_id), order=row.order, kind=row.kind, number=row.number,
        text=row.text, page=row.page, page_end=row.page_end, level=row.level, align=row.align, bold=row.bold,
        stream=row.stream, section_block_id=str(row.section_block_id) if row.section_block_id else None,
        section_number=row.section_number, section_title=row.section_title, table_id=row.table_id, row=row.row,
        col=row.col, bbox=BBox(**row.bbox) if row.bbox else None, low_ocr_quality=row.low_ocr_quality,
        source_map=SourceMap(**row.source_map) if row.source_map else None,
    )


def load_blocks(s: Session, document_id, *, from_page: int | None = None, to_page: int | None = None) -> list[Block]:
    q = select(BlockRow).where(BlockRow.document_id == as_uuid(document_id))
    if from_page is not None:
        q = q.where(BlockRow.page >= from_page)
    if to_page is not None:
        q = q.where(BlockRow.page <= to_page)
    return [row_to_block(r) for r in s.scalars(q.order_by(BlockRow.order))]


def save_chunks(s: Session, document_id, chunks: Sequence[ChunkRecord]) -> int:
    """Этап `index_chunks`: фрагменты с эмбеддингами, старые фрагменты документа заменяются."""
    doc_id = as_uuid(document_id)
    s.execute(delete(ChunkRow).where(ChunkRow.document_id == doc_id))
    rows = [{"id": as_uuid(c.id), "document_id": doc_id, "block_ids": [as_uuid(b) for b in c.block_ids],
             "text": c.text, "embedding": c.embedding, "page_from": c.page_from, "page_to": c.page_to,
             "char_start": c.char_start, "char_end": c.char_end}
            for c in chunks if c.embedding is not None]
    for start in range(0, len(rows), 500):
        s.execute(insert(ChunkRow).values(rows[start:start + 500]))
    s.flush()
    return len(rows)


# --- Пакет и правила -------------------------------------------------------------------------


def set_batch_meta(s: Session, batch_id, *, subject: str | None, meta: dict | None = None) -> dict:
    """Этап `extract_batch_meta`: предмет закупки и паспорт; возвращает данные события `batch.meta`."""
    batch = s.get(Batch, as_uuid(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    batch.subject = subject
    if meta is not None:
        batch.meta = meta
    s.flush()
    return {"subject": subject, "meta": batch.meta}


def set_batch_passport(s: Session, batch_id, passport: dict) -> dict:
    """Паспорт закупки (`ml.extract_passport`) в `batch.meta`; предмет — из паспорта, если его ещё нет.

    Возвращает данные события `batch.meta`. Паспорт можно считать параллельно с анализом:
    правилам нужен только предмет закупки, а его даёт `extract_batch_meta`.
    """
    batch = s.get(Batch, as_uuid(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    meta = dict(batch.meta or {})
    fields = list(passport.get("fields", []))
    # Форма `{"fields": [...]}` — как в `BatchMeta` фронтенда: карточка паспорта читает `meta.passport.fields`
    meta["passport"] = {"fields": fields}
    batch.meta = meta
    if not batch.subject:
        batch.subject = next((f["value"] for f in fields if f.get("key") == "subject"), None)
    s.flush()
    return {"subject": batch.subject, "meta": batch.meta}


def pin_batch_rules(s: Session, batch_id) -> int:
    """На старте пакета: включённые правила профиля в их текущих версиях. Повтор заменяет набор."""
    batch = s.get(Batch, as_uuid(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    versions = s.scalars(
        select(Rule.current_version_id).where(Rule.profile_id == batch.profile_id, Rule.enabled.is_(True),
                                              Rule.deleted_at.is_(None), Rule.current_version_id.is_not(None))
    ).all()
    s.execute(delete(BatchRule).where(BatchRule.batch_id == batch.id))
    s.add_all(BatchRule(batch_id=batch.id, rule_version_id=v) for v in versions)
    s.flush()
    return len(versions)


def version_to_spec(version: RuleVersion) -> RuleSpec:
    snap = version.snapshot
    return RuleSpec(
        id=str(version.rule_id), version_id=str(version.id), code=snap["code"], title=snap["title"],
        description=snap["description"], level=snap["level"], kind=snap["kind"],
        prompt_template=snap["prompt_template"], variables=[RuleVariable(**v) for v in snap.get("variables", [])],
        enabled=True,
        query_embedding=list(version.query_embedding) if version.query_embedding is not None else None,
    )


def batch_rules(s: Session, batch_id) -> list[RuleSpec]:
    """Правила пакета в версиях, зафиксированных на старте: вход `group_rules`."""
    versions = s.scalars(
        select(RuleVersion).join(BatchRule, BatchRule.rule_version_id == RuleVersion.id)
        .where(BatchRule.batch_id == as_uuid(batch_id))
    ).all()
    return sorted((version_to_spec(v) for v in versions), key=lambda r: r.code)


# --- Находки ИИ ------------------------------------------------------------------------------


def merge_findings(s: Session, document_id, results: Iterable[RuleGroupResult]) -> dict:
    """Этап `merge_findings`: находки ИИ документа заменяются кандидатами из групп правил.

    Ручные пометки сохраняются. Документ становится `done`, цвет и счётчики пересчитываются;
    возвращается результат `recalc` — данные для события `document.done`.
    """
    doc = _document(s, document_id)
    s.execute(delete(Finding).where(Finding.document_id == doc.id, Finding.source == "ai"))
    candidates: list[FindingCandidate] = [c for r in results for c in r.candidates]
    known = set(s.scalars(select(BlockRow.id).where(BlockRow.document_id == doc.id)))
    for cand in candidates:
        block_id = as_uuid(cand.block_id)
        if block_id not in known:
            continue  # блок исчез при повторном разборе — находку не к чему привязать
        finding = Finding(
            batch_id=doc.batch_id, document_id=doc.id,
            rule_id=as_uuid(cand.rule_id) if cand.rule_id else None,
            rule_version_id=as_uuid(cand.rule_version_id) if cand.rule_version_id else None,
            source="ai", level=cand.level, original_level=cand.level, status="new", block_id=block_id,
            start=cand.start, end=cand.end, quote=cand.quote, explanation=cand.explanation,
            recommendation=cand.recommendation, confidence=cand.confidence,
        )
        s.add(finding)
        s.flush()
        s.add(FindingEvent(finding_id=finding.id, actor_id=None, action="created",
                           new={"level": cand.level, "status": "new", "rule_code": cand.rule_code}))
    doc.status = "done"
    doc.stage = "done"
    doc.progress = 100
    doc.stage_detail = None
    doc.finished_at = now()
    s.flush()
    return recalc(s, doc.id)


def finish_batch(s: Session, batch_id) -> dict:
    """`finalize_batch`: статус пакета по документам; данные события `batch.done`."""
    batch = s.get(Batch, as_uuid(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    statuses = s.scalars(select(Document.status).where(Document.batch_id == batch.id)).all()
    batch.status = derived.batch_status(statuses)
    if batch.status in ("done", "partial_error"):
        batch.finished_at = batch.finished_at or now()
        batch.eta_seconds = None
    s.flush()
    from db.findings import batch_summary_counters

    return {"status": batch.status, **batch_summary_counters(s, batch.id)}
