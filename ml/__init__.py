"""ML-часть: извлечение текста, OCR, эмбеддинги, поиск, LLM и движок правил.

Обычные функции Python без зависимости от Celery: вход — путь к файлу или данные,
выход — данные по контрактам из `contracts/`. Задачи Celery оборачивают их, тесты и
стенды зовут напрямую. Публичные функции перечислены в `__all__`; импорт ленивый,
чтобы API не тянул OpenCV и PyMuPDF.
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "check_file": "ml.files",
    "convert_to_docx": "ml.convert",
    "extract_document": "ml.extract",
    "extract_docx": "ml.extract.docx_reader",
    "plan_pdf": "ml.extract.pdf_extract",
    "build_pdf_result": "ml.extract.pdf_extract",
    "classify_pages": "ml.extract.page_classes",
    "ocr_pages": "ml.ocr.pages",
    "embed_texts": "ml.analysis.embeddings",
    "embed_rule_query": "ml.analysis.embeddings",
    "search_chunks": "ml.analysis.search",
    "group_rules": "ml.analysis.engine",
    "analyze_rule_group": "ml.analysis.engine",
    "anchor_quote": "ml.analysis.anchor",
    "validate_rule": "ml.analysis.templates",
    "rule_query_text": "ml.analysis.templates",
    "test_rule": "ml.analysis.rule_test",
    "extract_batch_meta": "ml.analysis.batch_meta",
    "load_default_checklist": "ml.checklist",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'ml' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
