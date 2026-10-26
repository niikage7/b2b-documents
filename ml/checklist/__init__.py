"""Стартовый чек-лист R-01…R-08 профиля «44-ФЗ, поставка товаров» для сида БД."""

from __future__ import annotations

import json
from importlib import resources

from contracts.analysis import RuleSpec


def load_default_checklist() -> tuple[dict, list[RuleSpec]]:
    """Профиль (`name`, `short_name`, `is_default`) и правила для первого запуска."""
    data = json.loads(resources.files(__package__).joinpath("r44_default.json").read_text(encoding="utf-8"))
    return data["profile"], [RuleSpec.model_validate(rule) for rule in data["rules"]]
