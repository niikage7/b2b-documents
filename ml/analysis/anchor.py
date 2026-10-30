"""Привязка цитаты LLM к тексту блока (FR-4.3).

Цитата ищется в тексте блоков после нормализации, смещения пересчитываются обратно в
исходный текст блока. Нормализация: нижний регистр, «ё» → «е», латинские буквы,
похожие на кириллицу, → кириллица (частая подмена в OCR и в самих документах);
пробелы, кавычки, тире и дефисы не учитываются (дефис переноса из PDF сохраняется в
тексте, а модель его пишет не всегда). Сначала точное совпадение, затем
`rapidfuzz.fuzz.partial_ratio_alignment` с порогом `QUOTE_MIN_SCORE`. Нет совпадения —
находка отбрасывается и пишется в лог: в интерфейс не попадает ничего, что нельзя подсветить.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz

from ml.analysis.blocks import BlockLike
from ml.config import get_settings

_HOMOGLYPHS = str.maketrans(
    {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у", "k": "к", "m": "м", "t": "т",
     "h": "н", "b": "в", "ё": "е"}
)
# Здесь же невидимые символы из DOCX: нулевой ширины, знаки направления текста, BOM
_IGNORED = set("-‐‑‒–—−\"'«»„“”‘’`´­\u200b\u200c\u200d\u200e\u200f\u2060\ufeff")
_LEADING_NUMBER = re.compile(r"^\s*(?:п\.\s*)?(?:\d{1,3}(?:\.\d{1,3})*\.?|\d{1,3}\)|[а-яa-z]\))\s+")
_ELLIPSIS = re.compile(r"\.{3,}|…|\[\.\.\.\]|\(\.\.\.\)")
MIN_QUOTE_CHARS = 4


@dataclass
class Anchor:
    block: BlockLike
    start: int
    end: int
    score: float


@lru_cache(maxsize=4096)
def normalize_map(text: str) -> tuple[str, tuple[int, ...]]:
    """Нормализованная строка и для каждого её символа — индекс в исходном тексте."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace() or ch in _IGNORED:
            continue
        out.append(ch.lower().translate(_HOMOGLYPHS))
        idx.append(i)
    return "".join(out), tuple(idx)


def prepare_quote(quote: str) -> str:
    """Убирает обрамляющие кавычки и номер пункта; при многоточии берёт самую длинную часть."""
    q = quote.strip().strip("\"'«»„“”").strip()
    q = _LEADING_NUMBER.sub("", q)
    parts = [p.strip() for p in _ELLIPSIS.split(q) if p.strip()]
    return max(parts, key=len) if parts else ""


def _snap(text: str, start: int, end: int) -> tuple[int, int]:
    """Не режет слова: расширяет границы до целых слов и обрезает пробелы по краям."""
    while start > 0 and text[start - 1].isalnum() and text[start].isalnum():
        start -= 1
    while end < len(text) and text[end - 1].isalnum() and text[end].isalnum():
        end += 1
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def anchor_quote(
    quote: str,
    blocks: Sequence[BlockLike],
    *,
    min_score: float | None = None,
    fallback: Sequence[BlockLike] = (),
) -> tuple[Anchor | None, float]:
    """Ищет цитату в `blocks` (сначала) и точным совпадением — в `fallback`.

    Возвращает привязку или None и лучший найденный балл (для журнала отбрасываний).
    """
    min_score = get_settings().quote_min_score if min_score is None else min_score
    nq, _ = normalize_map(prepare_quote(quote))
    if len(nq) < MIN_QUOTE_CHARS:
        return None, 0.0

    for block in [*blocks, *fallback]:
        nb, idx = normalize_map(block.text)
        pos = nb.find(nq)
        if pos >= 0:
            start, end = _snap(block.text, idx[pos], idx[pos + len(nq) - 1] + 1)
            return Anchor(block, start, end, 100.0), 100.0

    best: tuple[float, BlockLike, int, int] | None = None
    best_score = 0.0
    for block in blocks:
        nb, idx = normalize_map(block.text)
        if not nb:
            continue
        res = fuzz.partial_ratio_alignment(nq, nb)
        if res is None:
            continue
        best_score = max(best_score, res.score)
        if res.score < min_score:
            continue
        span = res.dest_end - res.dest_start
        # Цитата длиннее блока (модель взяла два пункта сразу): блок должен покрывать заметную часть цитаты
        if span < 0.4 * min(len(nq), len(nb)) or (len(nq) > len(nb) and len(nb) < 0.4 * len(nq)):
            continue
        if best is None or res.score > best[0]:
            best = (res.score, block, res.dest_start, res.dest_end)
    if best is None:
        return None, best_score
    score, block, ds, de = best
    _, idx = normalize_map(block.text)
    start, end = _snap(block.text, idx[ds], idx[de - 1] + 1)
    if end <= start:
        return None, best_score
    return Anchor(block, start, end, round(score, 1)), score
