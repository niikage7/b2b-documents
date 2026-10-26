"""Стенд автонумерации DOCX: наши номера против номеров в рендере LibreOffice.

DOCX конвертируется в PDF, для каждого абзаца с номером ищется строка рендера, которая
начинается с начала его текста, и сравнивается то, что стоит перед текстом. LibreOffice
в редких случаях нумерует иначе, чем Word: такие расхождения разбираются вручную.

    python -m bench.numbering договор.docx [ещё.docx ...]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from ml.convert import soffice_convert
from ml.extract.docx_reader import extract_docx
from ml.extract.layout import group_lines
from ml.extract.pdf_text import page_text_words

_SPACE = re.compile(r"\s+")


@dataclass
class Mismatch:
    text: str
    ours: str
    rendered: str


@dataclass
class Report:
    path: Path
    numbered: int
    checked: int
    mismatches: list[Mismatch]


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def check(path: Path) -> Report:
    result = extract_docx(path)
    pdf = soffice_convert(path, "pdf")
    lines: list[str] = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            words, _ = page_text_words(page)
            lines.extend(_norm(line.text) for line in sorted(group_lines(words), key=lambda ln: (ln.y0, ln.x0)))
    numbered = [p for p in result.paragraphs if p.number and p.stream == "body" and p.table is None]
    mismatches: list[Mismatch] = []
    checked = 0
    cursor = 0  # строки рендера просматриваются по порядку: одинаковые начала пунктов не путаются
    for item in numbered:
        head = _norm(item.text)[:30]
        if len(head) < 5:
            continue
        pos = next((i for i in range(cursor, len(lines)) if head in lines[i]), None)
        if pos is None:
            continue
        cursor = pos + 1
        line = lines[pos]
        checked += 1
        rendered = line[: line.index(head)].strip()
        if rendered.replace(" ", "") != item.number.replace(" ", ""):
            mismatches.append(Mismatch(head, item.number, rendered))
    return Report(path, len(numbered), checked, mismatches)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        report = check(path)
        print(f"{path.name}: пунктов с номером {report.numbered}, сверено {report.checked}, "
              f"расхождений {len(report.mismatches)}")
        for m in report.mismatches[:30]:
            print(f"  наш «{m.ours}» ≠ рендер «{m.rendered}»: {m.text}")


if __name__ == "__main__":
    main()
