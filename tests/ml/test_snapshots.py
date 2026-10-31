"""Эталонные снимки парсера: три документа (DOCX, PDF с текстом, скан) → сохранённый результат.

Тест показывает любое изменение разбора. Изменение ожидаемое — обновите снимки:
`UPDATE_SNAPSHOTS=1 pytest tests/ml/test_snapshots.py`. Вёрстка PDF зависит от шрифта,
а OCR — от версии Tesseract, поэтому их снимки привязаны к окружению; для нового
окружения снимок создаётся той же командой.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from bench.fonts import font_path
from bench.samples import make_contract_docx, make_contract_pdf
from bench.scangen import make_scan
from ml.extract.docx_reader import extract_docx
from ml.extract.pdf_extract import extract_pdf
from ml.ocr.tesseract import tesseract_version

SNAPSHOTS = Path(__file__).resolve().parents[1] / "snapshots"


def summary(result) -> dict:
    return {
        "kind": result.kind,
        "pages": result.pages,
        "paragraphs": [
            {
                "number": p.number,
                "text": p.text,
                "page": p.page,
                "heading": p.heading_level,
                "stream": p.stream,
                "table": [p.table.row, p.table.col, p.table.row_span, p.table.col_span] if p.table else None,
                "spans": len(p.source_map.runs) or len(p.source_map.words),
            }
            for p in result.paragraphs
        ],
    }


def check_snapshot(name: str, data: dict) -> None:
    path = SNAPSHOTS / f"{name}.json"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        return
    if not path.exists():
        pytest.skip(f"нет снимка {path.name} для этого окружения: UPDATE_SNAPSHOTS=1")
    assert json.loads(path.read_text()) == data


def _font_tag() -> str:
    font = font_path("serif")
    return re.sub(r"[^a-z0-9]+", "-", font.stem.lower()) if font else "none"


def test_docx_snapshot(tmp_path):
    check_snapshot("docx_contract", summary(extract_docx(make_contract_docx(tmp_path / "c.docx"))))


@pytest.mark.skipif(font_path("serif") is None, reason="нет шрифта с кириллицей")
def test_pdf_snapshot(tmp_path):
    result = extract_pdf(make_contract_pdf(tmp_path / "c.pdf"), run_ocr=False)
    check_snapshot(f"pdf_contract.{_font_tag()}", summary(result))


@pytest.mark.tesseract
@pytest.mark.skipif(font_path("serif") is None, reason="нет шрифта с кириллицей")
def test_scan_snapshot(tmp_path):
    scan = make_scan(make_contract_pdf(tmp_path / "c.pdf"), tmp_path / "s.pdf", seed=7)
    check_snapshot(f"scan_contract.{_font_tag()}.tess{tesseract_version()}", summary(extract_pdf(scan)))
