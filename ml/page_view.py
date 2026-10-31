"""Страница исходного PDF и рамки цитаты на ней: сверка находки с оригиналом (FR-5.6*).

OCR может превратить «30 дней» в «80 дней» с высокой уверенностью, поэтому юрист должен видеть сам скан.
Страница рендерится по запросу. Рамки отдаются долями размера страницы — интерфейс не считает координаты.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pymupdf

PAGE_DPI = 130
JPEG_QUALITY = 80

# PyMuPDF не поддерживает работу в нескольких потоках, а API вызывает его из пула потоков
_render_lock = threading.Lock()


@dataclass(frozen=True)
class PageMeta:
    """Размер страницы после поворота и матрица из системы текстового слоя в систему показа."""

    width: float
    height: float
    matrix: tuple[float, float, float, float, float, float]


def render_page(path: str | Path, page_no: int) -> tuple[bytes, PageMeta] | None:
    """JPEG страницы (с 1) с учётом `/Rotate`; `None`, если такой страницы нет."""
    with _render_lock, pymupdf.open(str(path)) as doc:
        if not 1 <= page_no <= doc.page_count:
            return None
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=PAGE_DPI, colorspace=pymupdf.csGRAY, annots=False)
        m = page.rotation_matrix
        meta = PageMeta(width=page.rect.width, height=page.rect.height, matrix=(m.a, m.b, m.c, m.d, m.e, m.f))
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY), meta


def line_rects(words: list[dict]) -> list[tuple[float, float, float, float]]:
    """Слова подряд на одной строке → один прямоугольник: выделение выглядит как маркер, а не рябь."""
    rects: list[list[float]] = []
    for w in words:
        b = w["bbox"]
        x0, y0, x1, y1 = b["x0"], b["y0"], b["x1"], b["y1"]
        if rects:
            r = rects[-1]
            overlap = min(r[3], y1) - max(r[1], y0)
            if overlap > 0.5 * min(r[3] - r[1], y1 - y0) and x0 >= r[0] - 1:
                r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
                continue
        rects.append([x0, y0, x1, y1])
    return [tuple(r) for r in rects]


def quote_words(source_map: dict | None, start: int, end: int) -> dict[int, list[dict]]:
    """Слова цитаты из карты исходных позиций блока по страницам."""
    words = sorted((w for w in (source_map or {}).get("words") or [] if w["start"] < end and w["end"] > start),
                   key=lambda w: w["start"])
    by_page: dict[int, list[dict]] = {}
    for w in words:
        by_page.setdefault(w["page"], []).append(w)
    return by_page


def page_fractions(words: list[dict], meta: PageMeta) -> list[dict]:
    """Рамки слов одной страницы в долях её размера при показе.

    Рамки OCR уже в системе повёрнутой страницы, рамки текстового слоя — без поворота (как в разметке PDF).
    """
    ocr = bool(words) and words[0].get("confidence") is not None
    matrix = pymupdf.Matrix(*meta.matrix)
    out = []
    for rect in line_rects(words):
        r = pymupdf.Rect(rect)
        if not ocr:
            r = r * matrix
        out.append({"x0": round(r.x0 / meta.width, 5), "y0": round(r.y0 / meta.height, 5),
                    "x1": round(r.x1 / meta.width, 5), "y1": round(r.y1 / meta.height, 5)})
    return out
