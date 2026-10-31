"""Стили DOCX: наследование по `basedOn` и свойства, нужные парсеру."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ml.extract.ooxml import W, int_attr, on_off, wattr

_HEADING_NAME = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
_HEADING_ID = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
_TOC_NAME = re.compile(r"^toc\s*\d$|^table of contents$", re.IGNORECASE)


@dataclass
class Style:
    style_id: str
    type: str
    name: str | None
    based_on: str | None
    ppr: etree._Element | None
    rpr: etree._Element | None
    tblpr: etree._Element | None


@dataclass
class Styles:
    styles: dict[str, Style] = field(default_factory=dict)
    default_paragraph: str | None = None
    default_table: str | None = None
    default_rpr: etree._Element | None = None

    @classmethod
    def parse(cls, root: etree._Element | None) -> Styles:
        result = cls()
        if root is None:
            return result
        defaults = root.find(W.docDefaults)
        if defaults is not None:
            result.default_rpr = defaults.find(f"{W.rPrDefault}/{W.rPr}")
        for el in root.iter(W.style):
            sid = el.get(W.styleId)
            if not sid:
                continue
            style = Style(
                style_id=sid,
                type=el.get(W.type, "paragraph"),
                name=wattr(el.find(W.name)),
                based_on=wattr(el.find(W.basedOn)),
                ppr=el.find(W.pPr),
                rpr=el.find(W.rPr),
                tblpr=el.find(W.tblPr),
            )
            result.styles[sid] = style
            if el.get(W.default) in ("1", "true", "on"):
                if style.type == "paragraph":
                    result.default_paragraph = sid
                elif style.type == "table":
                    result.default_table = sid
        return result

    def chain(self, style_id: str | None) -> list[Style]:
        """Стиль и его предки по `basedOn`, от ближнего к дальнему. Циклы обрываются."""
        out: list[Style] = []
        seen: set[str] = set()
        while style_id and style_id not in seen and style_id in self.styles:
            seen.add(style_id)
            style = self.styles[style_id]
            out.append(style)
            style_id = style.based_on
        return out

    def paragraph_chain(self, style_id: str | None) -> list[Style]:
        return self.chain(style_id or self.default_paragraph)

    def name(self, style_id: str | None) -> str | None:
        style = self.styles.get(style_id or "")
        return style.name if style else None

    def ppr_find(self, style_id: str | None, path: str) -> etree._Element | None:
        """Первый найденный по цепочке стилей элемент `pPr/<path>`."""
        for style in self.paragraph_chain(style_id):
            if style.ppr is not None:
                el = style.ppr.find(path)
                if el is not None:
                    return el
        return None

    def rpr_flag(self, style_ids: list[str | None], tag: str) -> bool | None:
        """Флаг формата (`w:b`, `w:caps`) из цепочек стилей и умолчаний документа."""
        for sid in style_ids:
            for style in self.chain(sid):
                if style.rpr is not None:
                    val = on_off(style.rpr.find(tag))
                    if val is not None:
                        return val
        if self.default_rpr is not None:
            return on_off(self.default_rpr.find(tag))
        return None

    def rpr_size(self, style_ids: list[str | None]) -> float | None:
        for sid in style_ids:
            for style in self.chain(sid):
                if style.rpr is not None:
                    sz = int_attr(style.rpr.find(W.sz))
                    if sz:
                        return sz / 2
        if self.default_rpr is not None:
            sz = int_attr(self.default_rpr.find(W.sz))
            if sz:
                return sz / 2
        return None

    def heading_level(self, style_id: str | None) -> tuple[int | None, str | None]:
        """Уровень заголовка по стилю: `outlineLvl` по цепочке, затем встроенные «heading N»."""
        for style in self.paragraph_chain(style_id):
            if style.ppr is not None:
                lvl = int_attr(style.ppr.find(W.outlineLvl))
                if lvl is not None:
                    return (lvl + 1, "outline") if 0 <= lvl <= 8 else (None, None)
            for candidate, pattern in ((style.name, _HEADING_NAME), (style.style_id, _HEADING_ID)):
                if candidate:
                    m = pattern.match(candidate.strip())
                    if m:
                        return int(m.group(1)), "style"
        return None, None

    def is_toc(self, style_id: str | None) -> bool:
        name = self.name(style_id)
        return bool(name and _TOC_NAME.match(name.strip()))
