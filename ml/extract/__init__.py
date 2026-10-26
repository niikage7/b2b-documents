"""Извлечение текста: DOCX, PDF с текстовым слоем, классификация страниц, сборка абзацев.

`extract_document` делает всё в одном процессе (проверка, конвертация, разбор, OCR):
для тестов, стендов и небольших документов. В конвейере Celery этапы вызываются по
отдельности: `plan_pdf` → `ocr_pages` по группам → `build_pdf_result`.
"""

from __future__ import annotations

from pathlib import Path

from contracts.extraction import ExtractionResult


def extract_document(path: str | Path) -> ExtractionResult:
    from ml.convert import convert_to_docx
    from ml.extract.docx_reader import extract_docx
    from ml.extract.pdf_extract import extract_pdf
    from ml.files import require_ok

    check = require_ok(path)
    if check.kind == "pdf":
        return extract_pdf(path)
    if check.kind == "docx":
        return extract_docx(path)
    if check.kind in ("doc", "rtf"):
        return extract_docx(convert_to_docx(path), kind=check.kind)
    from ml.errors import UnsupportedFormatError

    raise UnsupportedFormatError()
