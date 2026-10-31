"""Документы и находки: экран документа, ручные пометки, ревью юриста (FR-5.1–5.5, FR-6.1–6.4)."""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Response
from fastapi import Path as Path_
from pydantic import BaseModel, Field

from api import hooks
from api.deps import CurrentUser, DbSession, StorageDep
from db import batches, documents, findings, pipeline
from db.errors import Conflict, NotFound
from db.models import Batch, Document
from ml.page_view import PageMeta, page_fractions, quote_words, render_page

router = APIRouter(tags=["documents"])


@router.get("/documents")
def recent_documents(
    user: CurrentUser,
    s: DbSession,
    status: Literal["done"] = "done",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    """Готовые документы для выбора на экране правила («Проверить на документе»)."""
    return documents.recent_documents(s, limit=limit)


@router.get("/documents/{document_id}")
def get_document(document_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    """Документ с блоками и всеми находками; длинный документ отдаёт первые 20 страниц (`blocks_complete`)."""
    return documents.document_view(s, document_id, viewer_id=user.id)


@router.get("/documents/{document_id}/blocks")
def document_blocks(
    document_id: uuid.UUID,
    user: CurrentUser,
    s: DbSession,
    from_page: Annotated[int, Query(ge=1)],
    to_page: Annotated[int, Query(ge=1)],
) -> dict:
    """Блоки диапазона страниц (не больше 20 за раз) для длинных документов."""
    return documents.document_blocks(s, document_id, from_page=from_page, to_page=to_page)


@router.get("/documents/{document_id}/search")
def search_document(document_id: uuid.UUID, user: CurrentUser, s: DbSession,
                    q: Annotated[str, Query(max_length=200)] = "") -> dict:
    """Поиск подстроки по тексту документа без учёта регистра и «ё»."""
    return documents.search_document(s, document_id, q)


@router.post("/documents/{document_id}/retry", status_code=202)
def retry_document(document_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks) -> None:
    """Повтор упавшего документа с этапа, на котором он упал (FR-2.3); пакет снова «в работе».

    409 `conflict` — документ не в статусе «Ошибка» или это архив, который не распаковался.
    """
    doc = s.get(Document, document_id)
    if doc is None:
        raise NotFound("Документ не найден")
    if doc.kind == "zip":
        # Архив сверх лимита или битый: тот же файл упадёт так же, нужен исправленный пакет
        raise Conflict("Архив не распаковался — исправьте его и загрузите в новый пакет")
    from worker.tasks import restart_stage

    stage = restart_stage(s, document_id)
    documents.retry_document(s, document_id)  # 409, если документ не в ошибке
    progress = pipeline.set_stage(s, document_id, "queued", progress=0)
    batch = s.get(Batch, doc.batch_id)
    batch.status, batch.finished_at = "processing", None
    s.flush()
    view = batches.batch_view(s, doc.batch_id)
    background.add_task(hooks.enqueue_retry, str(doc.batch_id), str(document_id), stage, progress, view)


class FindingPatch(BaseModel):
    version: int
    status: Literal["new", "accepted", "rejected", "edited"] | None = None
    level: Literal["critical", "warning"] | None = None
    explanation: str | None = Field(None, max_length=5000)
    comment: str | None = Field(None, max_length=5000)


@router.patch("/findings/{finding_id}")
def update_finding(finding_id: uuid.UUID, body: FindingPatch, user: CurrentUser, s: DbSession,
                   background: BackgroundTasks) -> dict:
    """Решение юриста: принять, отклонить, изменить уровень или пояснение, оставить комментарий.

    409 `version_conflict` — находку уже изменил другой пользователь (в `details.current` актуальная версия),
    409 `invalid_transition` — такой переход статуса недоступен.
    """
    result = findings.update_finding(s, finding_id, actor_id=user.id, version=body.version, status=body.status,
                                     level=body.level, explanation=body.explanation, comment=body.comment)
    background.add_task(hooks.publish_recalc, hooks.recalc_event(s, result))
    return result


class NewFinding(BaseModel):
    block_id: uuid.UUID
    start: int
    end: int
    level: Literal["critical", "warning"]
    comment: str | None = Field(None, max_length=5000)


@router.post("/documents/{document_id}/findings", status_code=201)
def create_finding(document_id: uuid.UUID, body: NewFinding, user: CurrentUser, s: DbSession,
                   background: BackgroundTasks) -> dict:
    """Ручная пометка: выделение внутри одного блока, сразу принята."""
    result = findings.create_user_finding(s, document_id, actor_id=user.id, block_id=body.block_id, start=body.start,
                                          end=body.end, level=body.level, comment=body.comment)
    background.add_task(hooks.publish_recalc, hooks.recalc_event(s, result))
    return result


@router.delete("/findings/{finding_id}")
def delete_finding(finding_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks) -> dict:
    """Удаление своей ручной пометки; в ответе пересчёт цвета и счётчиков."""
    result = findings.delete_user_finding(s, finding_id, actor_id=user.id)
    background.add_task(hooks.publish_recalc, hooks.recalc_event(s, result))
    return result


def _page_image(storage, *, batch_id: str, document_id: str, object_key: str,
                page: int) -> tuple[bytes, PageMeta]:
    """Страница и её размеры: рендер один раз, дальше из хранилища. Нет такой страницы — 404."""
    from worker.runtime import work_key

    image_key = work_key(batch_id, document_id, f"pages/{page}.jpg")
    meta_key = work_key(batch_id, document_id, f"pages/{page}.json")
    try:
        return storage.get_bytes(image_key), PageMeta(**json.loads(storage.get_bytes(meta_key)))
    except Exception:  # noqa: BLE001 — страницы ещё нет в хранилище: у MinIO и памяти разные исключения
        pass
    with tempfile.TemporaryDirectory(prefix="b2b-page-") as tmp:
        src = Path(tmp) / "source.pdf"
        storage.download(object_key, src)
        rendered = render_page(src, page)
    if rendered is None:
        raise NotFound("В документе нет такой страницы")
    image, meta = rendered
    storage.put_bytes(image_key, image, content_type="image/jpeg")
    storage.put_bytes(meta_key, json.dumps(asdict(meta)).encode(), content_type="application/json")
    return image, meta


@router.get("/findings/{finding_id}/source")
def finding_source(finding_id: uuid.UUID, user: CurrentUser, s: DbSession, storage: StorageDep) -> dict:
    """Страница исходного PDF и рамки цитаты в долях её размера (FR-5.6*): сверка с оригиналом."""
    place = findings.finding_place(s, finding_id)
    if place["kind"] not in findings.PAGE_VIEW_KINDS:
        raise Conflict("Оригинал страницы можно показать только для PDF и сканов")
    words = quote_words(place["source_map"], place["start"], place["end"])
    page = min(words) if words else place["page"]
    _, meta = _page_image(storage, batch_id=place["batch_id"], document_id=place["document_id"],
                          object_key=place["object_key"], page=page)
    return {"page": page, "image_url": f"/api/documents/{place['document_id']}/pages/{page}/image",
            "aspect": round(meta.width / meta.height, 5), "rects": page_fractions(words.get(page, []), meta)}


@router.get("/documents/{document_id}/pages/{page}/image")
def page_image(document_id: uuid.UUID, page: Annotated[int, Path_(ge=1)], user: CurrentUser, s: DbSession,
               storage: StorageDep) -> Response:
    """JPEG страницы исходного PDF."""
    doc = s.get(Document, document_id)
    if doc is None:
        raise NotFound("Документ не найден")
    if doc.kind not in findings.PAGE_VIEW_KINDS:
        raise Conflict("Оригинал страницы можно показать только для PDF и сканов")
    image, _ = _page_image(storage, batch_id=str(doc.batch_id), document_id=str(doc.id),
                           object_key=doc.object_key, page=page)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/findings/{finding_id}/history")
def finding_history(finding_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    return findings.finding_history(s, finding_id)
