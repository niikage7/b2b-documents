"""Пакеты: черновик, загрузка файлов, запуск, список и экран пакета (FR-1.1–1.3, FR-10.2)."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from api import hooks, sse, uploads
from api.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from db import batches, documents
from db.batches import BatchTooLarge
from db.errors import Conflict, ValidationFailed
from db.models import UploadedFile
from worker import runtime

router = APIRouter(tags=["batches"])

# Запас на служебные части multipart: границы, заголовки, имена файлов
MULTIPART_SLACK = 1024 * 1024

_UPLOAD_OPENAPI = {
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["files"],
        "properties": {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}}}}}}
}


@router.get("/batches")
def list_batches(
    user: CurrentUser,
    s: DbSession,
    search: Annotated[str | None, Query(max_length=200)] = None,
    status: Literal["all", "processing", "done", "error"] = "all",
    level: Literal["any", "critical", "warning", "ok"] = "any",
    period: Literal["7d", "30d", "all"] = "30d",
    owner: Literal["me", "all"] = "all",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """Экран «Все пакеты»: черновики не показываются, `owner=me` — только пакеты текущего пользователя."""
    return batches.list_batches(s, user_id=user.id, search=search, status=status, level=level, period=period,
                                owner=owner, page=page, page_size=page_size)


@router.post("/batches", status_code=201)
def create_draft(user: CurrentUser, s: DbSession) -> dict:
    return batches.create_draft(s, author_id=user.id)


@router.post("/batches/{batch_id}/files", status_code=201, openapi_extra=_UPLOAD_OPENAPI)
async def upload_files(batch_id: uuid.UUID, request: Request, user: CurrentUser, s: DbSession,
                       settings: SettingsDep, storage: StorageDep) -> dict:
    """Загрузка файлов в черновик (multipart, поле `files`).

    Каждый файл проверяется (формат, пароль, повреждение); непригодные остаются в списке с причиной.
    413 `batch_too_large` — суммарно больше 100 МБ.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_batch_bytes + MULTIPART_SLACK:
        # Тело не читаем: отказ до записи на диск
        raise BatchTooLarge(details={"max_batch_bytes": settings.max_batch_bytes})
    form = await request.form()
    try:
        parts = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
        if not parts:
            raise ValidationFailed({"files": "Выберите хотя бы один файл"})
        return await run_in_threadpool(_store_files, s, storage, settings, str(batch_id), parts)
    finally:
        await form.close()


def _store_files(s, storage, settings, batch_id: str, parts: list[UploadFile]) -> dict:
    view = batches.batch_view(s, batch_id)
    if view["status"] != "draft":
        raise Conflict("Пакет уже запущен, файлы добавлять нельзя")
    stored: list[str] = []
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        prepared = []
        total = view["total_bytes"]
        for i, part in enumerate(parts):
            filename = uploads.clean_filename(part.filename)
            path = Path(tmp) / f"{i}.bin"
            size = uploads.save_to_disk(part.file, path)
            total += size
            if total > settings.max_batch_bytes:
                raise BatchTooLarge(details={"total_bytes": view["total_bytes"],
                                             "max_batch_bytes": settings.max_batch_bytes})
            prepared.append((filename, path, size, part.content_type))
        try:
            for filename, path, size, content_type in prepared:
                key = uploads.object_key(batch_id, filename)
                mime = uploads.guess_mime(filename, content_type)
                storage.put_file(key, path, content_type=mime)
                stored.append(key)
                rows.append({"filename": filename, "size_bytes": size, "mime": mime, "object_key": key,
                             **uploads.inspect_file(path, filename, settings.allowed_extensions)})
            return batches.add_files(s, batch_id, rows)
        except BaseException:
            storage.delete(stored)  # в БД строки не попали: не оставляем сирот в хранилище
            raise


@router.delete("/batches/{batch_id}/files/{file_id}")
def remove_file(batch_id: uuid.UUID, file_id: uuid.UUID, user: CurrentUser, s: DbSession, storage: StorageDep,
                background: BackgroundTasks) -> dict:
    row = s.get(UploadedFile, file_id)
    key = row.object_key if row is not None and row.batch_id == batch_id else None
    result = batches.remove_file(s, batch_id, file_id)
    if key:
        background.add_task(storage.delete, [key])
    return result


class StartIn(BaseModel):
    name: str = Field("", max_length=300)
    profile_id: uuid.UUID | None = None


@router.post("/batches/{batch_id}/start", status_code=202)
def start_batch(batch_id: uuid.UUID, body: StartIn, user: CurrentUser, s: DbSession,
                background: BackgroundTasks) -> dict:
    """Запуск проверки: название обязательно, профиль — по умолчанию из черновика. Версии правил фиксируются."""
    result = batches.start_batch(s, batch_id, name=body.name, profile_id=body.profile_id)
    background.add_task(hooks.enqueue_batch, str(batch_id))
    return result


@router.get("/batches/{batch_id}")
def get_batch(batch_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    return batches.batch_view(s, batch_id)


@router.get("/batches/{batch_id}/events", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}},
                             "description": "Поток событий SSE: batch.snapshot, batch.progress, batch.meta, "
                                            "document.progress, document.done, document.error, batch.done, "
                                            "export.done"}})
def batch_events(batch_id: uuid.UUID, request: Request, user: CurrentUser, s: DbSession,
                 settings: SettingsDep) -> StreamingResponse:
    """Прогресс пакета в реальном времени (FR-2.2). У событий есть `id:`; после обрыва браузер
    присылает `Last-Event-ID` и получает пропущенное, иначе первым идёт `batch.snapshot`.
    """
    bus = runtime.get_events()
    begin = sse.start(s, bus, str(batch_id), request.headers.get("last-event-id"))
    return StreamingResponse(
        sse.stream(bus, str(batch_id), begin, heartbeat=settings.sse_heartbeat_seconds,
                   max_seconds=settings.sse_max_seconds),
        media_type="text/event-stream", headers=sse.HEADERS)


class BatchPatch(BaseModel):
    name: str | None = Field(None, max_length=300)
    subject: str | None = Field(None, max_length=2000)


@router.patch("/batches/{batch_id}")
def patch_batch(batch_id: uuid.UUID, body: BatchPatch, user: CurrentUser, s: DbSession) -> dict:
    return batches.rename_batch(s, batch_id, name=body.name, subject=body.subject)


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: uuid.UUID, user: CurrentUser, s: DbSession, storage: StorageDep,
                 background: BackgroundTasks) -> None:
    """Черновик удаляется вместе с файлами в MinIO, запущенный пакет скрывается."""
    keys = batches.delete_batch(s, batch_id)
    if keys:
        background.add_task(storage.delete, keys)


@router.post("/batches/{batch_id}/rerun", status_code=202)
def rerun_batch(batch_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks) -> None:
    """Повторная проверка по актуальным версиям правил; ручные пометки сохраняются."""
    document_ids = batches.rerun_batch(s, batch_id)
    background.add_task(hooks.enqueue_rerun, str(batch_id), document_ids)


@router.get("/batches/{batch_id}/findings")
def batch_findings(
    batch_id: uuid.UUID,
    user: CurrentUser,
    s: DbSession,
    level: Literal["critical", "warning"] | None = None,
    rule_id: uuid.UUID | None = None,
    status: Literal["new", "accepted", "rejected", "edited"] | None = None,
    document_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    batches.batch_view(s, batch_id)  # 404 для несуществующего пакета
    return documents.batch_findings(s, batch_id, level=level, rule_id=rule_id, status=status, document_id=document_id,
                                    page=page, page_size=page_size, viewer_id=user.id)
