"""Вывод по пакету для руководителя: участвовать ли и что сделать до подачи заявки.

Итог считает код по находкам, которые остались после ревью, а не модель: цвет и совет не должны расходиться.
Модель только пишет связный текст и выбирает главные риски по номерам из списка, поэтому в вывод не попадает
риск, которого нет среди находок. Без LLM вывод собирается шаблоном.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from contracts.analysis import BriefRisk, PackageBrief
from ml.analysis.llm import LLMClient, default_client
from ml.config import get_settings
from ml.errors import MLError

log = logging.getLogger(__name__)

MAX_FINDINGS = 40
MAX_TOP = 5
DECISIONS = {
    "go": "Существенных рисков не найдено",
    "caution": "Можно участвовать, учитывая риски",
    "clarify": "Сначала снять критичные риски",
}
_SEVERITY = {"critical": 0, "warning": 1}


class _Answer(BaseModel):
    summary: str = Field(description="2–3 предложения: что за закупка и главное, что надо учесть")
    top: list[int] = Field(default_factory=list, description="Номера главных рисков из списка, до пяти")
    actions: list[str] = Field(default_factory=list, description="Что сделать до подачи заявки, до четырёх шагов")


SYSTEM = """Ты — юрист поставщика. Пишешь короткий вывод по закупочной документации для руководителя, который решает, участвовать ли в закупке. Итог проверки уже определён, не меняй и не оспаривай его.

Правила:
1. summary — 2–3 предложения деловым языком: что закупается и на что в первую очередь обратить внимание. Без общих фраз и без пересказа всех находок.
2. top — номера до пяти самых опасных рисков из списка: сначала те, что ведут к одностороннему отказу заказчика, штрафам и реестру недобросовестных поставщиков.
3. actions — до четырёх конкретных шагов до подачи заявки: запрос о разъяснении по такому-то условию, закладка расходов в цену, проверка документа. Поставщик просит, а не требует.
4. Опирайся только на данные запроса. Не придумывай условий, сумм и норм закона.

Ответ — только JSON по заданной схеме."""


def decision_of(findings: Sequence[dict]) -> str:
    levels = {f.get("level") for f in findings}
    return "clarify" if "critical" in levels else ("caution" if "warning" in levels else "go")


def _title(finding: dict) -> str:
    rule = finding.get("rule") or {}
    return rule.get("title") or "Ручная пометка"


def _risk(finding: dict) -> BriefRisk:
    return BriefRisk(finding_id=finding.get("id"), document_id=finding.get("document_id"), title=_title(finding),
                     level=finding.get("level") or "warning", document=finding.get("filename"),
                     section_number=finding.get("section_number"), page=finding.get("page"),
                     quote=(finding.get("quote") or "")[:300])


def package_brief(
    findings: Sequence[dict],
    *,
    subject: str | None = None,
    passport: Sequence[dict] = (),
    documents: int | None = None,
    not_checked: int = 0,
    llm: LLMClient | None = None,
    use_llm: bool = True,
) -> PackageBrief:
    """Вывод по активным находкам пакета (`db.exports.clarification_input`) и полям паспорта закупки."""
    ranked = sorted(findings, key=lambda f: _SEVERITY.get(f.get("level"), 2))[:MAX_FINDINGS]
    decision = decision_of(ranked)
    critical = sum(f.get("level") == "critical" for f in ranked)
    counts = f"критичных находок — {critical}, требующих внимания — {len(ranked) - critical}"
    brief = PackageBrief(
        decision=decision, decision_label=DECISIONS[decision],
        summary=f"По документам пакета {counts}." if ranked else "Проверка по правилам чек-листа рисков не нашла.",
        top_risks=[_risk(f) for f in ranked[:MAX_TOP]],
        actions=["Направить заказчику запрос о даче разъяснений по критичным условиям."] if critical else [],
        not_checked=not_checked,
    )
    if not use_llm or not ranked or not (llm is not None or get_settings().llm_model):
        return brief
    risks = "\n".join(
        f"[{i}] {f.get('level')}: {_title(f)}; {f.get('filename') or 'документ'}"
        f"{', п. ' + f['section_number'] if f.get('section_number') else ''}: «{(f.get('quote') or '')[:200]}». "
        f"{(f.get('explanation') or '')[:300]}"
        for i, f in enumerate(ranked, 1))
    facts = "\n".join(f"- {p.get('label')}: {p.get('value')}" for p in passport) or "нет данных"
    user = (f"Предмет закупки: {subject or 'не указан'}\nДокументов в пакете: {documents or 'не указано'}\n"
            f"Итог проверки: {DECISIONS[decision]} ({counts})\n\nПаспорт закупки:\n{facts}\n\nРиски:\n{risks}\n\n"
            "Верни JSON вида {\"summary\": \"...\", \"top\": [1, 2], \"actions\": [\"...\"]}.")
    try:
        answer = (llm or default_client()).chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], _Answer,
            schema_name="package_brief", max_tokens=900, reasoning="none")
    except MLError as exc:
        log.warning("brief: LLM недоступна, вывод собран шаблоном: %s", exc)
        return brief
    brief.usage = answer.usage
    if answer.data.summary.strip():
        brief.summary = answer.data.summary.strip()
    top = [ranked[i - 1] for i in dict.fromkeys(answer.data.top) if 1 <= i <= len(ranked)][:MAX_TOP]
    if top:
        brief.top_risks = [_risk(f) for f in top]
    actions = [a.strip() for a in answer.data.actions if a.strip()][:4]
    if actions:
        brief.actions = actions
    return brief
