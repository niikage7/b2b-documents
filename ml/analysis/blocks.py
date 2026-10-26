"""Блоки и фрагменты в том виде, в каком их читает движок правил.

Движок принимает любые объекты с полями блока из «Требований к бэкенду» (`id`, `order`,
`text`, `number`, `page`, `section_number`, `section_title`) — например, Pydantic-модели
Андрея. Здесь же упрощённые блоки и нарезка для `test_rule` по тексту и для тестов:
они не заменяют `build_structure` и `index_chunks`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from contracts.extraction import ExtractionResult

CHARS_PER_TOKEN = 3.5
"""Оценка для русского текста у современных токенизаторов: 3–4 знака на токен."""


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


class BlockLike(Protocol):
    id: str
    order: int
    text: str
    number: str | None
    page: int


@dataclass
class SimpleBlock:
    id: str
    order: int
    text: str
    number: str | None = None
    page: int = 1
    section_number: str | None = None
    section_title: str | None = None


@dataclass
class Chunk:
    id: str
    block_ids: list[str]
    text: str
    embedding: list[float] | None = None


@dataclass
class ChunkHit:
    chunk_id: str
    block_ids: list[str]
    score: float
    text: str = ""


@dataclass
class Fragment:
    """Фрагмент в запросе к LLM: несколько блоков подряд под одним номером."""

    index: int
    blocks: list[BlockLike] = field(default_factory=list)


def section_of(block: BlockLike) -> str | None:
    """Номер пункта для показа модели и в ответе `test_rule`: свой номер или раздел блока."""
    return getattr(block, "number", None) or getattr(block, "section_number", None)


_SPLIT = re.compile(r"\n\s*\n|\n(?=\s*(?:\d{1,3}(?:\.\d{1,3})*[.)]|[а-яa-z]\))\s)")
_NUMBER = re.compile(r"^\s*((?:\d{1,3}(?:\.\d{1,3})*\.?|\d{1,3}\)|[а-яa-z]\)))\s+")


def blocks_from_text(text: str, *, prefix: str = "t") -> list[SimpleBlock]:
    """Текст из поля «Проверить на тексте» → блоки: абзацы по пустым строкам и номерам пунктов."""
    blocks: list[SimpleBlock] = []
    section = None
    for part in _SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        number = None
        m = _NUMBER.match(part)
        if m:
            number = m.group(1)
            part = part[m.end():].strip()
            section = number
        blocks.append(SimpleBlock(id=f"{prefix}{len(blocks)}", order=len(blocks), text=part, number=number,
                                  section_number=section))
    return blocks


def blocks_from_extraction(result: ExtractionResult, *, prefix: str = "b") -> list[SimpleBlock]:
    """Сырые абзацы → блоки без иерархии разделов: для стендов и тестов движка."""
    blocks: list[SimpleBlock] = []
    section = None
    title = None
    for item in result.paragraphs:
        if item.number:
            section = item.number
        if item.heading_level:
            title = item.text
        blocks.append(SimpleBlock(id=f"{prefix}{item.order}", order=item.order, text=item.text, number=item.number,
                                  page=item.page, section_number=section, section_title=title))
    return blocks


def chunk_blocks(blocks: Sequence[BlockLike], *, max_tokens: int = 400, prefix: str = "c") -> list[Chunk]:
    """Фрагменты по 300–500 токенов по границам блоков с перекрытием в один блок."""
    chunks: list[Chunk] = []
    current: list[BlockLike] = []
    tokens = 0
    for block in blocks:
        t = estimate_tokens(block.text)
        if current and tokens + t > max_tokens:
            chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
            current = current[-1:]
            tokens = estimate_tokens(current[0].text)
        current.append(block)
        tokens += t
    if current:
        chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
    return chunks


def _chunk(blocks: list[BlockLike], chunk_id: str) -> Chunk:
    text = "\n".join(f"{section_of(b)} {b.text}" if section_of(b) else b.text for b in blocks)
    return Chunk(id=chunk_id, block_ids=[b.id for b in blocks], text=text)
