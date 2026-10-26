"""Генератор «качественных сканов» из PDF с текстовым слоем.

Рендер 300 dpi, серый фон бумаги, наклон до 0,6°, лёгкий шум, JPEG. Страница
результата — только картинка, без текстового слоя. Эталон для замера OCR — текстовый
слой исходного PDF. Параметры детерминированы через `seed`.

    python -m bench.scangen исходный.pdf скан.pdf
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pymupdf


@dataclass(frozen=True)
class ScanStyle:
    dpi: int = 300
    max_skew: float = 0.6
    paper: tuple[int, int] = (232, 246)
    noise: float = 5.0
    jpeg_quality: int = 80
    blur: bool = True
    rotate90: int = 0  # повернуть страницу целиком: 0, 90, 180, 270 против часовой


def scan_page(page: pymupdf.Page, style: ScanStyle, rng: np.random.Generator) -> np.ndarray:
    pix = page.get_pixmap(dpi=style.dpi, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].astype(np.float32)
    paper = rng.uniform(*style.paper)
    # Чернила остаются тёмными, белое становится серой бумагой
    img = paper - (255 - img) * (paper / 255)
    angle = float(rng.uniform(-style.max_skew, style.max_skew))
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=float(paper))
    if style.blur:
        img = cv2.GaussianBlur(img, (3, 3), 0.6)
    img += rng.normal(0, style.noise, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if style.rotate90:
        img = np.ascontiguousarray(np.rot90(img, style.rotate90 // 90))
    return img


def make_scan(src: Path, dst: Path, *, style: ScanStyle | None = None, seed: int = 0,
              pages: list[int] | None = None) -> Path:
    style = style or ScanStyle()
    rng = np.random.default_rng(seed)
    out = pymupdf.open()
    with pymupdf.open(src) as doc:
        numbers = pages or list(range(1, doc.page_count + 1))
        for no in numbers:
            page = doc[no - 1]
            img = scan_page(page, style, rng)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, style.jpeg_quality])
            assert ok
            rect = page.rect
            if style.rotate90 in (90, 270):
                rect = pymupdf.Rect(0, 0, rect.height, rect.width)
            new = out.new_page(width=rect.width, height=rect.height)
            new.insert_image(new.rect, stream=buf.tobytes())
    out.save(dst, garbage=3, deflate=True)
    out.close()
    return dst


def reference_text(src: Path, pages: list[int] | None = None) -> dict[int, str]:
    with pymupdf.open(src) as doc:
        numbers = pages or list(range(1, doc.page_count + 1))
        return {no: doc[no - 1].get_text("text") for no in numbers}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--skew", type=float, default=0.6)
    parser.add_argument("--noise", type=float, default=5.0)
    args = parser.parse_args()
    make_scan(args.src, args.dst, style=ScanStyle(max_skew=args.skew, noise=args.noise), seed=args.seed)
    print(args.dst)


if __name__ == "__main__":
    main()
