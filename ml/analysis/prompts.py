"""Промпты движка правил: системная инструкция и запрос на группу правил.

Фрагменты в запросе перечислены один раз и пронумерованы; в инструкции правила
`{фрагмент}` заменяется ссылкой на его фрагменты («фрагменты [1], [3]»), так одна
группа из 3–4 правил укладывается в один вызов. Номер пункта показывается модели
рядом с текстом блока, но в цитату не входит.
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.analysis import RuleSpec
from ml.analysis.blocks import Fragment
from ml.analysis.templates import SUBJECT_UNKNOWN, render_template

PROMPT_VERSION = "2026-10-27.3"

SYSTEM = """Ты — юрист, который проверяет закупочную документацию по 44-ФЗ и 223-ФЗ на стороне поставщика: ищешь условия, из-за которых контракт будет трудно исполнить, а поставщик рискует штрафами, односторонним отказом заказчика и попаданием в реестр недобросовестных поставщиков.

Тебе дают пронумерованные фрагменты документа и правила проверки. Для каждого правила найди во фрагментах условия, которые правило велит проверить, и для каждого реши, есть ли в нём риск по этому правилу.

Правила ответа:
1. Цитата — дословный непрерывный отрывок из одного фрагмента длиной от 5 до 300 символов. Копируй текст символ в символ: без многоточий, без номера пункта, без пересказа и без своих слов. Цитируй условие вместе с его значением — размером, сроком, суммой; подпись поля без значения не цитируй.
2. check — одно предложение до 20 слов: что сказано в условии и с чем из правила ты его сравниваешь. Например: «срок поставки — 30 календарных дней с даты заключения контракта; риск, если меньше 10 дней».
3. risk — вердикт. true — только если условие нарушает правило: превышает порог из правила или подпадает под описанный в нём риск. false — условие в пределах порога (значение, равное порогу, тоже в пределах), соответствует закону и обычной практике или правило прямо его исключает. Находкой станут только условия с risk=true; условия с risk=false можно вернуть коротко, в отчёт они не попадут: quote — первые 5–10 слов условия, explanation и recommendation — пустые строки. Так ответ не упрётся в лимит длины.
4. Одна находка — одно место в документе. Если риск по правилу встречается в нескольких местах — сделай несколько находок.
5. Если во фрагментах нет условий по правилу, верни для него пустой список. Не придумывай риски, которых нет в тексте.
6. Пояснение (explanation) — 1–2 предложения на русском для юриста: в чём риск для поставщика и какое условие из текста к нему ведёт. Не повторяй цитату целиком.
7. Рекомендация (recommendation) — одно короткое предложение: что поставщику сделать именно с этим условием до подачи заявки. Например: направить запрос на разъяснение положений извещения и попросить указать «или эквивалент»; предложить заказчику срок не меньше N дней; заложить стоимость банковской гарантии в цену; проверить наличие лицензии. Поставщик не может требовать — он просит или предлагает. Без общих фраз вроде «внимательно изучить условия».
8. confidence — уверенность от 0 до 1 в вердикте risk.
9. fragment — номер фрагмента, из которого взята цитата.

Ответ — только JSON по заданной схеме, по одному элементу на каждое правило из запроса."""


def render_fragments(fragments: Sequence[Fragment]) -> str:
    parts = []
    for fragment in fragments:
        lines = []
        pages = sorted({getattr(b, "page", None) for b in fragment.blocks} - {None})
        where = f"стр. {pages[0]}" if len(pages) == 1 else (f"стр. {pages[0]}–{pages[-1]}" if pages else "")
        for block in fragment.blocks:
            number = getattr(block, "number", None)
            text = fragment.excerpts.get(block.id, block.text)
            lines.append(f"{number} {text}" if number else text)
        # Фрагменты короткие (около пункта договора): название раздела возвращает модели контекст
        title = next((t for b in fragment.blocks if (t := getattr(b, "section_title", None))), None)
        if title and title != fragment.blocks[0].text:
            where = f"{where}, раздел «{title[:120]}»" if where else f"раздел «{title[:120]}»"
        header = f"[{fragment.index}]" + (f" {where}" if where else "")
        parts.append(header + "\n" + "\n".join(lines))
    return "\n\n".join(parts)


def fragment_ref(indexes: Sequence[int]) -> str:
    if not indexes:
        return "фрагменты документа"
    if len(indexes) == 1:
        return f"фрагмент [{indexes[0]}]"
    return "фрагменты " + ", ".join(f"[{i}]" for i in indexes)


def group_prompt(
    rules: Sequence[RuleSpec],
    fragments: Sequence[Fragment],
    rule_fragments: dict[str, list[int]],
    *,
    subject: str | None,
) -> list[dict]:
    rule_parts = []
    for rule in rules:
        indexes = rule_fragments.get(rule.code, [])
        instruction = render_template(rule.prompt_template, rule, fragment=fragment_ref(indexes), subject=subject)
        part = [f"### {rule.code}. {rule.title}", f"Суть риска: {rule.description}", f"Инструкция: {instruction}"]
        if indexes:
            part.append(f"Где искать: {fragment_ref(indexes)}.")
        rule_parts.append("\n".join(part))
    codes = ", ".join(r.code for r in rules)
    user = (
        f"Предмет закупки: {subject or SUBJECT_UNKNOWN}\n\n"
        f"Фрагменты документа:\n\n{render_fragments(fragments)}\n\n"
        f"Правила проверки:\n\n" + "\n\n".join(rule_parts) + "\n\n"
        f"Верни JSON вида {{\"rules\": [{{\"rule_code\": \"...\", \"findings\": [{{\"fragment\": 1, \"quote\": \"...\", "
        f"\"check\": \"...\", \"risk\": true, \"explanation\": \"...\", \"recommendation\": \"...\", "
        f"\"confidence\": 0.8}}]}}]}} — по одному элементу "
        f"на каждое правило: {codes}."
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


META_SYSTEM = """Ты читаешь начало закупочного документа (обычно извещения о закупке) и определяешь предмет закупки — что покупает заказчик, одной короткой фразой в именительном падеже, как в поле «Наименование объекта закупки». Если предмет в тексте не указан, верни null. Ответ — только JSON по схеме."""


def meta_prompt(text: str) -> list[dict]:
    return [
        {"role": "system", "content": META_SYSTEM},
        {"role": "user", "content": f"Текст документа:\n\n{text}\n\nВерни JSON: {{\"subject\": \"...\" или null}}"},
    ]
