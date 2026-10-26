"""Классификация страниц PDF (FR-3.4): какие страницы нужно распознавать.

Признаки: символы текстового слоя (от 40 — слой считается текстом), доля площади под
картинками (от 80% — скан), по растру 100 dpi — пятна размером с букву (высота
1,2–8 мм, ширина до 10 мм), не покрытые словами текстового слоя.

| Страница | Условие                                                          | OCR |
| scan     | картинка на всю страницу, слоя нет                               | да  |
| text     | слой есть, непокрытых пятен мало                                 | нет |
| mixed    | слой есть, непокрытых пятен от 60 и не меньше 25% всех           | да  |
| curves   | слоя нет, буквоподобных пятен много: текст нарисован линиями     | да  |
| empty    | изображения почти нет                                            | нет |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf

from contracts.extraction import PageInfo
from ml.extract.layout import LWord
from ml.extract.pdf_text import LayerQuality

log = logging.getLogger(__name__)

TEXT_MIN_CHARS = 40
SCAN_IMAGE_SHARE = 0.80
RASTER_DPI = 100
MIXED_MIN_UNCOVERED = 60
MIXED_MIN_SHARE = 0.25
CURVES_MIN_BLOBS = 60
OCR_MIN_BLOBS = 15
EMPTY_MAX_INK = 0.002
_MM = RASTER_DPI / 25.4


@dataclass
class RasterStats:
    blobs: int
    uncovered: int
    ink: float


def image_share(page: pymupdf.Page) -> float:
    rect = page.rect
    area = rect.width * rect.height or 1
    covered = 0.0
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & rect
        if not box.is_empty:
            covered += box.width * box.height
    return min(1.0, covered / area)


def raster_stats(page: pymupdf.Page, words: list[LWord]) -> RasterStats:
    pix = page.get_pixmap(dpi=RASTER_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    ink_mask = (img < 160).astype(np.uint8)
    ink = float(ink_mask.mean())
    n, _, stats, centroids = cv2.connectedComponentsWithStats(ink_mask, connectivity=8)
    if n <= 1:
        return RasterStats(0, 0, ink)
    widths = stats[1:, cv2.CC_STAT_WIDTH]
    heights = stats[1:, cv2.CC_STAT_HEIGHT]
    letter = (heights >= 1.2 * _MM) & (heights <= 8 * _MM) & (widths <= 10 * _MM) & (stats[1:, cv2.CC_STAT_AREA] >= 4)
    blobs = int(letter.sum())
    if not blobs:
        return RasterStats(0, 0, ink)
    covered = np.zeros_like(ink_mask)
    scale = RASTER_DPI / 72
    for w in words:
        x0, y0 = max(0, int(w.x0 * scale) - 2), max(0, int(w.y0 * scale) - 2)
        x1, y1 = int(w.x1 * scale) + 3, int(w.y1 * scale) + 3
        covered[y0:y1, x0:x1] = 1
    cx = np.clip(centroids[1:, 0].astype(int), 0, pix.width - 1)
    cy = np.clip(centroids[1:, 1].astype(int), 0, pix.height - 1)
    uncovered = int((letter & (covered[cy, cx] == 0)).sum())
    return RasterStats(blobs, uncovered, ink)


def classify_page(page: pymupdf.Page, words: list[LWord], quality: LayerQuality) -> PageInfo:
    rect = page.rect
    info = PageInfo(page=page.number + 1, width=rect.width, height=rect.height, rotation=page.rotation)
    has_layer = quality.chars >= TEXT_MIN_CHARS and not quality.broken
    info.text_layer_broken = quality.broken
    has_images = bool(page.get_images(full=False))
    if has_layer and not has_images:
        info.kind = "text"
        return info
    share = image_share(page) if has_images else 0.0
    stats = raster_stats(page, words if has_layer else [])
    if has_layer:
        if stats.uncovered >= MIXED_MIN_UNCOVERED and stats.uncovered >= MIXED_MIN_SHARE * stats.blobs:
            info.kind, info.needs_ocr = "mixed", True
        else:
            info.kind = "text"
    elif stats.blobs < OCR_MIN_BLOBS or stats.ink < EMPTY_MAX_INK:
        info.kind = "empty"
    elif share >= SCAN_IMAGE_SHARE or (has_images and stats.blobs < CURVES_MIN_BLOBS):
        info.kind, info.needs_ocr = "scan", True
    elif share > 0.3:
        info.kind, info.needs_ocr = "scan", True
    else:
        info.kind, info.needs_ocr = "curves", True
    log.debug(
        "pdf: стр. %d: %s (символов слоя %d, картинки %.0f%%, пятен %d, непокрыто %d)",
        info.page, info.kind, quality.chars, share * 100, stats.blobs, stats.uncovered,
    )
    return info


def classify_pages(path) -> list[PageInfo]:
    """Классы всех страниц PDF. Открывает файл сам: вызывать в воркере с пулом процессов."""
    from ml.extract.pdf_extract import plan_pdf

    return plan_pdf(path)
