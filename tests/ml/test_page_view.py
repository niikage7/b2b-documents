"""Сверка с оригиналом (FR-5.6*): рамка цитаты ложится на чернила показанной страницы при любом /Rotate."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import pytest

from bench.fonts import font_path
from ml.extract.pdf_extract import extract_pdf
from ml.page_view import page_fractions, quote_words, render_page

pytestmark = pytest.mark.skipif(font_path("serif") is None, reason="нет шрифта с кириллицей")

TEXT = "Поставка в течение трёх дней с даты заключения Контракта"


def _pdf(path, rotation: int) -> None:
    font = pymupdf.Font(fontfile=str(font_path("serif")))
    doc = pymupdf.open()
    page = doc.new_page()
    tw = pymupdf.TextWriter(page.rect)
    tw.append((72, 100), TEXT, font=font, fontsize=14)
    tw.write_text(page)
    page.set_rotation(rotation)
    doc.save(path)


def _ink(image: bytes, rect: dict) -> float:
    """Доля тёмных точек внутри рамки на картинке страницы."""
    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    h, w = img.shape
    crop = img[int(rect["y0"] * h) : int(rect["y1"] * h) + 1, int(rect["x0"] * w) : int(rect["x1"] * w) + 1]
    return float((crop < 128).mean())


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_layer_rect_lands_on_ink(tmp_path, rotation):
    src = tmp_path / "doc.pdf"
    _pdf(src, rotation)
    (item,) = extract_pdf(src, run_ocr=False).paragraphs
    start = item.text.index("трёх")
    words = quote_words(item.source_map.model_dump(), start, start + len("трёх"))
    image, meta = render_page(src, 1)
    (rect,) = page_fractions(words[1], meta)
    assert 0 <= rect["x0"] < rect["x1"] <= 1 and 0 <= rect["y0"] < rect["y1"] <= 1
    assert _ink(image, rect) > 0.05


@pytest.mark.tesseract
@pytest.mark.parametrize("rotation", [90, 270])
def test_ocr_rect_lands_on_ink(tmp_path, rotation):
    from ml.ocr.pages import ocr_pages

    src = tmp_path / "doc.pdf"
    _pdf(src, rotation)
    (ocr,) = ocr_pages(src, [1], use_cache=False)
    word = next(w for w in ocr.words if w.text.startswith("Контракт"))
    words = [{"start": 0, "end": 1, "page": 1, "bbox": word.bbox.model_dump(), "confidence": word.confidence}]
    image, meta = render_page(src, 1)
    (rect,) = page_fractions(words, meta)
    assert _ink(image, rect) > 0.05


def test_missing_page(tmp_path):
    src = tmp_path / "doc.pdf"
    _pdf(src, 0)
    assert render_page(src, 2) is None


def test_block_without_word_boxes():
    assert quote_words(None, 0, 5) == {}
    assert quote_words({"format": "docx", "runs": []}, 0, 5) == {}
