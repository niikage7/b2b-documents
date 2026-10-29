"""Профили правил и правила: настройка чек-листа без деплоя (FR-9.1), тест правила (FR-9.3).

Список профилей и системные переменные видны всем (профиль выбирается при запуске пакета),
сами правила читает, меняет и проверяет только методолог.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import hooks
from api.deps import CurrentUser, DbSession, require_role
from db import derived, rules
from db.errors import Conflict, DataError, NotFound, ValidationFailed
from db.models import Document, User

router = APIRouter(tags=["rules"])

Methodologist = Annotated[User, Depends(require_role("methodologist", message="Правила доступны только методологу"))]


class ProfileIn(BaseModel):
    name: str = Field(max_length=300)
    short_name: str = Field("", max_length=60)
    copy_from_profile_id: uuid.UUID | None = None


class ProfilePatch(BaseModel):
    name: str | None = Field(None, max_length=300)
    short_name: str | None = Field(None, max_length=60)
    is_default: bool | None = None


@router.get("/rule-profiles")
def list_profiles(user: CurrentUser, s: DbSession) -> list[dict]:
    return rules.list_profiles(s)


@router.post("/rule-profiles", status_code=201)
def create_profile(body: ProfileIn, user: Methodologist, s: DbSession) -> dict:
    """Новый профиль; `copy_from_profile_id` копирует правила другого профиля."""
    return rules.create_profile(s, name=body.name, short_name=body.short_name.strip() or body.name,
                                copy_from_profile_id=body.copy_from_profile_id, actor_id=user.id)


@router.patch("/rule-profiles/{profile_id}")
def update_profile(profile_id: uuid.UUID, body: ProfilePatch, user: Methodologist, s: DbSession) -> dict:
    return rules.update_profile(s, profile_id, name=body.name, short_name=body.short_name, is_default=body.is_default)


@router.delete("/rule-profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: uuid.UUID, user: Methodologist, s: DbSession) -> None:
    """409 `default_profile` — профиль по умолчанию удалить нельзя."""
    rules.delete_profile(s, profile_id)


class VariableIn(BaseModel):
    name: str = Field(max_length=100)
    type: Literal["text", "number", "list"]
    value: str = Field("", max_length=5000)


class RuleIn(BaseModel):
    profile_id: uuid.UUID | None = None
    code: str = Field(max_length=40)
    title: str = Field(max_length=300)
    description: str = Field(max_length=5000)
    level: Literal["critical", "warning"]
    kind: Literal["semantic_llm", "llm"]
    prompt_template: str = Field(max_length=20000)
    variables: list[VariableIn] = Field(default_factory=list, max_length=50)
    enabled: bool = True


class RulePatch(BaseModel):
    version: int
    code: str | None = Field(None, max_length=40)
    title: str | None = Field(None, max_length=300)
    description: str | None = Field(None, max_length=5000)
    level: Literal["critical", "warning"] | None = None
    kind: Literal["semantic_llm", "llm"] | None = None
    prompt_template: str | None = Field(None, max_length=20000)
    variables: list[VariableIn] | None = Field(None, max_length=50)
    enabled: bool | None = None


@router.get("/rules/system-variables")
def system_variables(user: CurrentUser) -> list[dict]:
    """Переменные, которые подставляет система и которые нельзя переопределить."""
    return rules.system_variables()


@router.get("/rules")
def list_rules(
    user: Methodologist,
    s: DbSession,
    profile_id: uuid.UUID | None = None,
    search: str | None = None,
    kind: Literal["semantic_llm", "llm"] | None = None,
) -> list[dict]:
    """Таблица правил профиля (по умолчанию — профиль по умолчанию)."""
    return rules.list_rules(s, profile_id=profile_id, search=search, kind=kind)


@router.post("/rules", status_code=201)
def create_rule(body: RuleIn, user: Methodologist, s: DbSession) -> dict:
    """Новое правило. 422 — ошибки проверки правила по полям, `warnings` — предупреждения."""
    data = body.model_dump(mode="json")
    if data["profile_id"] is None:
        del data["profile_id"]  # профиль по умолчанию определяет слой данных
    return rules.create_rule(s, data, actor_id=user.id)


@router.get("/rules/{rule_id}")
def get_rule(rule_id: uuid.UUID, user: Methodologist, s: DbSession) -> dict:
    return rules.get_rule(s, rule_id)


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: uuid.UUID, body: RulePatch, user: Methodologist, s: DbSession) -> dict:
    """Правка правила: каждое сохранение — новая версия, действует только на новые пакеты. 409 — версия устарела."""
    data = body.model_dump(mode="json", exclude_unset=True)
    version = data.pop("version")
    return rules.update_rule(s, rule_id, data, version=version, actor_id=user.id)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: uuid.UUID, user: Methodologist, s: DbSession) -> None:
    rules.delete_rule(s, rule_id)


# --- Тест правила (FR-9.3) -------------------------------------------------------------------

RULE_TEST_MAX_TEXT = 20_000
# Сверх 60 с самой проверки — запас на очередь и доставку ответа
RULE_TEST_QUEUE_SECONDS = 10


class RuleDraft(BaseModel):
    """Правило из формы редактора, в том числе не сохранённое; лишние поля формы игнорируются."""

    code: str = Field("", max_length=40)
    title: str = Field("", max_length=300)
    description: str = Field("", max_length=5000)
    level: Literal["critical", "warning"]
    kind: Literal["semantic_llm", "llm"]
    prompt_template: str = Field("", max_length=20000)
    variables: list[VariableIn] = Field(default_factory=list, max_length=50)


class RuleTestIn(BaseModel):
    rule: RuleDraft
    document_id: uuid.UUID | None = None
    text: str | None = None


class RuleTestFailed(DataError):
    """Ошибка ML из воркера: код и текст как есть, статус — по виду ошибки."""

    def __init__(self, error: dict):
        super().__init__(error["message"], code=error["code"], details={"retryable": error.get("retryable", False)})
        if error.get("retryable"):
            self.status = 503
        elif error["code"] in ("rule_template", "text_too_long"):
            self.status = 422
        else:
            self.status = 500


def _check_draft(rule: RuleDraft) -> dict:
    from contracts.analysis import RuleSpec, RuleVariable
    from ml import validate_rule

    fields = {name: "Поле нужно заполнить" for name in ("code", "title", "description", "prompt_template")
              if not getattr(rule, name).strip()}
    if fields:
        raise ValidationFailed(fields)
    spec = RuleSpec(code=rule.code.strip(), title=rule.title, description=rule.description, level=rule.level,
                    kind=rule.kind, prompt_template=rule.prompt_template,
                    variables=[RuleVariable(**v.model_dump()) for v in rule.variables])
    errors, _ = validate_rule(spec)
    for err in errors:
        fields.setdefault(err.field, err.message)
    if fields:
        raise ValidationFailed(fields)
    return spec.model_dump(mode="json", exclude={"id", "version_id", "query_embedding"})


@router.post("/rules/test")
def test_rule(body: RuleTestIn, user: Methodologist, s: DbSession) -> dict:
    """Прогон правила до включения на документе или своём тексте, без записи находок; не дольше 60 с.

    Работает в очереди analysis: тот же движок, что и анализ пакета. 422 — ошибки правила или текста,
    503 `test_timeout` — проверка не уложилась в 60 секунд.
    """
    payload: dict = {"rule": _check_draft(body.rule)}
    if body.document_id is not None:
        doc = s.get(Document, body.document_id)
        if doc is None:
            raise NotFound("Документ не найден")
        if doc.status != "done":
            raise Conflict("Документ ещё обрабатывается или не обработан — выберите другой")
        payload["document_id"] = str(doc.id)
    else:
        text = body.text or ""
        if not text.strip():
            raise ValidationFailed({"text": "Вставьте текст для проверки"})
        if len(text) > RULE_TEST_MAX_TEXT:
            raise ValidationFailed({"text": "Текст длиннее 20 000 символов"}, code="text_too_long")
        payload["text"] = text
    s.commit()  # только чтение: транзакция не держит соединение, пока воркер проверяет правило

    from ml.config import get_settings

    answer = hooks.run_rule_test(payload, timeout=get_settings().test_rule_timeout + RULE_TEST_QUEUE_SECONDS)
    if "error" in answer:
        raise RuleTestFailed(answer["error"])
    result = answer["result"]
    findings = [{k: v for k, v in f.items() if k != "confidence"}
                | {"confidence_level": derived.confidence_level(f.get("confidence"))} for f in result["findings"]]
    return {"findings": findings, "duration_ms": result["duration_ms"], "dropped": result.get("dropped", 0)}


test_rule.__test__ = False  # не тест pytest, несмотря на имя
