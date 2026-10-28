"""Документы и находки: экран документа, ручные пометки, ревью юриста (FR-5.1–5.5, FR-6.1–6.4)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel, Field

from api import hooks
from api.deps import CurrentUser, DbSession
from db import documents, findings

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
    background.add_task(hooks.publish_recalc, result)
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
    background.add_task(hooks.publish_recalc, result)
    return result


@router.delete("/findings/{finding_id}")
def delete_finding(finding_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks) -> dict:
    """Удаление своей ручной пометки; в ответе пересчёт цвета и счётчиков."""
    result = findings.delete_user_finding(s, finding_id, actor_id=user.id)
    background.add_task(hooks.publish_recalc, result)
    return result


@router.get("/findings/{finding_id}/history")
def finding_history(finding_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    return findings.finding_history(s, finding_id)
