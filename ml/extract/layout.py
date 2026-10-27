"""Сборка абзацев из слов с рамками: общая часть PDF с текстовым слоем и OCR.

Слова → строки → порядок чтения (колонки читаются колонка за колонкой) → абзацы →
номер пункта по шаблону → склейка абзаца через границу страницы.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from contracts.extraction import BBox, RawParagraph, SourceMap, WordBox
from ml.extract.text import CLAUSE_NUMBER, clean_text

log = logging.getLogger(__name__)

COLUMN_GAP = 2.5
"""Разрыв в строке от 2,5 кегля — граница колонки."""
HEADER_ZONE = 0.10
"""Колонтитул ищется в полосе 10% высоты у верхнего и нижнего края листа."""

_NUMBER = CLAUSE_NUMBER
_TERMINAL = re.compile(r"[.!?;:]$")
_LIST_START = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3})*[.)]|[а-яa-z]\)|[-–—•·▪])\s")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


@dataclass
class LWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    page: int
    bold: bool = False
    conf: float | None = None
    line_key: object = None
    sup_suffix: int = 0  # сколько последних символов — надстрочные цифры (возможный знак сноски)
    # Рамка на странице для экспорта, если строки собираются в другой системе (выпрямленный скан)
    page_bbox: tuple[float, float, float, float] | None = None

    @property
    def export_box(self) -> tuple[float, float, float, float]:
        return self.page_bbox or (self.x0, self.y0, self.x1, self.y1)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Line:
    words: list[LWord]
    page: int
    x0: float = 0
    y0: float = 0
    x1: float = 0
    y1: float = 0
    size: float = 0
    bold: bool = False

    def __post_init__(self) -> None:
        self.words.sort(key=lambda w: w.x0)
        self.x0 = min(w.x0 for w in self.words)
        self.y0 = min(w.y0 for w in self.words)
        self.x1 = max(w.x1 for w in self.words)
        self.y1 = max(w.y1 for w in self.words)
        self.size = statistics.median(w.size for w in self.words)
        self.bold = all(w.bold for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Para:
    lines: list[Line] = field(default_factory=list)
    column_x0: float = 0
    column_x1: float = 0

    @property
    def page(self) -> int:
        return self.lines[0].page


# --- строки ---------------------------------------------------------------------------


def group_lines(words: list[LWord]) -> list[Line]:
    """Слова одной страницы → строки. Готовая разбивка (`line_key`) уважается, но строка
    всё равно режется на большом разрыве: это граница колонки."""
    if not words:
        return []
    by_key: dict[object, list[LWord]] = {}
    loose: list[LWord] = []
    for w in words:
        if w.line_key is None:
            loose.append(w)
        else:
            by_key.setdefault(w.line_key, []).append(w)
    groups = list(by_key.values())
    groups.extend(_cluster_by_height(loose))
    lines: list[Line] = []
    for group in groups:
        group.sort(key=lambda w: w.x0)
        current = [group[0]]
        for prev, w in zip(group, group[1:], strict=False):
            size = max(prev.size, w.size)
            if w.x0 - prev.x1 >= COLUMN_GAP * size:
                lines.append(Line(current, current[0].page))
                current = []
            current.append(w)
        lines.append(Line(current, current[0].page))
    return lines


def _cluster_by_height(words: list[LWord]) -> list[list[LWord]]:
    """Слова в одной строке, если центры по высоте расходятся меньше чем на 0,45 кегля."""
    groups: list[list[LWord]] = []
    centers: list[float] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        best = None
        for i in range(len(groups) - 1, max(-1, len(groups) - 6), -1):
            if abs(centers[i] - w.cy) < 0.45 * max(w.size, groups[i][0].size):
                best = i
                break
        if best is None:
            groups.append([w])
            centers.append(w.cy)
        else:
            groups[best].append(w)
    return groups


# --- колонтитулы ----------------------------------------------------------------------


def _norm_header(text: str) -> str:
    return _DIGITS.sub("#", _SPACE.sub(" ", text.strip().lower()))


def remove_headers_footers(pages: dict[int, list[Line]], heights: dict[int, float]) -> int:
    """Убирает строки, которые повторяются у края листа на половине страниц и больше
    (минимум на двух), и одиночные номера страниц у края. Возвращает число строк."""
    if not pages:
        return 0
    counts: Counter[str] = Counter()
    for page, lines in pages.items():
        seen = set()
        for line in lines:
            if _in_edge_zone(line, heights[page]):
                seen.add(_norm_header(line.text))
        counts.update(seen)
    threshold = max(2, (len(pages) + 1) // 2)
    repeated = {key for key, n in counts.items() if n >= threshold and key}
    removed = 0
    for page, lines in pages.items():
        keep = []
        for line in lines:
            if _in_edge_zone(line, heights[page]):
                key = _norm_header(line.text)
                if key in repeated or _is_page_number(line.text):
                    log.debug("pdf: колонтитул стр. %d: %.60s (порог %d стр.)", page, line.text, threshold)
                    removed += 1
                    continue
            keep.append(line)
        pages[page] = keep
    return removed


def _in_edge_zone(line: Line, height: float) -> bool:
    return line.y1 <= HEADER_ZONE * height or line.y0 >= (1 - HEADER_ZONE) * height


def _is_page_number(text: str) -> bool:
    t = text.strip().strip("-–— ")
    return bool(re.fullmatch(r"(?:стр\.?|страница|с\.)?\s*\d{1,4}(?:\s*(?:из|/)\s*\d{1,4})?", t, re.I))


# --- знаки сносок ---------------------------------------------------------------------


def strip_footnote_marks(lines: list[Line], height: float) -> None:
    """Надстрочная цифра убирается, только если ниже на листе есть строка сноски с тем же
    номером — иначе «м²» потеряет двойку."""
    marks = [(line, w) for line in lines for w in line.words if w.sup_suffix]
    if not marks:
        return
    footnote_numbers: dict[str, float] = {}
    for line in lines:
        first = line.words[0]
        m = re.match(r"^(\d{1,3})", first.text)
        if m and (first.sup_suffix or len(first.text) <= 3) and line.y0 > height * 0.5:
            footnote_numbers.setdefault(m.group(1), line.y0)
    for line, w in marks:
        digits = w.text[-w.sup_suffix :]
        y = footnote_numbers.get(digits)
        if y is None or line is _line_of_footnote(lines, digits) or line.y0 >= y:
            continue
        log.debug("pdf: убран знак сноски %s в «%s»", digits, w.text)
        w.text = w.text[: -w.sup_suffix]
        w.sup_suffix = 0
    for line in lines:
        line.words = [w for w in line.words if w.text]


def _line_of_footnote(lines: list[Line], digits: str) -> Line | None:
    for line in lines:
        if line.words and line.words[0].text.startswith(digits) and line.words[0].sup_suffix:
            return line
    return None


# --- порядок чтения -------------------------------------------------------------------


def reading_order(lines: list[Line]) -> list[list[Line]]:
    """Рекурсивное XY-сечение по самому большому зазору: колонки читаются целиком.

    Возвращает потоки текста в порядке чтения. Новый поток начинается только на границе
    колонок; горизонтальные сечения меняют порядок, но не режут абзацы.
    """
    if not lines:
        return []
    size = statistics.median(line.size for line in lines) or 10
    leaves: list[tuple[int, list[Line]]] = []
    counter = [0]
    _xy_cut(lines, size, leaves, 0, counter)
    flows: list[list[Line]] = []
    last_id = None
    for col_id, leaf in leaves:
        if not leaf:
            continue
        if col_id == last_id:
            flows[-1].extend(leaf)
        else:
            flows.append(list(leaf))
            last_id = col_id
    return flows


def _xy_cut(lines: list[Line], size: float, out: list, col_id: int, counter: list[int]) -> None:
    if len(lines) <= 1:
        out.append((col_id, lines))
        return
    hcut, hgap = _best_gap(lines, axis="y")
    vcut, vgap = _best_gap(lines, axis="x")
    if vcut is not None and vgap >= COLUMN_GAP * size and (hcut is None or vgap > hgap * 2):
        for side in ([ln for ln in lines if ln.x1 <= vcut], [ln for ln in lines if ln.x1 > vcut]):
            counter[0] += 1
            _xy_cut(side, size, out, counter[0], counter)
        return
    if hcut is not None and hgap > 0:
        _xy_cut([ln for ln in lines if ln.y1 <= hcut], size, out, col_id, counter)
        _xy_cut([ln for ln in lines if ln.y1 > hcut], size, out, col_id, counter)
        return
    out.append((col_id, sorted(lines, key=lambda line: (line.y0, line.x0))))


def _best_gap(lines: list[Line], axis: str) -> tuple[float | None, float]:
    if axis == "y":
        spans = sorted((line.y0, line.y1) for line in lines)
    else:
        spans = sorted((line.x0, line.x1) for line in lines)
    best_cut, best_gap = None, 0.0
    reach = spans[0][1]
    for start, end in spans[1:]:
        gap = start - reach
        if gap > best_gap:
            best_gap, best_cut = gap, reach + gap / 2
        reach = max(reach, end)
    return best_cut, best_gap


# --- абзацы ---------------------------------------------------------------------------


def build_paragraphs(flows: list[list[Line]]) -> list[Para]:
    paras: list[Para] = []
    for flow in flows:
        if not flow:
            continue
        col_x0 = min(line.x0 for line in flow)
        col_x1 = max(line.x1 for line in flow)
        pitches = [
            b.cy - a.cy for a, b in zip(flow, flow[1:], strict=False) if 0 < b.cy - a.cy < 3 * a.size
        ]
        pitch = statistics.median(pitches) if pitches else flow[0].size * 1.2
        current = Para([flow[0]], col_x0, col_x1)
        for prev, line in zip(flow, flow[1:], strict=False):
            if _breaks(current, prev, line, pitch, col_x1):
                paras.append(current)
                current = Para([line], col_x0, col_x1)
            else:
                current.lines.append(line)
        paras.append(current)
    return paras


def _breaks(para: Para, prev: Line, line: Line, pitch: float, col_x1: float) -> bool:
    size = max(prev.size, line.size)
    if line.cy - prev.cy > max(pitch * 1.35, pitch + 0.3 * size):
        return True
    if abs(line.size - prev.size) > 0.15 * size or prev.bold != line.bold:
        return True
    prev_text = prev.text
    if _LIST_START.match(line.text) and (_TERMINAL.search(prev_text) or _NUMBER.match(para.lines[0].text)):
        # Два начала пункта в одном абзаце не бывает: «6. Заголовок» и «6.1. Текст» — разные абзацы
        return True
    if _centered(prev, para) and not _centered(line, para):
        return True
    if _TERMINAL.search(prev_text) and prev.x1 < col_x1 - 4 * size:
        return True
    if prev.x1 < para.column_x0 + 0.6 * (col_x1 - para.column_x0) and line.text[:1].isupper():
        # Короткая строка не доходит до правого края: это конец абзаца, даже без точки
        return True
    if len(para.lines) >= 2:
        # Край продолжения — по второй строке: у пункта с выступом номер левее продолжения
        edge = para.lines[1].x0
        if line.x0 > edge + 1.0 * size:
            return True
        if line.x0 < edge - 1.0 * size and _LIST_START.match(line.text):
            return True
    elif line.x0 > prev.x0 + 1.0 * size and not _NUMBER.match(prev_text):
        # Вторая строка правее первой без номера в начале — это красная строка нового абзаца
        return True
    return False


def _centered(line: Line, para: Para) -> bool:
    """Строка по центру колонки: поля с обеих сторон заметные и примерно равные. Красная
    строка абзаца, которая заканчивается чуть раньше правого края, центром не считается."""
    left = line.x0 - para.column_x0
    right = para.column_x1 - line.x1
    width = para.column_x1 - para.column_x0
    margin = max(3 * line.size, 0.1 * width)
    return left > margin and right > margin and abs(left - right) < max(2 * line.size, 0.05 * width)


def para_to_raw(para: Para, order: int, *, number_pattern: bool = True) -> RawParagraph | None:
    pieces: list[str] = []
    words: list[WordBox] = []
    pos = 0
    confs: list[tuple[float, int]] = []
    for li, line in enumerate(para.lines):
        for wi, w in enumerate(line.words):
            if pieces:
                hyphen = wi == 0 and li > 0 and pieces[-1].endswith("-") and w.text[:1].islower()
                if not hyphen:
                    pieces.append(" ")
                    pos += 1
            text = clean_text(w.text)
            x0, y0, x1, y1 = w.export_box
            words.append(
                WordBox(start=pos, end=pos + len(text), page=w.page, bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                        confidence=w.conf)
            )
            if w.conf is not None:
                confs.append((w.conf, len(text)))
            pieces.append(text)
            pos += len(text)
    text = "".join(pieces)
    if not text.strip():
        return None
    number = None
    if number_pattern:
        m = _NUMBER.match(text)
        if m and len(text) > m.end():
            number = m.group(0)
            cut = m.end() + (len(text[m.end():]) - len(text[m.end():].lstrip()))
            text = text[cut:]
            shifted = []
            for wb in words:
                if wb.end <= cut:
                    continue
                shifted.append(wb.model_copy(update={"start": max(wb.start, cut) - cut, "end": wb.end - cut}))
            words = shifted
    first_page = [w.export_box for line in para.lines if line.page == para.page for w in line.words]
    bbox = BBox(
        x0=min(b[0] for b in first_page),
        y0=min(b[1] for b in first_page),
        x1=max(b[2] for b in first_page),
        y1=max(b[3] for b in first_page),
    )
    last_page = para.lines[-1].page
    sizes = Counter()
    for line in para.lines:
        sizes[round(line.size * 2) / 2] += len(line.words)
    bold = all(line.bold for line in para.lines)
    letters = [c for c in text if c.isalpha()]
    all_caps = len(letters) >= 3 and all(c.isupper() for c in letters)
    ocr_conf = None
    if confs:
        total = sum(n for _, n in confs) or 1
        ocr_conf = sum(c * n for c, n in confs) / total
    edge = para.lines[1].x0 if len(para.lines) > 1 else para.lines[0].x0
    align = None
    if all(_centered(line, para) for line in para.lines):
        align = "center"
    return RawParagraph(
        order=order,
        text=text,
        number=number,
        number_source="pattern" if number else None,
        bold=bold,
        all_caps=all_caps,
        align=align,
        indent=round(edge - para.column_x0, 1),
        font_size=sizes.most_common(1)[0][0] if sizes else None,
        page=para.page,
        page_end=last_page if last_page != para.page else None,
        bbox=bbox,
        ocr_confidence=ocr_conf,
        source_map=SourceMap(format="pdf", words=words),
    )


def mark_headings(items: list[RawParagraph]) -> None:
    """Запасное правило заголовков: короткий абзац, жирный, прописными или крупнее основного текста."""
    sizes = [i.font_size for i in items if i.font_size]
    body = statistics.median(sizes) if sizes else None
    for item in items:
        text = item.text
        if len(text) > 120 or not any(c.isalpha() for c in text) or re.search(r"[,;]$", text):
            continue
        larger = body is not None and item.font_size is not None and item.font_size >= body * 1.15
        if item.bold or item.all_caps or larger:
            item.heading_level = 1
            item.heading_source = "heuristic"


def merge_across_pages(paras: list[Para]) -> list[Para]:
    """Абзац, который переходит на следующую страницу, склеивается: строка без точки в
    конце страницы и строка со строчной буквы в начале следующей."""
    out: list[Para] = []
    for para in paras:
        if out:
            prev = out[-1]
            last, first = prev.lines[-1], para.lines[0]
            if (
                first.page == last.page + 1
                and not _TERMINAL.search(last.text)
                and first.text[:1].islower()
                and abs(first.size - last.size) <= 0.15 * max(first.size, last.size)
                and first.bold == last.bold
            ):
                prev.lines.extend(para.lines)
                continue
        out.append(para)
    return out
