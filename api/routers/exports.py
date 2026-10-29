"""Выгрузка размеченного документа и пакета (FR-7.1, FR-7.2, FR-8.1–8.3).

`POST` создаёт строку `export` и ставит задачу в очередь `export` — ответ сразу, 202. Интерфейс опрашивает
`GET /exports/{id}` (и получает `export.done` по SSE), а готовый файл скачивает по `download_url` —
это `GET /exports/{id}/download` того же API: браузер идёт с cookie входа, MinIO наружу не открывается.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api import hooks
from api.deps import CurrentUser, DbSession, StorageDep
from db import exports
from db.errors import Conflict, NotFound
from db.models import Batch, Document, Export

router = APIRouter(tags=["exports"])

BUSY_BATCH = ("draft", "queued", "processing")
MEDIA_TYPES = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "pdf": "application/pdf", "zip": "application/zip"}


class ExportRequest(BaseModel):
    # FR-7.3* (не MVP): отклонённые находки в выгрузке; интерфейс всегда шлёт false
    include_rejected: bool = False


@router.post("/documents/{document_id}/exports", status_code=202)
def export_document(document_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks,
                    body: ExportRequest | None = None) -> dict:
    """Размеченный документ в его формате: DOCX (DOC и RTF — тоже DOCX) или PDF с аннотациями.

    409 — документ ещё обрабатывается или упал с ошибкой.
    """
    doc = s.get(Document, document_id)
    if doc is None:
        raise NotFound("Документ не найден")
    if doc.status == "error":
        raise Conflict("Документ не проверен из-за ошибки — выгружать нечего")
    if doc.status != "done":
        raise Conflict("Документ ещё проверяется — скачать его можно после проверки")
    job = exports.create_export(s, scope="document", target_id=document_id, actor_id=user.id,
                                include_rejected=bool(body and body.include_rejected))
    background.add_task(hooks.enqueue_export, "document", job["export_id"])
    return job


@router.post("/batches/{batch_id}/exports", status_code=202)
def export_batch(batch_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks,
                 body: ExportRequest | None = None) -> dict:
    """Zip пакета: размеченные документы по папкам исходного архива и `Сводка.xlsx` в корне.

    409 — пакет ещё не запущен или обрабатывается.
    """
    batch = s.get(Batch, batch_id)
    if batch is None:
        raise NotFound("Пакет не найден")
    if batch.status in BUSY_BATCH:
        raise Conflict("Пакет ещё проверяется — скачать его можно после проверки")
    job = exports.create_export(s, scope="batch", target_id=batch_id, actor_id=user.id,
                                include_rejected=bool(body and body.include_rejected))
    background.add_task(hooks.enqueue_export, "batch", job["export_id"])
    return job


@router.get("/exports/{export_id}")
def get_export(export_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    """Статус выгрузки; у готовой — `download_url` и имя файла."""
    from worker.exports import public_view

    return public_view(exports.export_view(s, export_id))


def content_disposition(filename: str) -> str:
    """Имя файла по-русски (RFC 6266/5987) и запасное ASCII-имя для старых клиентов."""
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    return f"attachment; filename=\"export.{ext}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/exports/{export_id}/download")
def download_export(export_id: uuid.UUID, user: CurrentUser, s: DbSession, storage: StorageDep):
    """Готовый файл выгрузки. 409 — ещё готовится или не удался, 404 — срок хранения (сутки) истёк."""
    from worker.exports import safe_filename

    view = exports.export_view(s, export_id)
    if view["status"] == "error":
        raise Conflict(view["error_message"] or "Файл не удалось подготовить")
    if view["status"] != "done" or not view["object_key"]:
        raise Conflict("Файл ещё готовится")
    export = s.get(Export, export_id)
    if export.expires_at is not None and export.expires_at < datetime.now(UTC):
        raise NotFound("Срок хранения файла истёк — подготовьте выгрузку заново")
    chunks = storage.stream(view["object_key"])
    try:
        first = next(chunks, b"")
    except Exception as exc:
        raise NotFound("Файл выгрузки не найден — подготовьте выгрузку заново") from exc

    def body():
        yield first
        yield from chunks

    return StreamingResponse(body(), media_type=MEDIA_TYPES.get(export.format, "application/octet-stream"),
                             headers={"Content-Disposition": content_disposition(safe_filename(view["filename"])),
                                      "Cache-Control": "private, no-store"})
