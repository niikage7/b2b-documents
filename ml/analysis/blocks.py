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
from ml.config import get_settings

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
    """Номер пункта для ссылки на место: раздел блока («3.2 а)»), иначе его собственный номер."""
    return getattr(block, "section_number", None) or getattr(block, "number", None)


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


def blocks_from_extraction(result: ExtractionResult, *, document_id: str | None = None):
    """Сырые абзацы → блоки с разделами: то же, что этап `build_structure`, для стендов и тестов."""
    from ml.structure import build_structure

    return build_structure(result, document_id=document_id)


def chunk_blocks(blocks: Sequence[BlockLike], *, max_tokens: int | None = None, prefix: str = "c",
                 overlap: int | None = None) -> list[Chunk]:
    """Фрагменты до `max_tokens` по границам блоков с перекрытием в `overlap` блоков.

    По умолчанию — `CHUNK_TOKENS` и `CHUNK_OVERLAP`: фрагмент размером с пункт договора,
    короткие блоки (заголовок, подпункт) склеиваются с соседними, длинный блок — один фрагмент.
    """
    settings = get_settings()
    max_tokens = max_tokens or settings.chunk_tokens
    overlap = settings.chunk_overlap if overlap is None else overlap
    chunks: list[Chunk] = []
    current: list[BlockLike] = []
    tokens = 0
    for block in blocks:
        t = estimate_tokens(block.text)
        if current and tokens + t > max_tokens:
            chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
            current = current[-overlap:] if overlap else []
            tokens = sum(estimate_tokens(b.text) for b in current)
        current.append(block)
        tokens += t
    if current:
        chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
    return chunks


def _chunk(blocks: list[BlockLike], chunk_id: str) -> Chunk:
    text = "\n".join(f"{section_of(b)} {b.text}" if section_of(b) else b.text for b in blocks)
    # Название раздела в начале фрагмента: короткий пункт без него теряет тему для поиска
    title = getattr(blocks[0], "section_title", None)
    if title and title != blocks[0].text:
        text = f"{title}\n{text}"
    return Chunk(id=chunk_id, block_ids=[b.id for b in blocks], text=text)
