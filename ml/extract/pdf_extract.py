"""PDF целиком: план страниц, сборка абзацев из текстового слоя и слов OCR.

Порядок для задач Celery:

1. `plan_pdf(path)` — классы страниц и список страниц для OCR (очередь `ingest`).
2. `ocr_pages(path, pages)` по группам страниц (очередь `ocr`), возвращает только слова.
3. `build_pdf_result(path, plan, ocr)` — абзацы собираются один раз после всех групп,
   иначе абзац на границе групп разорвётся.

`extract_pdf` делает всё подряд в одном процессе: для тестов, стендов и `test_rule`.
PyMuPDF не поддерживает работу в нескольких потоках: всё, что открывает PDF, работает
в воркерах с пулом процессов.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable
from pathlib import Path

import pymupdf

from contracts.extraction import ExtractionResult, OcrPage, PageInfo, RawParagraph
from ml.config import PIPELINE_VERSION, get_settings
from ml.errors import CorruptedFileError, EncryptedFileError, TooManyPagesError
from ml.extract.layout import (
    LWord,
    build_paragraphs,
    group_lines,
    mark_headings,
    merge_across_pages,
    para_to_raw,
    reading_order,
    remove_headers_footers,
    strip_footnote_marks,
)
from ml.extract.page_classes import classify_page
from ml.extract.pdf_text import page_text_words

log = logging.getLogger(__name__)


def open_pdf(path: str | Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF бросает разные типы на битых файлах
        raise CorruptedFileError(detail=f"pdf: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise EncryptedFileError()
    if doc.page_count == 0:
        doc.close()
        raise CorruptedFileError("В файле PDF нет страниц")
    max_pages = get_settings().max_pages
    if doc.page_count > max_pages:
        count = doc.page_count
        doc.close()
        raise TooManyPagesError(f"В документе {count} страниц — проверяются документы до {max_pages} страниц")
    return doc


def plan_pdf(path: str | Path) -> list[PageInfo]:
    """Классы страниц: какие страницы читать из текстового слоя, какие распознавать (FR-3.4)."""
    with open_pdf(path) as doc:
        plan = []
        for page in doc:
            words, quality = page_text_words(page)
            plan.append(classify_page(page, words, quality))
    ocr = [p.page for p in plan if p.needs_ocr]
    log.info("pdf: %s: %d стр., в OCR %d", Path(path).name, len(plan), len(ocr))
    return plan


def ocr_words(page: OcrPage, *, page_frame: bool = False) -> list[LWord]:
    """Слова OCR в общем виде для сборки абзацев. Кегль оценивается по высоте строки Tesseract.

    Строки собираются заново по геометрии, а не по разбивке Tesseract: номер пункта с
    выступом он часто относит к отдельному блоку, и пункт резался бы пополам. Геометрия —
    выпрямленного изображения (`layout`): на странице строки скана наклонены. На смешанной
    странице (`page_frame`) — система страницы, общая со словами текстового слоя.
    """
    by_line: dict[tuple, list] = {}
    for w in page.words:
        by_line.setdefault((w.block, w.par, w.line), []).append(w)

    def frame(w):
        return w.bbox if page_frame or w.layout is None else w.layout

    # Кегль — один на страницу: высота строки OCR зависит от скобок и прописных, и по ней
    # каждая строка выглядела бы набранной своим кеглем
    heights = [max(frame(w).y1 - frame(w).y0 for w in words) for words in by_line.values()]
    size = max(statistics.median(heights) * 0.85, 4.0) if heights else 10.0
    out: list[LWord] = []
    for words in by_line.values():
        for w in words:
            b = frame(w)
            out.append(
                LWord(
                    text=w.text,
                    x0=b.x0,
                    y0=b.y0,
                    x1=b.x1,
                    y1=b.y1,
                    size=size,
                    page=page.page,
                    conf=w.confidence,
                    page_bbox=(w.bbox.x0, w.bbox.y0, w.bbox.x1, w.bbox.y1),
                )
            )
    return out


def _drop_overlapping(ocr: list[LWord], layer: list[LWord]) -> list[LWord]:
    """На смешанной странице слова OCR поверх слов текстового слоя отбрасываются."""
    if not layer:
        return ocr
    kept = []
    for w in ocr:
        cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
        if any(t.x0 - 1 <= cx <= t.x1 + 1 and t.y0 - 1 <= cy <= t.y1 + 1 for t in layer):
            continue
        kept.append(w)
    return kept


def build_pdf_result(
    path: str | Path,
    plan: list[PageInfo],
    ocr: Iterable[OcrPage] = (),
) -> ExtractionResult:
    settings = get_settings()
    ocr_by_page = {p.page: p for p in ocr}
    info_by_page = {p.page: p for p in plan}
    lines_by_page = {}
    heights = {}
    with open_pdf(path) as doc:
        for page in doc:
            no = page.number + 1
            info = info_by_page.get(no) or PageInfo(page=no, width=page.rect.width, height=page.rect.height)
            heights[no] = page.rect.height
            words: list[LWord] = []
            if not info.text_layer_broken:
                layer_words, _ = page_text_words(page)
                words.extend(layer_words)
            if info.needs_ocr:
                ocr_page = ocr_by_page.get(no)
                if ocr_page is None:
                    log.warning("pdf: стр. %d: нет результата OCR", no)
                else:
                    words.extend(_drop_overlapping(ocr_words(ocr_page, page_frame=bool(words)), words))
                    info.ocr_confidence = ocr_page.confidence
                    info.low_quality = ocr_page.low_quality
            lines_by_page[no] = group_lines(words)

    removed = remove_headers_footers(lines_by_page, heights)
    if removed:
        log.info("pdf: %s: убрано строк колонтитулов: %d", Path(path).name, removed)

    paras = []
    for no in sorted(lines_by_page):
        lines = lines_by_page[no]
        strip_footnote_marks(lines, heights[no])
        paras.extend(build_paragraphs(reading_order([ln for ln in lines if ln.words])))
    paras = merge_across_pages(paras)

    items: list[RawParagraph] = []
    for para in paras:
        item = para_to_raw(para, len(items))
        if item is None:
            continue
        pages = {item.page, item.page_end or item.page}
        item.low_ocr_quality = any(info_by_page.get(p) and info_by_page[p].low_quality for p in pages)
        items.append(item)
    mark_headings(items)

    ocr_pages_done = [p for p in plan if p.needs_ocr]
    weights = [(ocr_by_page[p.page].confidence, _chars(ocr_by_page[p.page])) for p in ocr_pages_done
               if p.page in ocr_by_page and ocr_by_page[p.page].confidence is not None]
    total = sum(n for _, n in weights)
    ocr_confidence = sum(c * n for c, n in weights) / total if total else None
    low = sorted(p.page for p in plan if p.low_quality)
    kind = "scan" if len(ocr_pages_done) * 2 > len(plan) else "pdf"
    log.info(
        "pdf: %s: абзацев %d, OCR %d стр., уверенность %s, порог %.2f",
        Path(path).name, len(items), len(ocr_pages_done),
        f"{ocr_confidence:.3f}" if ocr_confidence is not None else "—", settings.ocr_low_quality_threshold,
    )
    return ExtractionResult(
        kind=kind,
        format="pdf",
        pages=len(plan),
        paragraphs=items,
        page_info=plan,
        ocr_confidence=ocr_confidence,
        low_quality_pages=low,
        pipeline_version=PIPELINE_VERSION,
    )


def _chars(page: OcrPage) -> int:
    return sum(len(w.text) for w in page.words) or 1


def extract_pdf(path: str | Path, *, run_ocr: bool = True) -> ExtractionResult:
    plan = plan_pdf(path)
    needed = [p.page for p in plan if p.needs_ocr]
    ocr: list[OcrPage] = []
    if needed and run_ocr:
        from ml.ocr.pages import ocr_pages

        ocr = ocr_pages(path, needed)
    return build_pdf_result(path, plan, ocr)
