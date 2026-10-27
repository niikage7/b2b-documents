"""`ocr_pages`: OCR группы страниц PDF (FR-3.3, FR-3.5).

Функция работает над группой страниц, чтобы Влад мог параллелить группы в очереди
`ocr`, и возвращает только слова: абзацы собираются один раз после всех групп
(`build_pdf_result`). Результат каждой страницы кешируется: ключ — хеш файла, номер
страницы, параметры, версия Tesseract и версия конвейера.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import statistics
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from contracts.extraction import BBox, OcrPage, OcrWord
from ml.config import PIPELINE_VERSION, get_settings
from ml.convert import file_sha256
from ml.extract.pdf_extract import open_pdf
from ml.ocr.lines import pixel_lines, refine_detached_prefixes
from ml.ocr.preprocess import Prepared, prepare_image, render
from ml.ocr.tesseract import TsvWord, recognize, tesseract_version

log = logging.getLogger(__name__)

WORD_MIN_CONF = 0.30
LINE_MIN_CONF = 0.55
_LINE_ONLY = set("|_—=-–~¦")

ProgressFn = Callable[[int, int], None]
"""Обратный вызов прогресса: (готово страниц, всего страниц) — для `report_progress` Влада."""


def _cache_key(digest: str, page: int, dpi: int, lang: str) -> str:
    raw = f"{digest}:{page}:{dpi}:{lang}:{tesseract_version()}:{PIPELINE_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_dash(w: TsvWord, line_height: float) -> bool:
    """Одиночное тире между словами («далее — Товар») — текст, а не остаток линии таблицы:
    оно не длиннее двух высот строки."""
    return w.text in ("—", "–", "-") and w.width <= 2 * line_height


def clean_words(words: list[TsvWord]) -> list[TsvWord]:
    """Убирает мусор OCR: слова из одних линий, слова с уверенностью ниже 0,3 и строки
    со средней уверенностью ниже 0,55. Строки берутся по геометрии, а не по разбивке
    Tesseract, чтобы номер пункта считался вместе со своей строкой. Короткие строки
    («м», «кг», «шт.») порог проходят: Tesseract читает их с уверенностью 0,73–0,89."""
    kept: list[TsvWord] = []
    dropped_lines = dropped_words = 0
    candidates: list[TsvWord] = []
    line_height = statistics.median(w.height for w in words) if words else 0
    for w in words:
        if (set(w.text) <= _LINE_ONLY and not _is_dash(w, line_height)) or w.conf < WORD_MIN_CONF:
            dropped_words += 1
            continue
        candidates.append(w)
    for line in pixel_lines(candidates):
        chars = sum(len(w.text) for w in line)
        conf = sum(w.conf * len(w.text) for w in line) / chars
        if conf < LINE_MIN_CONF:
            dropped_lines += 1
            log.debug("ocr: строка отброшена (уверенность %.2f < %.2f): %s", conf, LINE_MIN_CONF,
                      " ".join(w.text for w in line)[:80])
            continue
        kept.extend(line)
    if dropped_words or dropped_lines:
        log.debug("ocr: отброшено слов %d (порог %.2f), строк %d (порог %.2f)",
                  dropped_words, WORD_MIN_CONF, dropped_lines, LINE_MIN_CONF)
    return kept


def _page_result(no: int, width: float, height: float, prep: Prepared, words: list[TsvWord]) -> OcrPage:
    settings = get_settings()
    out: list[OcrWord] = []
    for w in clean_words(words):
        x0, y0, x1, y1 = prep.to_page(w.left, w.top, w.left + w.width, w.top + w.height)
        scale = 72 / prep.dpi
        out.append(
            OcrWord(
                text=w.text,
                bbox=BBox(x0=max(0.0, x0), y0=max(0.0, y0), x1=min(width, x1), y1=min(height, y1)),
                layout=BBox(x0=w.left * scale, y0=w.top * scale, x1=(w.left + w.width) * scale,
                            y1=(w.top + w.height) * scale),
                confidence=round(w.conf, 4),
                block=w.block,
                par=w.par,
                line=w.line,
            )
        )
    chars = sum(len(w.text) for w in out)
    confidence = round(sum(w.confidence * len(w.text) for w in out) / chars, 4) if chars else None
    low = confidence is not None and confidence < settings.ocr_low_quality_threshold
    if low:
        log.info("ocr: стр. %d: низкое качество распознавания %.2f < %.2f", no, confidence,
                 settings.ocr_low_quality_threshold)
    return OcrPage(page=no, width=width, height=height, rotation=prep.rotation, skew=prep.skew,
                   words=out, confidence=confidence, low_quality=low)


def ocr_pages(
    path: str | Path,
    pages: Sequence[int],
    *,
    progress: ProgressFn | None = None,
    images_dir: Path | None = None,
    workers: int | None = None,
    use_cache: bool = True,
) -> list[OcrPage]:
    """OCR страниц `pages` (с 1). Возвращает только слова с рамками в пунктах страницы.

    `images_dir` — куда сохранить изображения страниц (JPEG) для FR-5.6*; путь кладётся
    в `OcrPage.image_key`, загрузку в MinIO делает задача Celery.
    """
    settings = get_settings()
    dpi, lang = settings.ocr_dpi, settings.ocr_lang
    digest = file_sha256(path)
    cache_dir = settings.cache_dir / "ocr"
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)

    results: dict[int, OcrPage] = {}
    todo: list[int] = []
    for no in pages:
        cached = cache_dir / f"{_cache_key(digest, no, dpi, lang)}.json"
        if use_cache and cached.exists():
            results[no] = OcrPage.model_validate_json(cached.read_text())
        else:
            todo.append(no)

    done = len(results)
    if progress:
        progress(done, len(pages))
    if todo:
        pool = workers or max(1, min(4, os.cpu_count() or 1))
        if images_dir is not None:
            images_dir.mkdir(parents=True, exist_ok=True)

        def run(no: int, width: float, height: float, original: np.ndarray) -> OcrPage:
            # Всё, кроме рендера, — в потоке: OpenCV и Tesseract отпускают GIL. Подготовка в главном
            # потоке шла подряд для всех страниц и занимала половину времени скана (замер 27.10)
            if images_dir is not None:
                cv2.imwrite(str(images_dir / f"page-{no:04d}.jpg"), original, [cv2.IMWRITE_JPEG_QUALITY, 80])
            prep = prepare_image(original, dpi)
            words = recognize(prep.image, lang=lang, dpi=dpi, timeout=settings.ocr_timeout)
            refine_detached_prefixes(prep.image, words, lang=lang, dpi=dpi, timeout=settings.ocr_timeout)
            result = _page_result(no, width, height, prep, words)
            if images_dir is not None:
                result.image_key = str(images_dir / f"page-{no:04d}.jpg")
            return result

        def finish(result: OcrPage) -> None:
            nonlocal done
            results[result.page] = result
            if use_cache:
                target = cache_dir / f"{_cache_key(digest, result.page, dpi, lang)}.json"
                tmp = target.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
                os.replace(tmp, target)
            done += 1
            if progress:
                progress(done, len(pages))

        # Рендер — в этом потоке (PyMuPDF не потокобезопасен). В работе не больше 2×pool страниц:
        # рендер 300 dpi — около 9 МБ на лист, скан на 500 страниц целиком занял бы 4 ГБ
        pending: deque[Future[OcrPage]] = deque()
        with open_pdf(path) as doc, ThreadPoolExecutor(max_workers=pool) as executor:
            for no in todo:
                page = doc[no - 1]
                pending.append(executor.submit(run, no, page.rect.width, page.rect.height, render(page, dpi)))
                if len(pending) >= 2 * pool:
                    finish(pending.popleft().result())
            while pending:
                finish(pending.popleft().result())
    return [results[no] for no in pages]
