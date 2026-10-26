"""Паспорт закупки: ключевые условия пакета одной карточкой — НМЦК, обеспечения, сроки, гарантия.

Тендерный специалист первым делом выписывает эти условия из извещения и проекта контракта:
по ним решают, участвовать ли. Здесь они собираются одним вызовом LLM:

1. для каждого поля — фрагменты документов пакета, найденные гибридным поиском (векторы и
   BM25) по строке поля, в пределах небольшого бюджета;
2. LLM заполняет поля коротким значением и дословной цитатой;
3. цитата привязывается к тексту блока, как у находок: значение без цитаты в тексте
   отбрасывается — в паспорт не попадает ничего, что нельзя показать в документе.

Паспорт пишется в `batch.meta` на этапе `extract_batch_meta`; предмет закупки из паспорта
подставляется в правила, если шаблон строки его не нашёл.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from contracts.analysis import LLMUsage, PassportField, ProcurementPassport
from ml.analysis.anchor import anchor_quote
from ml.analysis.blocks import BlockLike, Chunk, chunk_blocks, estimate_tokens
from ml.analysis.embeddings import Embedder, embed_texts
from ml.analysis.llm import LLMClient, default_client
from ml.analysis.search import ChunkSearch, InMemorySearch, LexicalIndex, rank_chunks, take_budget
from ml.errors import LLMError

log = logging.getLogger(__name__)

PASSPORT_VERSION = "2026-10-27"
FIELD_BUDGET_TOKENS = 400
"""Сколько текста получает каждое поле из всех документов пакета: условие обычно в одном пункте."""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    query: str
    hint: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("subject", "Предмет закупки", "Наименование объекта закупки, предмет контракта",
              "что покупает заказчик, коротко, в именительном падеже"),
    FieldSpec("number", "Номер закупки", "Номер извещения, реестровый номер закупки, электронный аукцион №",
              "номер закупки, например 0373100000126000045"),
    FieldSpec("procedure", "Способ закупки", "Способ определения поставщика: электронный аукцион, конкурс, "
              "запрос котировок", "например, «электронный аукцион»"),
    FieldSpec("customer", "Заказчик", "Наименование заказчика, учреждение, ИНН заказчика",
              "название заказчика без реквизитов"),
    FieldSpec("nmck", "НМЦК", "Начальная (максимальная) цена контракта, цена контракта составляет рублей",
              "сумма с валютой, например «4 850 000,00 руб.»"),
    FieldSpec("application_security", "Обеспечение заявки", "Размер обеспечения заявки на участие в закупке",
              "процент или сумма"),
    FieldSpec("contract_security", "Обеспечение исполнения контракта",
              "Размер обеспечения исполнения контракта", "процент или сумма"),
    FieldSpec("warranty_security", "Обеспечение гарантийных обязательств",
              "Размер обеспечения гарантийных обязательств", "процент или сумма"),
    FieldSpec("advance", "Аванс", "Аванс, авансовый платёж, предоплата", "размер аванса или «не предусмотрен»"),
    FieldSpec("delivery_term", "Срок поставки", "Срок поставки товара, поставка осуществляется в течение дней",
              "срок и от какой даты он считается"),
    FieldSpec("delivery_place", "Место поставки", "Место поставки товара, адрес поставки", "адрес"),
    FieldSpec("acceptance_term", "Срок приёмки", "Приёмка товара осуществляется в течение, экспертиза",
              "срок приёмки"),
    FieldSpec("payment_term", "Срок оплаты", "Оплата поставленного товара производится в течение дней",
              "срок оплаты и от какой даты"),
    FieldSpec("warranty_term", "Гарантийный срок", "Гарантийный срок на товар, гарантия качества месяцев",
              "срок гарантии"),
    FieldSpec("deadline", "Окончание подачи заявок", "Дата и время окончания срока подачи заявок",
              "дата и время"),
)


class _Value(BaseModel):
    key: str
    value: str = Field(description="Короткое значение поля")
    fragment: int | None = None
    quote: str = Field(description="Дословная цитата, где указано значение")


class _PassportAnswer(BaseModel):
    fields: list[_Value]


SYSTEM = """Ты помогаешь тендерному специалисту поставщика: по фрагментам закупочной документации заполняешь паспорт закупки — короткую карточку ключевых условий.

Правила:
1. Заполняй только поля, значение которых прямо указано во фрагментах. Не вычисляй и не придумывай: нет значения — не включай поле в ответ.
2. value — короткое значение для карточки, до 120 символов, своими словами допустимо только сокращение.
3. quote — дословный непрерывный отрывок из фрагмента, где указано значение, 5–300 символов, символ в символ, без номера пункта.
4. fragment — номер фрагмента с цитатой.
5. Если в разных документах значения расходятся, выбери значение из проекта контракта.

Ответ — только JSON по заданной схеме."""


def _render(fields: Sequence[FieldSpec], fragments: list[tuple[int, str, str]]) -> list[dict]:
    parts = [f"[{i}] {name}\n{text}" for i, name, text in fragments]
    wanted = "\n".join(f"- {f.key} — {f.label}: {f.hint}" for f in fields)
    user = ("Фрагменты документов:\n\n" + "\n\n".join(parts) + f"\n\nПоля паспорта:\n{wanted}\n\n"
            "Верни JSON вида {\"fields\": [{\"key\": \"nmck\", \"value\": \"...\", \"fragment\": 1, "
            "\"quote\": \"...\"}]}.")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def extract_passport(
    documents: Sequence[tuple[str, str, Sequence[BlockLike]]],
    *,
    search: ChunkSearch | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    fields: Sequence[FieldSpec] = FIELDS,
    budget_tokens: int = FIELD_BUDGET_TOKENS,
) -> ProcurementPassport:
    """Паспорт закупки по документам пакета: [(document_id, имя файла, блоки)].

    `search` — фрагменты из таблицы `chunk` (`PgVectorSearch`); без него фрагменты
    нарезаются и индексируются в памяти. Ошибка LLM не роняет этап: паспорт будет пустым.
    """
    passport = ProcurementPassport(fields=[])
    if not documents:
        return passport
    store = search
    chunks_by_doc: dict[str, list[Chunk]] = {}
    if store is None:
        memory = InMemorySearch()
        for doc_id, _, blocks in documents:
            memory.add(doc_id, chunk_blocks(sorted(blocks, key=lambda b: b.order), prefix=f"{doc_id}:"),
                       embedder=embedder)
        store = memory
    for doc_id, _, _ in documents:
        chunks_by_doc[doc_id] = store.chunks(doc_id)
    lexical = {doc_id: LexicalIndex([c.text for c in chunks]) for doc_id, chunks in chunks_by_doc.items() if chunks}
    vectors = embed_texts([f.query for f in fields], "query", embedder=embedder)

    # Фрагменты всех полей одним списком: блоки одного документа подряд объединяются
    selected: dict[str, set[str]] = {doc_id: set() for doc_id, _, _ in documents}
    for spec, vector in zip(fields, vectors, strict=True):
        hits = []
        for doc_id, chunks in chunks_by_doc.items():
            if chunks:
                ranked = rank_chunks(chunks, vector, f"{spec.label} {spec.query}", lexical=lexical[doc_id])
                hits += [(doc_id, h) for h in take_budget(ranked, budget_tokens)]
        # Лучшие фрагменты поля из всех документов: у RRF первые места документов равны,
        # поэтому каждый документ получает свою долю бюджета
        hits.sort(key=lambda x: -x[1].score)
        used = 0
        for doc_id, hit in hits:
            tokens = estimate_tokens(hit.text)
            if used and used + tokens > budget_tokens:
                break
            selected[doc_id].update(hit.block_ids)
            used += tokens

    fragments: list[tuple[int, str, str]] = []
    fragment_blocks: dict[int, tuple[str, list[BlockLike]]] = {}
    for doc_id, filename, blocks in documents:
        run: list[BlockLike] = []
        ordered = sorted(blocks, key=lambda b: b.order)
        for pos, block in enumerate(ordered):
            if block.id in selected[doc_id]:
                run.append(block)
            if run and (block.id not in selected[doc_id] or pos == len(ordered) - 1):
                index = len(fragments) + 1
                text = "\n".join(f"{b.number} {b.text}" if getattr(b, "number", None) else b.text for b in run)
                fragments.append((index, filename, text))
                fragment_blocks[index] = (doc_id, list(run))
                run = []
    if not fragments:
        return passport

    client = llm or default_client()
    try:
        answer = client.chat(_render(fields, fragments), _PassportAnswer, schema_name="procurement_passport",
                             max_tokens=1500, reasoning="none")
    except LLMError as exc:
        log.warning("passport: LLM недоступна, паспорт не заполнен: %s", exc)
        return passport
    usage = LLMUsage()
    usage.add(answer.usage)
    labels = {f.key: f.label for f in fields}
    names = {doc_id: filename for doc_id, filename, _ in documents}
    seen: set[str] = set()
    all_blocks = [(doc_id, b) for doc_id, blocks in fragment_blocks.values() for b in blocks]
    for item in answer.data.fields:
        if item.key not in labels or item.key in seen or not item.value.strip():
            continue
        preferred = fragment_blocks.get(item.fragment or 0)
        candidates = preferred[1] if preferred else [b for _, b in all_blocks]
        rest = [b for _, b in all_blocks if b not in candidates]
        anchor, score = anchor_quote(item.quote, candidates, fallback=rest)
        if anchor is None:
            log.info("passport: %s отброшено, цитата не найдена (балл %.0f): %.80s", item.key, score, item.quote)
            continue
        doc_id = next(d for d, b in all_blocks if b is anchor.block)
        block = anchor.block
        seen.add(item.key)
        passport.fields.append(PassportField(
            key=item.key, label=labels[item.key], value=item.value.strip()[:200],
            quote=block.text[anchor.start:anchor.end], document_id=doc_id, filename=names.get(doc_id),
            block_id=block.id, start=anchor.start, end=anchor.end, page=getattr(block, "page", None),
            section_number=getattr(block, "section_number", None) or getattr(block, "number", None),
        ))
    order = {f.key: i for i, f in enumerate(fields)}
    passport.fields.sort(key=lambda f: order[f.key])
    passport.usage = usage
    return passport
