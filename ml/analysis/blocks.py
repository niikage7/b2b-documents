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
LONG_BLOCK_TOKENS = 300
"""Блок длиннее режется для поиска на окна по предложениям: ячейка таблицы на 2 000 токенов
целиком заняла бы бюджет правила, а LLM получает только найденные окна блока."""


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
    start: int | None = None
    """Окно одного длинного блока: смещения начала и конца в тексте блока. None — целые блоки."""
    end: int | None = None


@dataclass
class ChunkHit:
    chunk_id: str
    block_ids: list[str]
    score: float
    text: str = ""
    start: int | None = None
    end: int | None = None


@dataclass
class Fragment:
    """Фрагмент в запросе к LLM: несколько блоков подряд под одним номером."""

    index: int
    blocks: list[BlockLike] = field(default_factory=list)
    excerpts: dict[str, str] = field(default_factory=dict)
    """Текст блока для запроса вместо `block.text`: найденные окна длинного блока."""


Selection = dict[str, list[tuple[int, int]] | None]
"""Блоки, отобранные поиском: id → окна длинного блока или None, если блок нужен целиком."""


def select_hit(selected: Selection, hit: ChunkHit) -> None:
    for block_id in hit.block_ids:
        if hit.start is None or hit.end is None:
            selected[block_id] = None
        elif block_id not in selected:
            selected[block_id] = [(hit.start, hit.end)]
        elif selected[block_id] is not None:
            selected[block_id].append((hit.start, hit.end))


def excerpt(text: str, ranges: list[tuple[int, int]] | None) -> str:
    """Окна блока подряд, пропуски между ними — «…». Без окон — весь текст."""
    if not ranges:
        return text
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    body = " … ".join(text[s:e].strip() for s, e in merged)
    return ("… " if merged[0][0] > 0 else "") + body + (" …" if merged[-1][1] < len(text.rstrip()) else "")


def section_of(block: BlockLike) -> str | None:
    """Номер пункта для ссылки на место: раздел блока («3.2 а)»), иначе его собственный номер."""
    return getattr(block, "section_number", None) or getattr(block, "number", None)


_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+|\n+")
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
    короткие блоки (заголовок, подпункт) склеиваются с соседними. Блок длиннее
    `LONG_BLOCK_TOKENS` режется на окна по предложениям (`Chunk.start`, `Chunk.end`).
    """
    settings = get_settings()
    max_tokens = max_tokens or settings.chunk_tokens
    overlap = settings.chunk_overlap if overlap is None else overlap
    chunks: list[Chunk] = []
    current: list[BlockLike] = []
    tokens = 0
    for block in blocks:
        t = estimate_tokens(block.text)
        if t > LONG_BLOCK_TOKENS:
            if current:
                chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
                current, tokens = [], 0
            for start, end in sentence_windows(block.text, max_tokens):
                chunks.append(_chunk([block], f"{prefix}{len(chunks)}", start, end))
            continue
        if current and tokens + t > max_tokens:
            chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
            current = current[-overlap:] if overlap else []
            tokens = sum(estimate_tokens(b.text) for b in current)
        current.append(block)
        tokens += t
    if current:
        chunks.append(_chunk(current, f"{prefix}{len(chunks)}"))
    return chunks


def sentence_windows(text: str, max_tokens: int) -> list[tuple[int, int]]:
    """Окна до `max_tokens` по границам предложений; слишком длинное предложение режется по пробелу."""
    bounds = [0, *(m.end() for m in _SENTENCE_END.finditer(text)), len(text)]
    max_chars = int(max_tokens * CHARS_PER_TOKEN)
    pieces: list[tuple[int, int]] = []
    for start, end in zip(bounds, bounds[1:], strict=False):
        while end - start > 2 * max_chars:
            cut = text.rfind(" ", start + max_chars // 2, start + max_chars)
            cut = cut if cut > start else start + max_chars
            pieces.append((start, cut))
            start = cut
        if text[start:end].strip():
            pieces.append((start, end))
    windows: list[tuple[int, int]] = []
    for start, end in pieces:
        if windows and estimate_tokens(text[windows[-1][0] : end]) <= max_tokens:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    out = []
    for start, end in windows:
        part = text[start:end]
        out.append((start + len(part) - len(part.lstrip()), end - len(part) + len(part.rstrip())))
    return out


def _chunk(blocks: list[BlockLike], chunk_id: str, start: int | None = None, end: int | None = None) -> Chunk:
    if start is not None:
        block = blocks[0]
        text = f"{section_of(block)} {block.text[start:end]}" if section_of(block) else block.text[start:end]
    else:
        text = "\n".join(f"{section_of(b)} {b.text}" if section_of(b) else b.text for b in blocks)
    # Название раздела в начале фрагмента: короткий пункт без него теряет тему для поиска
    title = getattr(blocks[0], "section_title", None)
    if title and title != blocks[0].text:
        text = f"{title}\n{text}"
    return Chunk(id=chunk_id, block_ids=[b.id for b in blocks], text=text, start=start, end=end)
