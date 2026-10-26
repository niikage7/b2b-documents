from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Локально модель rus для Tesseract может лежать в кеше проекта (в образе — пакет tesseract-ocr-rus)
_LOCAL_TESSDATA = ROOT / ".cache" / "tessdata"
if "TESSDATA_PREFIX" not in os.environ and (_LOCAL_TESSDATA / "rus.traineddata").exists():
    os.environ["TESSDATA_PREFIX"] = str(_LOCAL_TESSDATA)


def _tesseract_ready() -> bool:
    if shutil.which("tesseract") is None:
        return False
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=False)
    langs = set((out.stdout + out.stderr).split())
    return {"rus", "eng"} <= langs


TESSERACT = _tesseract_ready()


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Каждый тест — со своим кешем конвертации, OCR и LLM."""
    monkeypatch.setenv("ML_CACHE_DIR", str(tmp_path / "ml-cache"))
    from ml.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="нет Tesseract с языками rus и eng")
    for item in items:
        if "tesseract" in item.keywords and not TESSERACT:
            item.add_marker(skip)
