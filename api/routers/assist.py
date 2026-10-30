"""Запросы к ИИ по кнопке: вопрос к пакету, вывод для руководителя, запрос о разъяснениях, черновик правила.

Ответ ждёт HTTP-запрос, но работа идёт в очереди `analysis`: API не держит модель эмбеддингов и не зависит от
числа запросов. Запрос о разъяснениях — обычная выгрузка: 202 и файл через `GET /api/exports/{id}`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from api import hooks
from api.deps import CurrentUser, DbSession, require_role
from db import exports, rules
from db.errors import Conflict, DataError, NotFound, ValidationFailed
from db.models import Batch, Document, User

router = APIRouter(tags=["assist"])

Methodologist = Annotated[User, Depends(require_role("methodologist", message="Правила доступны только методологу"))]

ASSIST_TIMEOUT_SECONDS = 70
MAX_QUESTION = 500
MAX_DESCRIPTION = 2000


class AssistFailed(DataError):
    """Ошибка ML из воркера: код и текст как есть."""

    def __init__(self, error: dict):
        super().__init__(error["message"], code=error["code"], details={"retryable": error.get("retryable", False)})
        self.status = 503 if error.get("retryable") else 500


def _run(payload: dict) -> dict:
    answer = hooks.run_assist(payload, timeout=ASSIST_TIMEOUT_SECONDS)
    if "error" in answer:
        raise AssistFailed(answer["error"])
    result = answer["result"]
    result.pop("usage", None)
    return result


def _checked_batch(s, batch_id: uuid.UUID) -> Batch:
    batch = s.get(Batch, batch_id)
    if batch is None:
        raise NotFound("Пакет не найден")
    if not s.query(Document.id).filter(Document.batch_id == batch.id, Document.status == "done").first():
        raise Conflict("В пакете ещё нет проверенных документов")
    return batch


class AskIn(BaseModel):
    question: str = Field(max_length=MAX_QUESTION)


@router.post("/batches/{batch_id}/ask")
def ask(batch_id: uuid.UUID, body: AskIn, user: CurrentUser, s: DbSession) -> dict:
    """Вопрос по документам пакета: ответ с цитатами. `found=false` — в документах ответа нет."""
    if not body.question.strip():
        raise ValidationFailed({"question": "Напишите вопрос"})
    _checked_batch(s, batch_id)
    s.commit()  # только чтение: транзакция не держит соединение, пока воркер ждёт модель
    return _run({"kind": "ask", "batch_id": str(batch_id), "question": body.question})


@router.post("/batches/{batch_id}/brief")
def brief(batch_id: uuid.UUID, user: CurrentUser, s: DbSession) -> dict:
    """Вывод для руководителя по находкам после ревью; сохраняется в `batch.meta.brief`."""
    _checked_batch(s, batch_id)
    s.commit()
    return _run({"kind": "brief", "batch_id": str(batch_id)})


@router.post("/batches/{batch_id}/clarification", status_code=202)
def clarification(batch_id: uuid.UUID, user: CurrentUser, s: DbSession, background: BackgroundTasks) -> dict:
    """Запрос о даче разъяснений заказчику по активным находкам пакета, DOCX. 409 — находок нет."""
    _checked_batch(s, batch_id)
    if not exports.clarification_input(s, batch_id)["findings"]:
        raise Conflict("Находок нет — спрашивать заказчика не о чем")
    job = exports.create_export(s, scope="clarification", target_id=batch_id, actor_id=user.id)
    background.add_task(hooks.enqueue_clarification, job["export_id"])
    return job


class RuleDraftIn(BaseModel):
    description: str = Field(max_length=MAX_DESCRIPTION)
    profile_id: uuid.UUID | None = None


@router.post("/rules/draft")
def draft_rule(body: RuleDraftIn, user: Methodologist, s: DbSession) -> dict:
    """Черновик правила из описания риска; ничего не сохраняет. Код — следующий свободный в профиле."""
    if len(body.description.strip()) < 10:
        raise ValidationFailed({"description": "Опишите риск хотя бы одним предложением"})
    codes = {r["code"] for r in rules.list_rules(s, profile_id=body.profile_id)}
    s.commit()
    result = _run({"kind": "rule_draft", "description": body.description})
    number = next(n for n in range(1, len(codes) + 2) if f"R-{n:02d}" not in codes)
    return result | {"code": f"R-{number:02d}"}
