"""Производные значения из раздела «Справочники и правила вычислений» «Требований к бэкенду».

Чистые функции без БД: их же зовут пересчёт после анализа, `PATCH` находки и тесты.
Цвет документа — худший уровень среди активных находок (не `rejected`), ИИ и ручных.
"""

from __future__ import annotations

from collections.abc import Iterable

LEVELS = ("critical", "warning")
FINDING_STATUSES = ("new", "accepted", "rejected", "edited")
DECIDED = ("accepted", "rejected", "edited")
CONFIDENCE_HIGH = 0.8
CONFIDENCE_MEDIUM = 0.5

# Допустимые переходы статуса находки: из → в
TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"accepted", "rejected", "edited"}),
    "accepted": frozenset({"new"}),
    "rejected": frozenset({"new"}),
    "edited": frozenset({"new", "accepted", "rejected"}),
}

# Порядок документов на разборе пакета: critical, warning, ok, в работе, not_checked
_DOCUMENT_ORDER = {"critical": 0, "warning": 1, "ok": 2, None: 3, "not_checked": 4}
_SEVERITY = {"critical": 2, "warning": 1}


def is_active(status: str) -> bool:
    return status != "rejected"


def can_transition(old: str, new: str) -> bool:
    return old == new or new in TRANSITIONS.get(old, frozenset())


def document_worst_level(status: str, active_levels: Iterable[str]) -> str | None:
    """`error` → `not_checked`; не `done` → `None`; иначе худший уровень активных находок или `ok`."""
    if status == "error":
        return "not_checked"
    if status != "done":
        return None
    worst = max(active_levels, key=lambda lv: _SEVERITY.get(lv, 0), default=None)
    return worst if worst in _SEVERITY else "ok"


def batch_worst_level(batch_status: str, document_levels: Iterable[str | None]) -> str | None:
    """Худший `worst_level` среди готовых документов; пакет в работе — `None`."""
    if batch_status in ("draft", "queued", "processing"):
        return None
    levels = [lv for lv in document_levels if lv in ("critical", "warning", "ok")]
    if not levels:
        return "not_checked"
    return max(levels, key=lambda lv: _SEVERITY.get(lv, 0))


def batch_status(document_statuses: Iterable[str]) -> str:
    """Все `done` → `done`; все закончились, есть `error` → `partial_error`; иначе `processing`."""
    statuses = list(document_statuses)
    if statuses and all(s == "done" for s in statuses):
        return "done"
    if statuses and all(s in ("done", "error") for s in statuses):
        return "partial_error"
    return "processing"


def confidence_level(confidence: float | None, *, high: float = CONFIDENCE_HIGH,
                     medium: float = CONFIDENCE_MEDIUM) -> str | None:
    if confidence is None:
        return None
    if confidence >= high:
        return "high"
    if confidence >= medium:
        return "medium"
    return "low"


def document_sort_key(worst_level: str | None) -> int:
    return _DOCUMENT_ORDER.get(worst_level, 3)


def initials(full_name: str) -> str:
    """«Мария Волкова» → «МВ»: для аватара."""
    parts = [p for p in full_name.split() if p]
    return "".join(p[0].upper() for p in parts[:2])
