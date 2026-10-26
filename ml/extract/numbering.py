"""Автонумерация DOCX по правилам Word (FR-3.1).

Номера пункта в XML абзаца нет: его рисует Word по `numbering.xml`. Здесь повторён
алгоритм Word:

1. Список абзаца — из `numPr` абзаца, иначе из стиля по цепочке `basedOn`, иначе по
   стилю, привязанному к уровню списка (`w:lvl/w:pStyle`).
2. Счётчики живут у абстрактного списка: несколько `w:num` с одним `abstractNumId`
   продолжают общий счёт.
3. `startOverride` начинает счёт заново при первом абзаце этого `w:num`. Новый номер
   уровня сбрасывает более глубокие уровни, если `lvlRestart` не задаёт иначе.
4. `numStyleLink` ведёт к настоящему списку через стиль нумерации; `numId = 0` —
   нумерации нет.
5. Номер собирается по `lvlText` с подстановками `%1…%9`; `isLgl` переводит
   вышестоящие уровни в арабские цифры; маркеры (`bullet`) номером не считаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ml.extract.docx_styles import Styles
from ml.extract.ooxml import W, int_attr, on_off, wattr

_RU_LOWER = "абвгдежзиклмнопрстуфхцчшщэюя"  # как у Word: без ё, й, ъ, ы, ь
_LATIN_LOWER = "abcdefghijklmnopqrstuvwxyz"
_PLACEHOLDER = re.compile(r"%([1-9])")

_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def to_roman(n: int) -> str:
    if n <= 0:
        return str(n)
    out = []
    for value, letters in _ROMAN:
        while n >= value:
            out.append(letters)
            n -= value
    return "".join(out)


def to_letters(n: int, alphabet: str) -> str:
    """Буквенный номер как у Word: a…z, затем aa…zz, aaa… (буква повторяется)."""
    if n <= 0:
        return str(n)
    size = len(alphabet)
    return alphabet[(n - 1) % size] * ((n - 1) // size + 1)


def format_number(n: int, fmt: str | None) -> str:
    fmt = fmt or "decimal"
    if fmt in ("decimal", "decimalHalfWidth", "decimalFullWidth"):
        return str(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "upperRoman":
        return to_roman(n)
    if fmt == "lowerRoman":
        return to_roman(n).lower()
    if fmt == "upperLetter":
        return to_letters(n, _LATIN_LOWER).upper()
    if fmt == "lowerLetter":
        return to_letters(n, _LATIN_LOWER)
    if fmt == "russianLower":
        return to_letters(n, _RU_LOWER)
    if fmt == "russianUpper":
        return to_letters(n, _RU_LOWER).upper()
    if fmt == "none":
        return ""
    if fmt == "decimalEnclosedParen":
        return f"({n})"
    if fmt == "decimalEnclosedFullstop":
        return f"{n}."
    return str(n)


@dataclass
class Level:
    ilvl: int
    start: int = 1
    num_fmt: str = "decimal"
    lvl_text: str | None = None
    restart: int | None = None  # lvlRestart: 0 — не сбрасывать; n — сбрасывать после уровня n (с 1)
    is_lgl: bool = False
    pstyle: str | None = None
    ppr: etree._Element | None = None

    @classmethod
    def parse(cls, el: etree._Element) -> Level:
        ilvl = int(el.get(W.ilvl, "0"))
        start = int_attr(el.find(W.start))
        return cls(
            ilvl=ilvl,
            start=1 if start is None else start,
            num_fmt=wattr(el.find(W.numFmt)) or "decimal",
            lvl_text=wattr(el.find(W.lvlText)),
            restart=int_attr(el.find(W.lvlRestart)),
            is_lgl=bool(on_off(el.find(W.isLgl))),
            pstyle=wattr(el.find(W.pStyle)),
            ppr=el.find(W.pPr),
        )


@dataclass
class AbstractNum:
    abstract_id: str
    levels: dict[int, Level] = field(default_factory=dict)
    num_style_link: str | None = None
    style_link: str | None = None


@dataclass
class Num:
    num_id: str
    abstract_id: str
    start_overrides: dict[int, int] = field(default_factory=dict)
    level_overrides: dict[int, Level] = field(default_factory=dict)


@dataclass
class NumberResult:
    number: str | None
    ilvl: int
    is_bullet: bool
    num_id: str
    abstract_id: str
    level: Level | None


class Numbering:
    """Определения списков и счётчики. Абзацы подаются строго в порядке документа."""

    def __init__(self, root: etree._Element | None, styles: Styles):
        self.styles = styles
        self.abstracts: dict[str, AbstractNum] = {}
        self.nums: dict[str, Num] = {}
        self._counters: dict[str, list[int | None]] = {}
        self._used_nums: set[str] = set()
        if root is not None:
            self._parse(root)
        self._pstyle_links = self._build_pstyle_links()

    def _parse(self, root: etree._Element) -> None:
        for el in root.iter(W.abstractNum):
            aid = el.get(W.abstractNumId)
            if aid is None:
                continue
            abstract = AbstractNum(
                abstract_id=aid,
                num_style_link=wattr(el.find(W.numStyleLink)),
                style_link=wattr(el.find(W.styleLink)),
            )
            for lvl_el in el.findall(W.lvl):
                level = Level.parse(lvl_el)
                abstract.levels[level.ilvl] = level
            self.abstracts[aid] = abstract
        for el in root.iter(W.num):
            nid = el.get(W.numId)
            aid = wattr(el.find(W.abstractNumId))
            if nid is None or aid is None:
                continue
            num = Num(num_id=nid, abstract_id=aid)
            for ov in el.findall(W.lvlOverride):
                ilvl = int(ov.get(W.ilvl, "0"))
                start = int_attr(ov.find(W.startOverride))
                if start is not None:
                    num.start_overrides[ilvl] = start
                lvl_el = ov.find(W.lvl)
                if lvl_el is not None:
                    level = Level.parse(lvl_el)
                    level.ilvl = ilvl
                    num.level_overrides[ilvl] = level
            self.nums[nid] = num

    def _build_pstyle_links(self) -> dict[str, tuple[str, int]]:
        """Стиль абзаца → (numId, ilvl) по `w:lvl/w:pStyle` абстрактного списка."""
        links: dict[str, tuple[str, int]] = {}
        num_by_abstract: dict[str, str] = {}
        for nid in sorted(self.nums, key=_num_sort_key):
            num_by_abstract.setdefault(self.nums[nid].abstract_id, nid)
        for aid, abstract in self.abstracts.items():
            nid = num_by_abstract.get(aid)
            if nid is None:
                continue
            for level in abstract.levels.values():
                if level.pstyle:
                    links.setdefault(level.pstyle, (nid, level.ilvl))
        return links

    def _resolve_abstract(self, num: Num) -> AbstractNum | None:
        """Абстрактный список с учётом `numStyleLink` → стиль нумерации → его `numId`."""
        abstract = self.abstracts.get(num.abstract_id)
        seen: set[str] = set()
        while abstract is not None and abstract.num_style_link and abstract.abstract_id not in seen:
            seen.add(abstract.abstract_id)
            numpr = self.styles.ppr_find(abstract.num_style_link, W.numPr)
            linked_id = wattr(numpr.find(W.numId)) if numpr is not None else None
            linked = self.nums.get(linked_id or "")
            if linked is None:
                break
            abstract = self.abstracts.get(linked.abstract_id)
        return abstract

    def paragraph_num(self, ppr: etree._Element | None, style_id: str | None) -> tuple[str, int] | None:
        """(numId, ilvl) абзаца или None, если абзац не в списке."""
        num_id: str | None = None
        ilvl: int | None = None
        numpr = ppr.find(W.numPr) if ppr is not None else None
        if numpr is not None:
            num_id = wattr(numpr.find(W.numId))
            ilvl = int_attr(numpr.find(W.ilvl))
        if num_id is None or ilvl is None:
            style_numpr = self.styles.ppr_find(style_id, W.numPr)
            if style_numpr is not None:
                if num_id is None:
                    num_id = wattr(style_numpr.find(W.numId))
                if ilvl is None:
                    ilvl = int_attr(style_numpr.find(W.ilvl))
        if num_id is None and style_id:
            for style in self.styles.paragraph_chain(style_id):
                link = self._pstyle_links.get(style.style_id)
                if link:
                    num_id, link_ilvl = link
                    ilvl = link_ilvl if ilvl is None else ilvl
                    break
        if num_id is None or num_id == "0" or num_id not in self.nums:
            return None
        if ilvl is None:
            ilvl = self._ilvl_for_style(num_id, style_id)
        return num_id, max(0, min(8, ilvl))

    def _ilvl_for_style(self, num_id: str, style_id: str | None) -> int:
        abstract = self._resolve_abstract(self.nums[num_id])
        if abstract and style_id:
            for level in abstract.levels.values():
                if level.pstyle == style_id:
                    return level.ilvl
        return 0

    def level(self, num_id: str, ilvl: int) -> Level | None:
        num = self.nums.get(num_id)
        if num is None:
            return None
        if ilvl in num.level_overrides:
            return num.level_overrides[ilvl]
        abstract = self._resolve_abstract(num)
        return abstract.levels.get(ilvl) if abstract else None

    def _start(self, num: Num, ilvl: int) -> int:
        if ilvl in num.start_overrides:
            return num.start_overrides[ilvl]
        level = self.level(num.num_id, ilvl)
        return level.start if level else 1

    def next_number(self, num_id: str, ilvl: int) -> NumberResult | None:
        """Продвигает счётчики и возвращает номер абзаца в списке (numId, ilvl)."""
        num = self.nums.get(num_id)
        if num is None:
            return None
        abstract = self._resolve_abstract(num)
        if abstract is None:
            return None
        aid = abstract.abstract_id
        counters = self._counters.setdefault(aid, [None] * 9)

        if num_id not in self._used_nums:
            self._used_nums.add(num_id)
            for ov_ilvl in num.start_overrides:
                if 0 <= ov_ilvl <= 8:
                    counters[ov_ilvl] = None

        level = self.level(num_id, ilvl)
        counters[ilvl] = self._start(num, ilvl) if counters[ilvl] is None else counters[ilvl] + 1
        for deeper in range(ilvl + 1, 9):
            deeper_level = self.level(num_id, deeper)
            restart = deeper_level.restart if deeper_level else None
            if restart == 0:
                continue
            # По умолчанию уровень сбрасывается после любого вышестоящего;
            # lvlRestart = n — только после уровней 1…n (номера уровней с 1)
            if restart is None or ilvl < restart:
                counters[deeper] = None

        if level is None:
            return NumberResult(str(counters[ilvl]), ilvl, False, num_id, aid, None)
        if level.num_fmt == "bullet":
            return NumberResult(None, ilvl, True, num_id, aid, level)
        text = self._render(num, ilvl, level, counters)
        return NumberResult(text or None, ilvl, False, num_id, aid, level)

    def _render(self, num: Num, ilvl: int, level: Level, counters: list[int | None]) -> str:
        template = level.lvl_text
        if template is None:
            template = f"%{ilvl + 1}."

        def repl(m: re.Match[str]) -> str:
            ref = int(m.group(1)) - 1
            value = counters[ref]
            if value is None:
                value = self._start(num, ref)
            ref_level = self.level(num.num_id, ref)
            fmt = ref_level.num_fmt if ref_level else "decimal"
            if level.is_lgl and ref < ilvl:
                fmt = "decimal"
            if fmt == "bullet":
                return ""
            return format_number(value, fmt)

        return _PLACEHOLDER.sub(repl, template).strip()


def _num_sort_key(num_id: str) -> tuple[int, str]:
    try:
        return int(num_id), num_id
    except ValueError:
        return 1 << 30, num_id
