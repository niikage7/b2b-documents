"""Стартовые чек-листы R-01…R-08 для сида БД: 44-ФЗ (профиль по умолчанию) и 223-ФЗ."""

from __future__ import annotations

import json
from importlib import resources

from contracts.analysis import RuleSpec


def _load(filename: str) -> tuple[dict, list[RuleSpec]]:
    data = json.loads(resources.files(__package__).joinpath(filename).read_text(encoding="utf-8"))
    return data["profile"], [RuleSpec.model_validate(rule) for rule in data["rules"]]


def load_default_checklist() -> tuple[dict, list[RuleSpec]]:
    """Профиль (`name`, `short_name`, `is_default`) и правила для первого запуска."""
    return _load("r44_default.json")


def load_extra_checklists() -> list[tuple[dict, list[RuleSpec]]]:
    """Профили, которые выбирают при загрузке пакета: не по умолчанию."""
    return [_load("r223_default.json")]
