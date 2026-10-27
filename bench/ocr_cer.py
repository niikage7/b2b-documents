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
6. Подчёркивания удаляются: «________» — поле для заполнения, на скане это линия, и
   подготовка стирает её вместе с линиями таблиц. `--keep-underscores` оставляет их.
7. Любая последовательность пробельных символов → один пробел, края обрезаются.

Регистр, цифры и знаки препинания сохраняются. Строки обеих сторон собираются одним
кодом (`group_lines`) и упорядочиваются сверху вниз, слева направо, поэтому
колонтитулы и колонки входят в сравнение одинаково.

    python -m bench.ocr_cer                    # синтетический договор и извещение
    python -m bench.ocr_cer docs/*.pdf --seeds 3
    python -m bench.ocr_cer .cache/packages/*/* --save pages.json

DOCX, DOC и RTF сначала сохраняются в PDF через LibreOffice: так проверяются макеты
настоящих документов — таблицы ТЗ, печатные формы, мелкий шрифт. `--save` пишет эталон и
распознавание каждой страницы для разбора ошибок.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
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


def normalize(text: str, *, underscores: bool = False) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("ё", "е").replace("Ё", "Е").translate(_QUOTES).translate(_DASHES).replace("­", "")
    if not underscores:
        text = text.replace("_", " ")
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
    ref: str = ""
    hyp: str = ""

    @property
    def accuracy(self) -> float:
        return 1 - self.distance / self.chars if self.chars else 1.0


def as_pdf(path: Path) -> Path:
    """PDF как есть; DOCX, DOC и RTF — через LibreOffice, как их напечатал бы заказчик."""
    if path.suffix.lower() == ".pdf":
        return path
    from ml.convert import soffice_convert

    return soffice_convert(path, "pdf")


def score_pdf(src: Path, *, seed: int = 0, style: ScanStyle | None = None, workdir: Path,
              name: str | None = None, underscores: bool = False) -> list[PageScore]:
    name = name or src.name
    scan = make_scan(src, workdir / f"{src.stem}.scan{seed}.pdf", style=style, seed=seed)
    reference: dict[int, str] = {}
    with pymupdf.open(src) as doc:
        for page in doc:
            words, _ = page_text_words(page)
            reference[page.number + 1] = normalize(lines_text(words), underscores=underscores)
    plan = plan_pdf(scan)
    needed = [p.page for p in plan if p.needs_ocr]
    started = time.perf_counter()
    pages = ocr_pages(scan, needed, use_cache=False)
    per_page = (time.perf_counter() - started) / max(1, len(pages))
    scores = []
    for ocr in pages:
        hyp = normalize(lines_text(ocr_words(ocr)), underscores=underscores)
        ref = reference.get(ocr.page, "")
        scores.append(PageScore(name, ocr.page, len(ref), Levenshtein.distance(ref, hyp), per_page, ref, hyp))
    missing = set(reference) - set(needed)
    for page in sorted(missing):
        # Страница не ушла в OCR: для скана это ошибка классификации, считаем её целиком потерянной
        scores.append(PageScore(name, page, len(reference[page]), len(reference[page]), 0.0, reference[page]))
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
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF с текстовым слоем, DOCX, DOC, RTF; по умолчанию синтетика")
    parser.add_argument("--seeds", type=int, default=1, help="сколько вариантов скана на файл")
    parser.add_argument("--skew", type=float, default=0.6)
    parser.add_argument("--noise", type=float, default=5.0)
    parser.add_argument("--threshold", type=float, default=0.95)
    parser.add_argument("--save", type=Path, help="JSON с эталоном и распознаванием каждой страницы")
    parser.add_argument("--keep-underscores", action="store_true", help="не удалять подчёркивания при сравнении")
    args = parser.parse_args()
    style = ScanStyle(max_skew=args.skew, noise=args.noise)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        sources = [(as_pdf(p), p.name if p.suffix.lower() == ".pdf" else f"{p.parent.name}/{p.name}")
                   for p in args.pdfs] or [(p, p.name) for p in synthetic_sources(workdir)]
        scores: list[PageScore] = []
        for src, name in sources:
            for seed in range(args.seeds):
                scores.extend(score_pdf(src, seed=seed, style=style, workdir=workdir, name=name,
                                        underscores=args.keep_underscores))
    chars = sum(s.chars for s in scores)
    dist = sum(s.distance for s in scores)
    accuracy = 1 - dist / chars if chars else 0.0
    if args.save:
        args.save.write_text(json.dumps([asdict(s) for s in scores], ensure_ascii=False))
    by_doc: dict[str, list[PageScore]] = defaultdict(list)
    for s in scores:
        by_doc[s.source].append(s)
    if len(by_doc) > 1:
        print(f"{'файл':<44} {'стр':>4} {'знаков':>7} {'1-CER':>7}")
        for source, items in by_doc.items():
            c, d = sum(s.chars for s in items), sum(s.distance for s in items)
            print(f"{source:<44} {len(items):>4} {c:>7} {1 - d / c if c else 1.0:>7.4f}")
        print()
    print(f"{'файл':<44} {'стр':>4} {'знаков':>7} {'1-CER':>7}")
    for s in sorted(scores, key=lambda s: s.accuracy)[:15]:
        print(f"{s.source:<44} {s.page:>4} {s.chars:>7} {s.accuracy:>7.4f}")
    print(f"\nстраниц: {len(scores)}, знаков: {chars}, ошибок: {dist}")
    print(f"1 − CER = {accuracy:.4f} (порог {args.threshold}) — {'OK' if accuracy >= args.threshold else 'НИЖЕ ПОРОГА'}")
    if scores:
        print(f"OCR: {sum(s.seconds for s in scores) / len(scores):.2f} с на страницу")


if __name__ == "__main__":
    main()
