"""`build_structure` и `index_chunks` (FR-3.1, FR-4.2): сырые абзацы → блоки с разделами → фрагменты.

Раздел блока нужен каждой находке: «каждое замечание содержит ссылку на раздел» — критерий
приёмки. Правила:

- номер пункта приводится к виду раздела: «3.2.» → «3.2», «IV.» → «IV»;
- блок без номера наследует номер пункта выше: второй абзац пункта 3.2 — тоже «3.2»;
- подпункт «а)» получает номер вместе с пунктом: «3.2 а)»;
- номера в ячейках таблиц (номера строк спецификации) разделов не образуют, ячейка берёт
  раздел, в котором стоит таблица;
- заголовок без номера («Приложение № 1») начинает новый раздел и сбрасывает номер;
- `section_title` — ближайший заголовок выше; глубина заголовка — по номеру («4.2» глубже
  «4»), без номера — по уровню из парсера;
- сноски стоят в конце документа и раздела не получают: ссылкой служит страница.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from contracts.document import Block, ChunkRecord
from contracts.extraction import ExtractionResult, RawParagraph
from ml.analysis.blocks import chunk_blocks
from ml.analysis.embeddings import Embedder, embed_texts

_NAMESPACE = uuid.UUID("5b6c0f3e-2a7d-4f43-9b1e-3c2f0d4a8e61")
_DECIMAL = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?$")
_ROMAN = re.compile(r"^((?:раздел|глава|часть|статья)\s+)?([IVXLC]{1,7}|\d{1,3})\.?$", re.IGNORECASE)
_SUB = re.compile(r"^(?:[а-яёa-z]|\d{1,2})\)$", re.IGNORECASE)


def stable_id(document_id: str | None, kind: str, index: int) -> str:
    """UUID, который не меняется при повторе этапа: uuid5 от документа, вида объекта и номера."""
    if document_id is None:
        return f"{kind[0]}{index}"
    return str(uuid.uuid5(_NAMESPACE, f"{document_id}/{kind}/{index}"))


def normalize_number(raw: str | None) -> tuple[str | None, str | None, int | None]:
    """Номер как в документе → (номер раздела, вид, глубина). Вид: decimal, roman, sub, other."""
    if not raw:
        return None, None, None
    text = raw.strip()
    m = _DECIMAL.match(text)
    if m:
        number = m.group(1)
        return number, "decimal", number.count(".") + 1
    if _SUB.match(text):
        return text, "sub", None
    m = _ROMAN.match(text)
    if m:
        prefix = (m.group(1) or "").strip()
        value = m.group(2).upper()
        return (f"{prefix.capitalize()} {value}" if prefix else value), "roman", 1
    return text.rstrip("."), "other", None


def _kind(p: RawParagraph) -> str:
    if p.table is not None:
        return "table_cell"
    if p.heading_level:
        return "heading"
    if p.number or p.is_bullet:
        return "list_item"
    return "paragraph"


def build_structure(result: ExtractionResult, *, document_id: str | None = None) -> list[Block]:
    """Сырые абзацы → блоки с разделами (`section_number`, `section_title`, `section_block_id`)."""
    blocks: list[Block] = []
    headings: list[tuple[int, Block]] = []  # стек заголовков: (глубина, блок)
    current: str | None = None  # номер пункта, к которому относятся блоки без номера
    for p in sorted(result.paragraphs, key=lambda x: x.order):
        kind = _kind(p)
        number, number_kind, depth = normalize_number(p.number)
        in_table = p.table is not None
        block_id = stable_id(document_id, "block", p.order)
        level = None
        if kind == "heading":
            level = depth or p.heading_level or 1
        elif number_kind == "decimal":
            level = depth

        section_number = None
        if p.stream == "footnote":
            pass
        elif in_table:
            section_number = current
        elif kind == "heading":
            # Заголовок открывает раздел: его номер — номер раздела, без номера — раздел сбрасывается
            section_number = number if number_kind in ("decimal", "roman", "other") else None
            current = section_number
        elif number_kind == "decimal" or number_kind == "roman" or number_kind == "other":
            section_number = number
            current = number
        elif number_kind == "sub":
            section_number = f"{current} {number}" if current else number
        else:
            section_number = current

        block = Block(
            id=block_id,
            document_id=document_id,
            order=p.order,
            kind=kind,
            number=number,
            text=p.text,
            page=p.page,
            page_end=p.page_end,
            level=level,
            align=p.align,
            bold=p.bold,
            stream=p.stream,
            section_number=section_number,
            table_id=p.table.table_id if in_table else None,
            row=p.table.row if in_table else None,
            col=p.table.col if in_table else None,
            bbox=p.bbox,
            low_ocr_quality=p.low_ocr_quality,
            source_map=p.source_map,
        )
        if kind == "heading" and p.stream == "body":
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, block))
        if p.stream != "footnote" and headings:
            owner = headings[-1][1]
            block.section_block_id = owner.id
            block.section_title = owner.text
        blocks.append(block)
    return blocks


def build_chunks(blocks: Sequence[Block], *, document_id: str | None = None,
                 max_tokens: int | None = None) -> list[ChunkRecord]:
    """Блоки → фрагменты для таблицы `chunk` без эмбеддингов. Сноски в поиск не идут."""
    body = [b for b in sorted(blocks, key=lambda b: b.order) if b.stream != "footnote" and b.text.strip()]
    by_id = {b.id: b for b in body}
    out = []
    for i, chunk in enumerate(chunk_blocks(body, max_tokens=max_tokens)):
        pages = [by_id[bid].page for bid in chunk.block_ids] + [by_id[bid].page_end or by_id[bid].page
                                                                for bid in chunk.block_ids]
        out.append(ChunkRecord(id=stable_id(document_id, "chunk", i), document_id=document_id,
                               block_ids=list(chunk.block_ids), text=chunk.text,
                               page_from=min(pages), page_to=max(pages)))
    return out


def index_chunks(blocks: Sequence[Block], *, document_id: str | None = None,
                 embedder: Embedder | None = None) -> list[ChunkRecord]:
    """Этап `index_chunks`: фрагменты с эмбеддингами — строки таблицы `chunk`."""
    chunks = build_chunks(blocks, document_id=document_id)
    vectors = embed_texts([c.text for c in chunks], "passage", embedder=embedder)
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.embedding = vector
    return chunks
