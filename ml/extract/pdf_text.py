"""Текстовый слой PDF (FR-3.2): символы → слова и отрезки строк по геометрии.

Строки собираются по положению символов, а не по их порядку в файле: LibreOffice и
учётные системы пишут часть знаков отдельно от строки. Ориентиры подобраны на
договорах: символы в одной строке, если центры по высоте расходятся меньше чем на
0,45 кегля; пробел — от зазора 0,18 кегля; граница колонки — от 2,5 кегля.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import pymupdf

from ml.extract.layout import COLUMN_GAP, LWord

log = logging.getLogger(__name__)

SAME_LINE = 0.45
WORD_GAP = 0.18
_TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
_VOWELS = set("аеёиоуыэюяaeiouy")


@dataclass
class Char:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    baseline: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class LayerQuality:
    chars: int
    broken: bool
    reason: str | None = None


def page_chars(page: pymupdf.Page) -> list[Char]:
    chars: list[Char] = []
    skipped = 0
    data = page.get_text("rawdict", flags=_TEXT_FLAGS)
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            dx, dy = line["dir"]
            if abs(dy) > 0.1 or dx <= 0:
                skipped += sum(len(s["chars"]) for s in line["spans"])
                continue
            for span in line["spans"]:
                font = span.get("font", "").lower()
                bold = bool(span["flags"] & 16) or any(k in font for k in ("bold", "black", "heavy", "semibold"))
                size = float(span["size"]) or 1.0
                for ch in span["chars"]:
                    c = ch["c"]
                    if not c or c.isspace() or ch.get("synthetic"):
                        continue
                    if c == "\u00ad":
                        # Глиф дефиса часто извлекается как мягкий перенос; на странице он виден
                        c = "-"
                    x0, y0, x1, y1 = ch["bbox"]
                    chars.append(Char(c, x0, y0, x1, y1, size, bold, ch["origin"][1]))
    if skipped:
        log.debug("pdf: стр. %d: пропущено %d символов не горизонтального текста", page.number + 1, skipped)
    return chars


def layer_quality(chars: list[Char], words: list[LWord]) -> LayerQuality:
    """Битый текстовый слой не используется, страница уходит в OCR, если: больше 5% символов
    U+FFFD, из частной области Unicode или управляющих; в среднем больше 20 букв на слово;
    доля гласных вне 20–70%."""
    n = len(chars)
    if n == 0:
        return LayerQuality(0, False)
    bad = sum(1 for ch in chars if ch.c == "�" or 0xE000 <= ord(ch.c[0]) <= 0xF8FF or ord(ch.c[0]) < 32)
    if bad / n > 0.05:
        return LayerQuality(n, True, f"символов-заглушек {bad / n:.0%} > 5%")
    letters = [ch.c.lower() for ch in chars if ch.c.isalpha()]
    if words:
        per_word = len(letters) / len(words)
        if per_word > 20:
            return LayerQuality(n, True, f"{per_word:.0f} букв на слово > 20")
    if len(letters) >= 50:
        vowels = sum(1 for c in letters if c in _VOWELS) / len(letters)
        if not 0.2 <= vowels <= 0.7:
            return LayerQuality(n, True, f"доля гласных {vowels:.0%} вне 20–70%")
    return LayerQuality(n, False)


def chars_to_words(chars: list[Char], page_no: int) -> list[LWord]:
    """Символы → слова; `line_key` слова — отрезок строки между границами колонок."""
    words: list[LWord] = []
    for line_idx, line in enumerate(_lines(chars)):
        line.sort(key=lambda ch: ch.x0)
        line = _dedupe(line)
        base = max(ch.size for ch in line)
        base_chars = [ch for ch in line if ch.size >= 0.8 * base]
        baseline = statistics.median(ch.baseline for ch in base_chars)
        segment = 0
        current: list[Char] = [line[0]]
        for prev, ch in zip(line, line[1:], strict=False):
            gap = ch.x0 - prev.x1
            size = max(prev.size, ch.size, 0.8 * base)
            if gap >= COLUMN_GAP * size:
                words.append(_word(current, base, baseline, page_no, (page_no, line_idx, segment)))
                segment += 1
                current = [ch]
            elif gap >= WORD_GAP * size:
                words.append(_word(current, base, baseline, page_no, (page_no, line_idx, segment)))
                current = [ch]
            else:
                current.append(ch)
        words.append(_word(current, base, baseline, page_no, (page_no, line_idx, segment)))
    return words


def _lines(chars: list[Char]) -> list[list[Char]]:
    groups: list[list[Char]] = []
    centers: list[float] = []
    sizes: list[float] = []
    for ch in sorted(chars, key=lambda ch: (ch.cy, ch.x0)):
        target = None
        for i in range(len(groups) - 1, max(-1, len(groups) - 8), -1):
            if abs(centers[i] - ch.cy) < SAME_LINE * max(ch.size, sizes[i]):
                target = i
                break
        if target is None:
            groups.append([ch])
            centers.append(ch.cy)
            sizes.append(ch.size)
        else:
            group = groups[target]
            group.append(ch)
            if ch.size >= sizes[target]:
                # Центр строки — по символам основного кегля, надстрочные знаки его не сдвигают
                sizes[target] = ch.size
                centers[target] = statistics.median(c.cy for c in group if c.size >= 0.8 * ch.size)
    return groups


def _dedupe(line: list[Char]) -> list[Char]:
    """Убирает двойную отрисовку символа (ложный жирный): тот же знак со сдвигом меньше 0,1 кегля."""
    out = [line[0]]
    for ch in line[1:]:
        prev = out[-1]
        if ch.c == prev.c and abs(ch.x0 - prev.x0) < 0.1 * ch.size and abs(ch.cy - prev.cy) < 0.1 * ch.size:
            continue
        out.append(ch)
    return out


def _word(chars: list[Char], base: float, baseline: float, page_no: int, key: tuple) -> LWord:
    sup = 0
    for ch in reversed(chars):
        if ch.c.isdigit() and ch.size < 0.8 * base and ch.baseline < baseline - 0.2 * base:
            sup += 1
        else:
            break
    return LWord(
        text="".join(ch.c for ch in chars),
        x0=min(ch.x0 for ch in chars),
        y0=min(ch.y0 for ch in chars),
        x1=max(ch.x1 for ch in chars),
        y1=max(ch.y1 for ch in chars),
        size=base,
        page=page_no,
        bold=all(ch.bold for ch in chars),
        line_key=key,
        sup_suffix=sup,
    )


def page_text_words(page: pymupdf.Page) -> tuple[list[LWord], LayerQuality]:
    chars = page_chars(page)
    words = chars_to_words(chars, page.number + 1) if chars else []
    quality = layer_quality(chars, words)
    if quality.broken:
        log.info("pdf: стр. %d: битый текстовый слой (%s), страница уйдёт в OCR", page.number + 1, quality.reason)
    return words, quality
