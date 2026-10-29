from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import pytest

from bench.fonts import font_path
from bench.ocr_cer import score_pdf
from bench.samples import CONTRACT, expected_numbers, make_contract_pdf
from bench.scangen import ScanStyle, make_scan
from ml.extract.pdf_extract import extract_pdf
from ml.extract.pdf_text import page_text_words
from ml.ocr.pages import ocr_pages
from ml.ocr.preprocess import Prepared, deskew, estimate_skew, render, whiten_edges

needs_font = pytest.mark.skipif(font_path("serif") is None, reason="нет шрифта с кириллицей")


@pytest.fixture(scope="module")
def contract_pdf(tmp_path_factory):
    return make_contract_pdf(tmp_path_factory.mktemp("ocr") / "contract.pdf")


@needs_font
def test_deskew_estimates_and_straightens(contract_pdf):
    with pymupdf.open(contract_pdf) as doc:
        img = render(doc[0], 150)
    h, w = img.shape
    for angle in (-1.5, 0.7):
        rotated = cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1), (w, h), borderValue=255)
        assert abs(estimate_skew(rotated) + angle) <= 0.15
        prep = deskew(Prepared(image=rotated, dpi=150))
        assert abs(estimate_skew(prep.image)) <= 0.15


def test_whiten_edges():
    img = np.full((200, 100), 255, np.uint8)
    img[:, :8] = 10
    img[190:, :] = 20
    out = whiten_edges(img)
    assert out[:, :8].min() == 255 and out[190:].min() == 255


@needs_font
@pytest.mark.tesseract
def test_scan_numbers_and_accuracy(tmp_path, contract_pdf):
    scores = score_pdf(contract_pdf, seed=3, workdir=tmp_path)
    chars = sum(s.chars for s in scores)
    accuracy = 1 - sum(s.distance for s in scores) / chars
    assert accuracy >= 0.95, scores
    scan = tmp_path / "contract.scan3.pdf"
    result = extract_pdf(scan)
    assert result.kind == "scan" and result.ocr_confidence and result.ocr_confidence > 0.85
    numbers = [i.number for i in result.paragraphs if i.number]
    assert numbers == [n for n in expected_numbers(CONTRACT) if n] + ["10."]


@needs_font
@pytest.mark.tesseract
def test_word_boxes_in_page_coordinates(tmp_path, contract_pdf):
    scan = make_scan(contract_pdf, tmp_path / "flat.pdf", style=ScanStyle(max_skew=0, noise=2), pages=[1])
    (page,) = ocr_pages(scan, [1], use_cache=False)
    with pymupdf.open(contract_pdf) as doc:
        source, _ = page_text_words(doc[0])
    hits = 0
    for w in page.words:
        cx, cy = (w.bbox.x0 + w.bbox.x1) / 2, (w.bbox.y0 + w.bbox.y1) / 2
        if any(s.x0 - 2 <= cx <= s.x1 + 2 and s.y0 - 2 <= cy <= s.y1 + 2 for s in source):
            hits += 1
    assert hits / len(page.words) > 0.95


@needs_font
@pytest.mark.tesseract
def test_rotated_scan_is_turned_back(tmp_path, contract_pdf):
    scan = make_scan(contract_pdf, tmp_path / "rot.pdf", style=ScanStyle(rotate90=90), pages=[1])
    (page,) = ocr_pages(scan, [1], use_cache=False)
    assert page.rotation in (90, 270)
    text = " ".join(w.text for w in page.words)
    assert "Поставщик обязуется поставить" in text


@needs_font
@pytest.mark.tesseract
def test_ocr_cache_and_progress(tmp_path, contract_pdf):
    scan = make_scan(contract_pdf, tmp_path / "s.pdf", pages=[1])
    calls = []
    first = ocr_pages(scan, [1], progress=lambda done, total: calls.append((done, total)), images_dir=tmp_path / "img")
    assert calls[-1] == (1, 1)
    assert (tmp_path / "img" / "page-0001.jpg").exists()
    again = ocr_pages(scan, [1])
    assert again[0].words == first[0].words


def test_ocr_pages_streams_in_order(tmp_path, monkeypatch):
    """Страницы возвращаются в порядке запроса, прогресс растёт по одной, а рендеров в работе
    не больше 2×pool: скан на сотни страниц не держит все изображения в памяти."""
    import threading
    import time

    import ml.ocr.pages as pages_mod
    from ml.ocr.tesseract import TsvWord

    doc = pymupdf.open()
    for _ in range(7):
        doc.new_page(width=200, height=200)
    path = tmp_path / "blank.pdf"
    doc.save(path)

    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0}
    real_render = pages_mod.render

    def fake_render(page, dpi):
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        return real_render(page, 30)

    def fake_recognize(img, **_):
        with lock:
            state["in_flight"] -= 1
        time.sleep(0.02)  # без ограничения рендер за это время ушёл бы на все страницы вперёд
        return [TsvWord(1, 1, 1, 10, 10, 40, 12, 0.9, "слово")]

    monkeypatch.setattr(pages_mod, "render", fake_render)
    monkeypatch.setattr(pages_mod, "prepare_image", lambda img, dpi: Prepared(image=img, dpi=dpi))
    monkeypatch.setattr(pages_mod, "recognize", fake_recognize)
    monkeypatch.setattr(pages_mod, "refine_detached_prefixes", lambda *a, **k: None)
    calls = []
    order = [5, 1, 7, 2, 6, 3, 4]
    result = ocr_pages(path, order, workers=1, use_cache=False, progress=lambda d, t: calls.append((d, t)))
    assert [p.page for p in result] == order
    assert calls == [(i, 7) for i in range(8)]
    assert state["peak"] <= 2


def test_orientation_falls_back_to_full_size(monkeypatch):
    import ml.ocr.preprocess as pp
    from ml.ocr.tesseract import Orientation

    seen = []

    def fake_detect(img, *, dpi=300, timeout=30):
        seen.append((img.shape, dpi))
        return Orientation(90, 1.0) if dpi < 300 else Orientation(180, 12.0)

    monkeypatch.setattr(pp, "detect_orientation", fake_detect)
    img = np.full((400, 300), 255, np.uint8)
    found = pp.page_orientation(img, 300)
    assert seen == [((200, 150), 150), ((400, 300), 300)]
    assert found == Orientation(180, 12.0)


def test_missing_language_model_fails_instead_of_latin(monkeypatch, tmp_path):
    from ml.errors import OcrError
    from ml.ocr import tesseract

    monkeypatch.setattr(tesseract, "_available_langs", lambda _tessdata: frozenset({"eng", "osd"}))
    with pytest.raises(OcrError, match="rus"):
        tesseract.require_langs("rus+eng")
    tesseract.require_langs("eng")
