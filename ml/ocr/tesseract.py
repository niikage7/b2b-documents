"""Вызов Tesseract через командную строку: распознавание в TSV и определение ориентации.

`OMP_THREAD_LIMIT=1`: один поток на процесс, параллельность — по страницам. Так
результат повторяем и не зависит от загрузки машины.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from ml.config import get_settings
from ml.errors import OcrError


@dataclass
class TsvWord:
    block: int
    par: int
    line: int
    left: int
    top: int
    width: int
    height: int
    conf: float  # 0–1
    text: str


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"
    return env


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise OcrError(detail="не удалось закодировать изображение")
    return buf.tobytes()


def _run(args: list[str], data: bytes, timeout: float) -> str:
    settings = get_settings()
    try:
        proc = subprocess.run(
            [settings.tesseract_bin, "stdin", "stdout", *args],
            input=data,
            capture_output=True,
            timeout=timeout,
            env=_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise OcrError(detail=f"нет {settings.tesseract_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrError(detail=f"таймаут {timeout:.0f} с") from exc
    if proc.returncode != 0:
        raise OcrError(detail=proc.stderr.decode(errors="replace")[-500:])
    return proc.stdout.decode("utf-8", errors="replace")


@lru_cache(maxsize=1)
def tesseract_version() -> str:
    settings = get_settings()
    try:
        out = subprocess.run([settings.tesseract_bin, "--version"], capture_output=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    text = (out.stdout or out.stderr).decode(errors="replace")
    m = re.search(r"tesseract\s+v?([\d.]+)", text)
    return m.group(1) if m else "unknown"


@lru_cache(maxsize=4)
def _available_langs(tessdata: str | None) -> frozenset[str] | None:
    settings = get_settings()
    try:
        out = subprocess.run([settings.tesseract_bin, "--list-langs"], capture_output=True, timeout=10,
                             env=_env(), check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    lines = (out.stdout + out.stderr).decode(errors="replace").splitlines()
    return frozenset(line.strip() for line in lines[1:] if line.strip())


def require_langs(lang: str) -> None:
    """Модели языков `lang` («rus+eng») установлены. Без модели `rus` Tesseract не падает, а молча
    читает кириллицу латиницей («Поставщик» → «MOCTaBIIIHK»), и такой текст попал бы в кеш OCR.
    """
    available = _available_langs(os.environ.get("TESSDATA_PREFIX"))
    if available is None:
        return  # нет бинарника — ошибку с причиной даст сам вызов распознавания
    missing = [x for x in lang.split("+") if x and x not in available]
    if missing:
        raise OcrError(detail=f"нет модели Tesseract для языков: {', '.join(missing)}")


def recognize(img: np.ndarray, *, lang: str, dpi: int, psm: int = 3, timeout: float = 60) -> list[TsvWord]:
    """Слова с рамками и уверенностью. `--psm 3`: `--psm 6` склеивает столбцы таблиц в одну строку."""
    # Не конфиг «tsv»: при своём TESSDATA_PREFIX каталога configs рядом может не быть
    args = ["-l", lang, "--psm", str(psm), "--dpi", str(dpi), "-c", "tessedit_create_tsv=1", "-c", "tessedit_create_txt=0"]
    out = _run(args, _png(img), timeout)
    words: list[TsvWord] = []
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        words.append(
            TsvWord(
                block=int(cols[2]),
                par=int(cols[3]),
                line=int(cols[4]),
                left=int(cols[6]),
                top=int(cols[7]),
                width=int(cols[8]),
                height=int(cols[9]),
                conf=conf / 100,
                text=text,
            )
        )
    return words


@dataclass
class Orientation:
    rotate: int  # на сколько градусов по часовой повернуть изображение, чтобы текст встал прямо
    confidence: float


def detect_orientation(img: np.ndarray, *, dpi: int = 300, timeout: float = 30) -> Orientation | None:
    """Ориентация по `--psm 0`. Мало текста на странице — None, страница остаётся как есть."""
    try:
        out = _run(["--psm", "0", "--dpi", str(dpi)], _png(img), timeout)
    except OcrError:
        return None
    rotate = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rotate or not conf:
        return None
    return Orientation(int(rotate.group(1)) % 360, float(conf.group(1)))
