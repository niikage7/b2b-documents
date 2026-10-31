"""Финансовая нагрузка участника: сколько денег нужно и на какой срок — из значений паспорта закупки, без LLM.

Значение, которое нельзя прочитать однозначно, остаётся без числа: две разные суммы или проценты в одном поле
не складываются и не выбираются наугад. Суммы от процента считаются от НМЦК: цена контракта после торгов может быть ниже.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from contracts.analysis import CashCycle, FinancialLoad, SecurityItem

SECURITIES = ("application_security", "contract_security", "warranty_security")
_ABSENT = re.compile(r"\b(?:не\s+(?:требу|предусм|установл|устанав)|отсутств|нет\b|без\s+обеспеч)", re.I)
_NUMBER = r"\d{1,3}(?:[\s ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_IN_WORDS = r"(?:\([^)]*\)\s*)?"  # «30 (тридцать) процентов»
_PERCENT = re.compile(rf"({_NUMBER})\s*{_IN_WORDS}(?:%|процент)", re.I)
_MONEY = re.compile(rf"({_NUMBER})\s*{_IN_WORDS}(млн\.?|миллион\w*|тыс\.?|тысяч\w*)?\s*(?:руб|₽|российск)", re.I)
_SCALES = {"млн": 1_000_000, "мил": 1_000_000, "тыс": 1_000}
_TERM = re.compile(
    r"(\d+)\s*(?:\([^)]*\)\s*)?(?:(календарн\w*|рабоч\w*)\s+)?(дн\w*|день|месяц\w*)", re.I)
_MONTHS = {name: i + 1 for i, name in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}
_DATE_NUMERIC = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})(?:\D+(\d{1,2}):(\d{2}))?")
_DATE_WORDS = re.compile(r"\b(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\D+(\d{1,2}):(\d{2}))?", re.I)


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[\s ]", "", text).replace(",", "."))
    except InvalidOperation:
        return None


def parse_percent(value: str) -> float | None:
    """Единственный процент в значении; разные проценты — `None`."""
    found = {_number(m) for m in _PERCENT.findall(value)}
    return float(found.pop()) if len(found) == 1 and None not in found else None


def parse_money(value: str) -> float | None:
    """Единственная сумма в рублях; разные суммы — `None`."""
    found = set()
    for digits, scale in _MONEY.findall(value):
        number = _number(digits)
        if number is None:
            return None
        found.add(number * _SCALES.get(scale[:3].lower(), 1))
    return float(found.pop()) if len(found) == 1 else None


def parse_term_days(value: str) -> int | None:
    """Самый длинный срок в значении в календарных днях; рабочие дни — по пять из семи, месяц — 30 дней."""
    days = []
    for count, kind, unit in _TERM.findall(value):
        number = int(count)
        if unit.lower().startswith("месяц"):
            days.append(number * 30)
        elif kind.lower().startswith("рабоч"):
            days.append(math.ceil(number * 7 / 5))
        else:
            days.append(number)
    return max(days) if days else None


def parse_deadline(value: str) -> str | None:
    """Дата из значения в ISO: `2026-11-05T10:00` или `2026-11-05`, если времени нет; неверная дата — `None`."""
    numeric = _DATE_NUMERIC.search(value)
    if numeric:
        day, month, year, hour, minute = numeric.groups()
        parts = (int(year), int(month), int(day))
    else:
        words = _DATE_WORDS.search(value)
        month_number = _MONTHS.get(words.group(2).lower()) if words else None
        if not month_number:
            return None
        day, _, year, hour, minute = words.groups()
        parts = (int(year), month_number, int(day))
    try:
        moment = datetime(*parts, int(hour or 0), int(minute or 0))
    except ValueError:
        return None
    return moment.isoformat(timespec="minutes") if hour else moment.date().isoformat()


def _field(fields: Iterable[Any], key: str) -> tuple[str, str] | None:
    for field in fields:
        get = field.get if isinstance(field, dict) else lambda name, f=field: getattr(f, name, None)
        if get("key") == key and get("value"):
            return get("value"), get("label") or key
    return None


def _security(key: str, label: str, value: str, nmck: float | None) -> SecurityItem:
    money, percent = parse_money(value), parse_percent(value)
    if money is not None:
        return SecurityItem(key=key, label=label, value=value, required=True, amount=round(money, 2),
                            percent=percent, basis="stated")
    if percent is not None and nmck:
        return SecurityItem(key=key, label=label, value=value, required=True, amount=round(nmck * percent / 100, 2),
                            percent=percent, basis="percent_of_nmck")
    if _ABSENT.search(value):
        return SecurityItem(key=key, label=label, value=value, required=False)
    return SecurityItem(key=key, label=label, value=value, required=True, percent=percent)


def financial_load(fields: Iterable[Any]) -> FinancialLoad | None:
    """Обеспечения в рублях, срок отвлечения денег и дата окончания подачи заявок; `None`, если считать нечего.

    `fields` — поля паспорта: `PassportField` или их JSON из `batch.meta`.
    """
    fields = list(fields)
    nmck_field = _field(fields, "nmck")
    nmck = parse_money(nmck_field[0]) if nmck_field else None
    securities = []
    for key in SECURITIES:
        found = _field(fields, key)
        if found:
            securities.append(_security(key, found[1], found[0], nmck))
    amounts = [s.amount for s in securities if s.amount is not None]
    unknown = any(s.required and s.amount is None for s in securities)

    terms = {key: parse_term_days(found[0]) if (found := _field(fields, f"{key}_term")) else None
             for key in ("delivery", "acceptance", "payment")}
    cycle = None
    # Без срока поставки или оплаты длина цикла не имеет смысла; срок приёмки часто уже входит в срок оплаты
    if terms["delivery"] is not None and terms["payment"] is not None:
        cycle = CashCycle(delivery_days=terms["delivery"], acceptance_days=terms["acceptance"],
                          payment_days=terms["payment"],
                          total_days=terms["delivery"] + (terms["acceptance"] or 0) + terms["payment"])
    deadline = _field(fields, "deadline")
    deadline_at = parse_deadline(deadline[0]) if deadline else None
    advance = _field(fields, "advance")
    advance_amount = None
    if advance and not _ABSENT.search(advance[0]):
        advance_amount = parse_money(advance[0])
        percent = parse_percent(advance[0])
        if advance_amount is None and percent is not None and nmck:
            advance_amount = round(nmck * percent / 100, 2)

    if not securities and cycle is None and deadline_at is None:
        return None
    return FinancialLoad(nmck=nmck, securities=securities, total_security=round(sum(amounts), 2) if amounts else None,
                         total_incomplete=unknown, advance_amount=advance_amount, cash_cycle=cycle,
                         deadline_at=deadline_at)
