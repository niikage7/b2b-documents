"""Стенд точности OCR (критерий «≥ 95% на качественных сканах», FR-3.3).

Из PDF с текстовым слоем генерируются «качественные сканы» (`bench.scangen`), их
распознаёт весь конвейер (`plan_pdf` → `ocr_pages`), эталон — текстовый слой
исходника. Метрика — 1 − CER по нормализованному тексту страницы.

Правила нормализации (одинаково для эталона и распознавания):

1. Unicode NFKC.
2. «ё» → «е», «Ё» → «Е».
3. Кавычки «»„“”″ → ", ‘’ → '.
4. Дефисы и тире ‐‑‒–—− → -.
5. Мягкий перенос удаляется.
6. Любая последовательность пробельных символов → один пробел, края обрезаются.

Регистр, цифры и знаки препинания сохраняются. Строки обеих сторон собираются одним
кодом (`group_lines`) и упорядочиваются сверху вниз, слева направо, поэтому
колонтитулы и колонки входят в сравнение одинаково.

    python -m bench.ocr_cer                    # синтетический договор и извещение
    python -m bench.ocr_cer docs/*.pdf --seeds 3
"""

from __future__ import annotations

import argparse
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from rapidfuzz.distance import Levenshtein

from bench.scangen import ScanStyle, make_scan
from ml.extract.layout import group_lines
from ml.extract.pdf_extract import ocr_words, plan_pdf
from ml.extract.pdf_text import page_text_words
from ml.ocr.pages import ocr_pages

_QUOTES = str.maketrans({"«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "″": '"', "‘": "'", "’": "'"})
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("ё", "е").replace("Ё", "Е").translate(_QUOTES).translate(_DASHES).replace("­", "")
    return _SPACES.sub(" ", text).strip()


def lines_text(words) -> str:
    lines = sorted(group_lines(words), key=lambda line: (round(line.y0 / 4), line.x0))
    return "\n".join(line.text for line in lines)


@dataclass
class PageScore:
    source: str
    page: int
    chars: int
    distance: int
    seconds: float

    @property
    def accuracy(self) -> float:
        return 1 - self.distance / self.chars if self.chars else 1.0


def score_pdf(src: Path, *, seed: int = 0, style: ScanStyle | None = None, workdir: Path) -> list[PageScore]:
    scan = make_scan(src, workdir / f"{src.stem}.scan{seed}.pdf", style=style, seed=seed)
    reference: dict[int, str] = {}
    with pymupdf.open(src) as doc:
        for page in doc:
            words, _ = page_text_words(page)
            reference[page.number + 1] = normalize(lines_text(words))
    plan = plan_pdf(scan)
    needed = [p.page for p in plan if p.needs_ocr]
    started = time.perf_counter()
    pages = ocr_pages(scan, needed, use_cache=False)
    per_page = (time.perf_counter() - started) / max(1, len(pages))
    scores = []
    for ocr in pages:
        hyp = normalize(lines_text(ocr_words(ocr)))
        ref = reference.get(ocr.page, "")
        scores.append(PageScore(src.name, ocr.page, len(ref), Levenshtein.distance(ref, hyp), per_page))
    missing = set(reference) - set(needed)
    for page in sorted(missing):
        # Страница не ушла в OCR: для скана это ошибка классификации, считаем её целиком потерянной
        scores.append(PageScore(src.name, page, len(reference[page]), len(reference[page]), 0.0))
    return scores


def synthetic_sources(workdir: Path) -> list[Path]:
    from bench.samples import make_contract_pdf, make_notice_pdf

    return [
        make_contract_pdf(workdir / "contract-12pt.pdf"),
        make_contract_pdf(workdir / "contract-11pt.pdf", font_size=11),
        make_notice_pdf(workdir / "notice.pdf"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF с текстовым слоем; по умолчанию синтетика")
    parser.add_argument("--seeds", type=int, default=1, help="сколько вариантов скана на файл")
    parser.add_argument("--skew", type=float, default=0.6)
    parser.add_argument("--noise", type=float, default=5.0)
    parser.add_argument("--threshold", type=float, default=0.95)
    args = parser.parse_args()
    style = ScanStyle(max_skew=args.skew, noise=args.noise)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        sources = args.pdfs or synthetic_sources(workdir)
        scores: list[PageScore] = []
        for src in sources:
            for seed in range(args.seeds):
                scores.extend(score_pdf(src, seed=seed, style=style, workdir=workdir))
    chars = sum(s.chars for s in scores)
    dist = sum(s.distance for s in scores)
    accuracy = 1 - dist / chars if chars else 0.0
    print(f"{'файл':<28} {'стр':>4} {'знаков':>7} {'1-CER':>7}")
    for s in sorted(scores, key=lambda s: s.accuracy)[:15]:
        print(f"{s.source:<28} {s.page:>4} {s.chars:>7} {s.accuracy:>7.4f}")
    print(f"\nстраниц: {len(scores)}, знаков: {chars}, ошибок: {dist}")
    print(f"1 − CER = {accuracy:.4f} (порог {args.threshold}) — {'OK' if accuracy >= args.threshold else 'НИЖЕ ПОРОГА'}")
    if scores:
        print(f"OCR: {sum(s.seconds for s in scores) / len(scores):.2f} с на страницу")


if __name__ == "__main__":
    main()
