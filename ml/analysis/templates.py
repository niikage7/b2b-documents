"""Шаблон инструкции правила: переменные, подстановка и проверка (FR-9.2).

Системные переменные `{фрагмент}` и `{предмет_закупки}` заполняет система,
пользовательские хранятся в `rule.variables`. Проверки совпадают с таблицей «Переменные
правила» из «Требований к бэкенду»: API может звать `validate_rule` и отдавать ошибки
в `details.fields` как есть.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contracts.analysis import SYSTEM_VARIABLES, RuleSpec
from ml.errors import RuleTemplateError

_VAR = re.compile(r"\{([^{}\s]{1,40})\}")
_NAME = re.compile(r"^[\wа-яА-ЯёЁ]{1,40}$")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")
SUBJECT_UNKNOWN = "не указан"


@dataclass
class FieldError:
    field: str
    message: str


def template_variables(template: str) -> list[str]:
    return list(dict.fromkeys(_VAR.findall(template)))


def validate_rule(rule: RuleSpec) -> tuple[list[FieldError], list[str]]:
    """Ошибки (422) и предупреждения правила. Тексты — для методолога, на русском."""
    errors: list[FieldError] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for i, var in enumerate(rule.variables):
        if not _NAME.match(var.name):
            errors.append(FieldError(f"variables[{i}].name", "Имя — буквы, цифры и «_», от 1 до 40 символов"))
        elif var.name in SYSTEM_VARIABLES:
            errors.append(FieldError(f"variables[{i}].name", f"Имя «{var.name}» занято системной переменной"))
        elif var.name in seen:
            errors.append(FieldError(f"variables[{i}].name", f"Переменная «{var.name}» уже описана"))
        seen.add(var.name)
        value = var.value.strip()
        if var.type == "number" and not _NUMBER.match(value):
            errors.append(FieldError(f"variables[{i}].value", "Нужно число, например 10 или 0,5"))
        elif var.type == "list" and not [v for v in value.split(";") if v.strip()]:
            errors.append(FieldError(f"variables[{i}].value", "Список — значения через точку с запятой"))
        elif var.type == "text" and len(var.value) > 2000:
            errors.append(FieldError(f"variables[{i}].value", "Текст — не длиннее 2000 символов"))
    used = template_variables(rule.prompt_template)
    for name in used:
        if name not in seen and name not in SYSTEM_VARIABLES:
            errors.append(FieldError("prompt_template", f"Переменная {{{name}}} не описана"))
    if rule.kind == "semantic_llm" and "фрагмент" not in used:
        errors.append(FieldError("prompt_template", "В инструкции смыслового правила нужна переменная {фрагмент}"))
    if not rule.prompt_template.strip():
        errors.append(FieldError("prompt_template", "Инструкция для ИИ не заполнена"))
    for name in seen - set(used):
        warnings.append(f"Переменная {{{name}}} описана, но не используется в инструкции")
    return errors, warnings


def _value(rule: RuleSpec, name: str) -> str | None:
    for var in rule.variables:
        if var.name == name:
            if var.type == "list":
                return ", ".join(v.strip() for v in var.value.split(";") if v.strip())
            return var.value.strip()
    return None


def render_template(template: str, rule: RuleSpec, *, fragment: str, subject: str | None) -> str:
    """Подставляет переменные. Неизвестная переменная — ошибка: такое правило не прошло бы проверку API."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "фрагмент":
            return fragment
        if name == "предмет_закупки":
            return subject or SUBJECT_UNKNOWN
        value = _value(rule, name)
        if value is None:
            raise RuleTemplateError(f"В правиле {rule.code} переменная {{{name}}} не описана")
        return value

    return _VAR.sub(repl, template)


def rule_query_text(rule: RuleSpec) -> str:
    """Строка смыслового поиска: название и описание правила с подставленными переменными."""
    text = f"{rule.title}. {rule.description}"

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in SYSTEM_VARIABLES:
            return ""
        return _value(rule, name) or ""

    return re.sub(r"\s+", " ", _VAR.sub(repl, text)).strip()


def rule_search_text(rule: RuleSpec) -> str:
    """Строка лексического поиска (BM25): название, описание и инструкция правила.

    В инструкции методолог перечисляет термины, по которым узнаётся условие («пеня»,
    «ключевая ставка», «обеспечение заявки»), — для поиска по словам они важнее описания.
    """
    instruction = _VAR.sub(lambda m: "" if m.group(1) in SYSTEM_VARIABLES else (_value(rule, m.group(1)) or ""),
                           rule.prompt_template)
    return re.sub(r"\s+", " ", f"{rule_query_text(rule)} {instruction}").strip()
