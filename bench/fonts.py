"""Шрифты с кириллицей для синтетических документов: macOS и Debian (образ воркера)."""

from __future__ import annotations

import os
from pathlib import Path

_CANDIDATES = {
    "serif": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ],
    "serif-bold": [
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ],
    "sans": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}


def font_path(kind: str = "serif") -> Path | None:
    env = os.environ.get(f"BENCH_FONT_{kind.upper().replace('-', '_')}")
    if env and Path(env).exists():
        return Path(env)
    for candidate in _CANDIDATES[kind]:
        if Path(candidate).exists():
            return Path(candidate)
    return None
