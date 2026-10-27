"""Профили и правила (FR-9.1, FR-9.4*): CRUD с версиями и проверкой переменных.

Каждое сохранение правила создаёт строку `rule_version` со снимком: пакет проверяется
версиями на момент старта, поэтому правка правила не меняет уже запущенные пакеты.
`query_embedding` можно не передавать: движок посчитает эмбеддинг строки поиска сам.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from contracts.analysis import SYSTEM_VARIABLES, RuleSpec, RuleVariable
from db.errors import Conflict, NotFound, ValidationFailed, VersionConflict
from db.findings import iso
from db.models import Finding, Rule, RuleProfile, RuleVersion, User

MAX_LLM_RULES = 5
"""Правил типа `llm` (весь документ окнами) в профиле — не больше: каждое читает документ целиком."""

_FIELDS = ("code", "title", "description", "level", "kind", "prompt_template", "variables", "enabled")


def _id(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def system_variables() -> list[dict]:
    """`GET /api/rules/system-variables`."""
    return [{"name": name, "type": "auto", "description": text} for name, text in SYSTEM_VARIABLES.items()]


# --- Профили ---------------------------------------------------------------------------------


def default_profile_id(s: Session) -> uuid.UUID:
    pid = s.scalar(select(RuleProfile.id).where(RuleProfile.is_default.is_(True)))
    if pid is None:
        raise NotFound("Профиль правил по умолчанию не настроен")
    return pid


def list_profiles(s: Session) -> list[dict]:
    counts = dict(s.execute(
        select(Rule.profile_id, func.count()).where(Rule.enabled.is_(True), Rule.deleted_at.is_(None))
        .group_by(Rule.profile_id)
    ).all())
    profiles = s.scalars(select(RuleProfile).order_by(RuleProfile.is_default.desc(), RuleProfile.name)).all()
    return [{"id": str(p.id), "name": p.name, "short_name": p.short_name, "is_default": p.is_default,
             "enabled_rules_count": counts.get(p.id, 0)} for p in profiles]


def create_profile(s: Session, *, name: str, short_name: str, copy_from_profile_id=None, actor_id=None) -> dict:
    if not name.strip() or not short_name.strip():
        raise ValidationFailed({"name" if not name.strip() else "short_name": "Заполните название"})
    profile = RuleProfile(name=name.strip(), short_name=short_name.strip(), is_default=False)
    s.add(profile)
    s.flush()
    if copy_from_profile_id:
        for rule in s.scalars(select(Rule).where(Rule.profile_id == _id(copy_from_profile_id),
                                                 Rule.deleted_at.is_(None))):
            _save_new_rule(s, profile.id, {f: getattr(rule, f) for f in _FIELDS}, actor_id=actor_id)
    return next(p for p in list_profiles(s) if p["id"] == str(profile.id))


def update_profile(s: Session, profile_id, *, name: str | None = None, short_name: str | None = None,
                   is_default: bool | None = None) -> dict:
    profile = s.get(RuleProfile, _id(profile_id))
    if profile is None:
        raise NotFound("Профиль не найден")
    if name is not None:
        profile.name = name.strip()
    if short_name is not None:
        profile.short_name = short_name.strip()
    if is_default:
        # Снять флаг с прежнего профиля в той же транзакции: частичный уникальный индекс
        s.execute(update(RuleProfile).where(RuleProfile.id != profile.id).values(is_default=False))
        s.flush()
        profile.is_default = True
    s.flush()
    return next(p for p in list_profiles(s) if p["id"] == str(profile.id))


def delete_profile(s: Session, profile_id) -> None:
    profile = s.get(RuleProfile, _id(profile_id))
    if profile is None:
        raise NotFound("Профиль не найден")
    if profile.is_default:
        raise Conflict("Профиль по умолчанию удалить нельзя", code="default_profile")
    s.delete(profile)
    s.flush()


# --- Правила ---------------------------------------------------------------------------------


def rule_to_spec(rule: Rule, *, query_embedding: Sequence[float] | None = None) -> RuleSpec:
    return RuleSpec(
        id=str(rule.id) if rule.id else None,
        version_id=str(rule.current_version_id) if rule.current_version_id else None,
        code=rule.code, title=rule.title, description=rule.description, level=rule.level, kind=rule.kind,
        prompt_template=rule.prompt_template, variables=[RuleVariable(**v) for v in rule.variables or []],
        enabled=rule.enabled, query_embedding=list(query_embedding) if query_embedding is not None else None,
    )


def _snapshot(rule: Rule) -> dict:
    return {f: getattr(rule, f) for f in _FIELDS} | {"profile_id": str(rule.profile_id)}


def _validate(s: Session, profile_id: uuid.UUID, data: dict, *, rule_id: uuid.UUID | None = None) -> list[str]:
    """Проверки из таблицы «Переменные правила»; возвращает предупреждения."""
    from ml.analysis.templates import validate_rule

    fields: dict[str, str] = {}
    for name in ("code", "title", "description", "prompt_template"):
        if not str(data.get(name) or "").strip():
            fields[name] = "Поле нужно заполнить"
    if data.get("level") not in ("critical", "warning"):
        fields["level"] = "Уровень — «Критично» или «Внимание»"
    if data.get("kind") not in ("semantic_llm", "llm"):
        fields["kind"] = "Тип правила — «Смысловой поиск + проверка ИИ» или «Проверка ИИ»"
    for i, var in enumerate(data.get("variables") or []):
        if var.get("type") not in ("text", "number", "list"):
            fields[f"variables[{i}].type"] = "Тип переменной — текст, число или список"
    if fields:
        raise ValidationFailed(fields)
    spec = RuleSpec(code=data["code"], title=data["title"], description=data["description"], level=data["level"],
                    kind=data["kind"], prompt_template=data["prompt_template"],
                    variables=[RuleVariable(**v) for v in data.get("variables") or []])
    errors, warnings = validate_rule(spec)
    for err in errors:
        fields.setdefault(err.field, err.message)
    clash = s.scalar(select(Rule.id).where(Rule.profile_id == profile_id, Rule.code == data["code"].strip(),
                                           Rule.deleted_at.is_(None), Rule.id != (rule_id or uuid.uuid4())))
    if clash:
        fields["code"] = f"Правило с кодом {data['code']} уже есть в профиле"
    if fields:
        raise ValidationFailed(fields)
    if data["kind"] == "llm" and data.get("enabled", True):
        n = s.scalar(select(func.count()).select_from(Rule).where(
            Rule.profile_id == profile_id, Rule.kind == "llm", Rule.enabled.is_(True), Rule.deleted_at.is_(None),
            Rule.id != (rule_id or uuid.uuid4())))
        if n >= MAX_LLM_RULES:
            raise ValidationFailed(
                {"kind": f"В профиле уже {n} правил «Проверка ИИ»: каждое читает документ целиком, "
                         f"больше {MAX_LLM_RULES} замедлят проверку"}, code="too_many_llm_rules")
    return warnings


def _new_version(s: Session, rule: Rule, *, actor_id, query_embedding) -> RuleVersion:
    version = RuleVersion(rule_id=rule.id, version=rule.version, snapshot=_snapshot(rule),
                          created_by=_id(actor_id) if actor_id else None,
                          query_embedding=list(query_embedding) if query_embedding is not None else None)
    s.add(version)
    s.flush()
    rule.current_version_id = version.id
    return version


def _save_new_rule(s: Session, profile_id: uuid.UUID, data: dict, *, actor_id=None, query_embedding=None) -> Rule:
    rule = Rule(profile_id=profile_id, code=data["code"].strip(), title=data["title"].strip(),
                description=data["description"].strip(), level=data["level"], kind=data["kind"],
                prompt_template=data["prompt_template"].strip(), variables=list(data.get("variables") or []),
                enabled=data.get("enabled", True), version=1, updated_by=_id(actor_id) if actor_id else None)
    s.add(rule)
    s.flush()
    _new_version(s, rule, actor_id=actor_id, query_embedding=query_embedding)
    s.flush()
    return rule


def _hits_30d(s: Session, rule_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    since = datetime.now(UTC) - timedelta(days=30)
    return dict(s.execute(
        select(Finding.rule_id, func.count()).where(Finding.rule_id.in_(rule_ids), Finding.source == "ai",
                                                     Finding.created_at >= since).group_by(Finding.rule_id)
    ).all()) if rule_ids else {}


def rule_view(s: Session, rule: Rule) -> dict:
    user = s.get(User, rule.updated_by) if rule.updated_by else None
    return {
        "id": str(rule.id), "profile_id": str(rule.profile_id), "code": rule.code, "title": rule.title,
        "description": rule.description, "level": rule.level, "kind": rule.kind,
        "prompt_template": rule.prompt_template, "variables": rule.variables or [], "enabled": rule.enabled,
        "hits_30d": _hits_30d(s, [rule.id]).get(rule.id, 0), "updated_at": iso(rule.updated_at),
        "updated_by": {"full_name": user.full_name} if user else None, "version": rule.version,
    }


def list_rules(s: Session, *, profile_id=None, search: str | None = None, kind: str | None = None) -> list[dict]:
    """`GET /api/rules`: строки таблицы правил, сортировка по коду."""
    q = select(Rule).where(Rule.deleted_at.is_(None),
                           Rule.profile_id == (_id(profile_id) if profile_id else default_profile_id(s)))
    if search:
        text = search.strip()
        q = q.where(or_(Rule.code.icontains(text, autoescape=True), Rule.title.icontains(text, autoescape=True)))
    if kind:
        q = q.where(Rule.kind == kind)
    rules = s.scalars(q.order_by(Rule.code)).all()
    hits = _hits_30d(s, [r.id for r in rules])
    return [{"id": str(r.id), "code": r.code, "title": r.title, "kind": r.kind, "level": r.level,
             "enabled": r.enabled, "hits_30d": hits.get(r.id, 0)} for r in rules]


def _rule(s: Session, rule_id) -> Rule:
    rule = s.get(Rule, _id(rule_id))
    if rule is None or rule.deleted_at is not None:
        raise NotFound("Правило не найдено")
    return rule


def get_rule(s: Session, rule_id) -> dict:
    return rule_view(s, _rule(s, rule_id))


def create_rule(s: Session, data: dict, *, actor_id=None, query_embedding=None) -> dict:
    """`POST /api/rules`: правило и его первая версия. Ответ — правило и `warnings`."""
    profile_id = _id(data["profile_id"]) if data.get("profile_id") else default_profile_id(s)
    warnings = _validate(s, profile_id, data)
    rule = _save_new_rule(s, profile_id, data, actor_id=actor_id, query_embedding=query_embedding)
    return rule_view(s, rule) | {"warnings": warnings}


def update_rule(s: Session, rule_id, data: dict, *, version: int, actor_id=None, query_embedding=None) -> dict:
    """`PATCH /api/rules/{id}`: любые поля и `version`; каждое сохранение — новая версия правила."""
    rule = s.scalar(select(Rule).where(Rule.id == _id(rule_id), Rule.deleted_at.is_(None)).with_for_update())
    if rule is None:
        raise NotFound("Правило не найдено")
    if rule.version != version:
        raise VersionConflict(details={"current": rule_view(s, rule)})
    merged = {f: getattr(rule, f) for f in _FIELDS} | {k: v for k, v in data.items() if k in _FIELDS}
    warnings = _validate(s, rule.profile_id, merged, rule_id=rule.id)
    for field in _FIELDS:
        value = merged[field]
        setattr(rule, field, value.strip() if isinstance(value, str) else value)
    rule.version += 1
    rule.updated_at = datetime.now(UTC)
    rule.updated_by = _id(actor_id) if actor_id else None
    # Эмбеддинг строки поиска переносится, если название и описание не менялись
    if query_embedding is None and not ({"title", "description", "variables"} & data.keys()):
        prev = s.get(RuleVersion, rule.current_version_id) if rule.current_version_id else None
        query_embedding = prev.query_embedding if prev is not None else None
    _new_version(s, rule, actor_id=actor_id, query_embedding=query_embedding)
    s.flush()
    return rule_view(s, rule) | {"warnings": warnings}


def delete_rule(s: Session, rule_id) -> None:
    """Мягкое удаление: старые находки сохраняют код и название правила."""
    rule = _rule(s, rule_id)
    rule.deleted_at = datetime.now(UTC)
    rule.enabled = False
    s.flush()


def import_rules(s: Session, profile_id, specs: Sequence[RuleSpec], *, actor_id=None) -> int:
    """Загрузка чек-листа в профиль (сид, импорт): правила с тем же кодом пропускаются."""
    profile_id = _id(profile_id)
    existing = set(s.scalars(select(Rule.code).where(Rule.profile_id == profile_id, Rule.deleted_at.is_(None))))
    added = 0
    for spec in specs:
        if spec.code in existing:
            continue
        data = spec.model_dump(include=set(_FIELDS))
        data["variables"] = [v.model_dump() for v in spec.variables]
        _save_new_rule(s, profile_id, data, actor_id=actor_id, query_embedding=spec.query_embedding)
        added += 1
    return added
