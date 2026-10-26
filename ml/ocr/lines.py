"""Строки скана по геометрии и повторное чтение номеров пунктов с выступом.

В режиме `--psm 3` Tesseract относит номер пункта с выступом («1.2.» левее текста) к
отдельному блоку и читает его без контекста строки: «1.2.» превращается в «Га» или
«V1,». Такие номера перечитываются вместе с началом своей строки: все кропы страницы
складываются в одно изображение столбиком и читаются одним вызовом `--psm 6`.
"""

from __future__ import annotations

import logging
import statistics

import numpy as np
from rapidfuzz import fuzz

from ml.ocr.tesseract import TsvWord, recognize

log = logging.getLogger(__name__)

PREFIX_MAX_CHARS = 8
CONTEXT_WORDS = 3
GAP = 24
PAD = 6


def pixel_lines(words: list[TsvWord]) -> list[list[TsvWord]]:
    """Слова → строки по вертикальному центру (изображение уже выпрямлено)."""
    if not words:
        return []
    height = statistics.median(w.height for w in words) or 1
    lines: list[list[TsvWord]] = []
    centers: list[float] = []
    for w in sorted(words, key=lambda w: (w.top + w.height / 2, w.left)):
        cy = w.top + w.height / 2
        for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
            if abs(centers[i] - cy) < 0.5 * height:
                lines[i].append(w)
                centers[i] = statistics.median(x.top + x.height / 2 for x in lines[i])
                break
        else:
            lines.append([w])
            centers.append(cy)
    for line in lines:
        line.sort(key=lambda w: w.left)
    return lines


def _detached_prefix(line: list[TsvWord]) -> int:
    """Сколько первых слов строки — оторванный номер пункта (0 — нет такого).

    Номер — ведущие слова из одного блока Tesseract, за которыми идёт текст из другого
    блока; вместе не длиннее 8 знаков («2.1.» иногда распадается на «vse» и «©»)."""
    if len(line) < 2:
        return 0
    block = line[0].block
    k = 1
    while k < len(line) and line[k].block == block and k < 3:
        k += 1
    if k >= len(line) or line[k].block == block:
        return 0
    if sum(len(w.text) for w in line[:k]) > PREFIX_MAX_CHARS:
        return 0
    first, text = line[k - 1], line[k]
    gap = text.left - (first.left + first.width)
    return k if 0 <= gap < 2.5 * max(first.height, text.height) * 1.5 else 0


def refine_detached_prefixes(img: np.ndarray, words: list[TsvWord], *, lang: str, dpi: int,
                             timeout: float = 60) -> int:
    """Перечитывает оторванные номера пунктов в контексте строки. Возвращает число исправлений."""
    candidates = [(line, k) for line in pixel_lines(words) if (k := _detached_prefix(line))]
    if not candidates:
        return 0
    crops = []
    for line, k in candidates:
        ctx = line[: k + CONTEXT_WORDS]
        x0 = max(0, min(w.left for w in ctx) - PAD)
        x1 = min(img.shape[1], max(w.left + w.width for w in ctx) + PAD)
        y0 = max(0, min(w.top for w in ctx) - PAD)
        y1 = min(img.shape[0], max(w.top + w.height for w in ctx) + PAD)
        crops.append(img[y0:y1, x0:x1])
    width = max(c.shape[1] for c in crops) + 2 * GAP
    height = sum(c.shape[0] for c in crops) + GAP * (len(crops) + 1)
    sheet = np.full((height, width), 255, dtype=np.uint8)
    offsets = []
    y = GAP
    for crop in crops:
        sheet[y : y + crop.shape[0], GAP : GAP + crop.shape[1]] = crop
        offsets.append((y, y + crop.shape[0]))
        y += crop.shape[0] + GAP
    result = recognize(sheet, lang=lang, dpi=dpi, psm=6, timeout=timeout)
    fixed = 0
    for (line, k), (top, bottom) in zip(candidates, offsets, strict=True):
        found = sorted(
            (w for w in result if top - GAP / 2 <= w.top + w.height / 2 <= bottom + GAP / 2),
            key=lambda w: w.left,
        )
        if len(found) < 2:
            continue
        # Второе слово перечитанной строки должно совпасть с первым словом текста
        if fuzz.ratio(found[1].text, line[k].text) < 70:
            continue
        first = line[0]
        before = " ".join(w.text for w in line[:k])
        if before == found[0].text and found[0].conf <= first.conf:
            continue
        log.debug("ocr: номер пункта перечитан: %r (%.2f) → %r (%.2f)", before, first.conf,
                  found[0].text, found[0].conf)
        last = line[k - 1]
        first.width = last.left + last.width - first.left
        first.text = found[0].text
        first.conf = found[0].conf
        # Номер теперь в одном блоке со строкой: сборка строк его не оторвёт
        first.block, first.par, first.line = line[k].block, line[k].par, line[k].line
        for extra in line[1:k]:
            words.remove(extra)
        fixed += 1
    return fixed
