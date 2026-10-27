"""Чтение DOCX (часть FR-3.1): сырые абзацы с автонумерацией, таблицами, страницами и картой runs.

Тело документа обходится по XML в порядке следования: абзацы, таблицы, элементы
управления содержимым (`w:sdt`). Текст собирается из `w:t`, `w:tab`, `w:br`,
`w:noBreakHyphen`, `w:sym`, ссылок и результатов полей. Документ берётся таким, каким
он станет после принятия правок: `w:ins` читается, `w:del` пропускается. Надписи и
формулы отдаются отдельными абзацами, колонтитулы не читаются, сноски — в конце.
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from contracts.extraction import (
    DocumentKind,
    DocxRunSpan,
    ExtractionResult,
    PageInfo,
    RawParagraph,
    SourceMap,
    TableCellRef,
)
from ml.config import PIPELINE_VERSION
from ml.errors import CorruptedFileError
from ml.extract.docx_styles import Styles
from ml.extract.numbering import Numbering
from ml.extract.ooxml import MC, W, int_attr, local, on_off, wattr
from ml.extract.text import clean_text, drop_prefix, split_clause_number, strip_spans

log = logging.getLogger(__name__)

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_comments=True)
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
_NUMBERING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
_FOOTNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"

_INLINE_CONTAINERS = {
    W.hyperlink, W.smartTag, W.customXml, W.ins, W.moveTo, W.fldSimple, W.dir, W.bdo, W.sdtContent,
}
_SKIPPED_INLINE = {W("del"), W.moveFrom}
_ALIGN = {
    "left": "left", "start": "left", "center": "center", "right": "right", "end": "right",
    "both": "justify", "distribute": "justify", "thaiDistribute": "justify", "lowKashida": "justify",
    "mediumKashida": "justify", "highKashida": "justify",
}
# Частые знаки шрифта Symbol, которые Word пишет через w:sym (код F0xx)
_SYMBOL_FONT = {
    0xA3: "≤", 0xB3: "≥", 0xB4: "×", 0xB1: "±", 0xBB: "≈", 0xB0: "°", 0xB9: "≠",
    0xB7: "•", 0xD7: "·", 0xAE: "→", 0xAC: "←", 0xA5: "∞", 0x2D: "−", 0xBE: "—",
}
_TERMINAL = re.compile(r"[,;]\s*$")

MAX_PART_BYTES = 300 * 1024 * 1024
"""Предел распакованного размера части DOCX: защита от «zip-бомбы» внутри файла."""

CHARS_PER_PAGE = 1800
"""Оценка числа знаков на странице для DOCX без меток страниц (14 кегль, поля ГОСТ)."""
PARAGRAPH_OVERHEAD = 40
LABEL_MAX_CHARS = 200
"""Подпись поля в таблице «подпись — значение» — не длиннее одного-двух предложений."""
CELL_PAGE_CHARS = 500
"""Меньше этого текста ячейка на страницу не уходит: более близкая метка в той же ячейке — повтор."""


@dataclass
class _Para:
    """Абзац `w:p` после чтения, до сборки в RawParagraph."""

    text: str
    spans: list[DocxRunSpan]
    number: str | None
    list_level: int | None
    is_bullet: bool
    heading_level: int | None
    heading_source: str | None
    style_id: str | None
    style_name: str | None
    bold: bool
    all_caps: bool
    align: str | None
    indent: float | None
    font_size: float | None
    page: int
    page_end: int
    textboxes: list[etree._Element] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)


@dataclass
class _RowPages:
    """Строка таблицы при чтении страниц по меткам `lastRenderedPageBreak`.

    Word ставит метку в каждой ячейке строки, которая переходит на следующую страницу, иногда
    две в одной ячейке и ещё одну в начале следующей строки. Переход один: ячейки строки
    начинают со страницы строки, строка кончается на самой дальней из них.
    """

    start_chars: int
    after_break: bool
    """Предыдущая строка перешла на новую страницу: метка до первого текста этой строки — её повтор."""
    break_at: int | None = None


@dataclass
class _Cell:
    """Ячейка строки таблицы после чтения, до вывода блоков."""

    col: int
    span: int
    vmerge: etree._Element | None
    paras: list[_Para]
    nested: list[etree._Element]
    page: int


@dataclass
class _Ctx:
    """Состояние чтения одного абзаца."""

    paragraph_index: int
    run_index: dict[etree._Element, int]
    para_style: str | None
    pieces: list[str] = field(default_factory=list)
    spans: list[DocxRunSpan] = field(default_factory=list)
    pos: int = 0
    fields: list[str] = field(default_factory=list)
    chars: int = 0
    bold_chars: int = 0
    caps_chars: int = 0
    size_weight: dict[float, int] = field(default_factory=dict)
    start_page: int | None = None
    textboxes: list[etree._Element] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)


class DocxReader:
    def __init__(self, path: str | Path, kind: DocumentKind = "docx", source_pages: int | None = None):
        self.path = Path(path)
        self.kind = kind
        try:
            self.zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise CorruptedFileError(detail=f"docx: {exc}") from exc
        self.main_part = self._main_part_name()
        rels = self._rels(self.main_part)
        self.document = self._xml(self.main_part)
        if self.document is None:
            raise CorruptedFileError(detail="docx: нет word/document.xml")
        self.body = self.document.find(W.body)
        if self.body is None:
            raise CorruptedFileError(detail="docx: нет w:body")
        self.styles = Styles.parse(self._xml(rels.get(_STYLES)))
        self.numbering = Numbering(self._xml(rels.get(_NUMBERING)), self.styles)
        self.footnotes = self._xml(rels.get(_FOOTNOTES))
        # Число страниц, которое показал Word: из `docProps/app.xml`, у сконвертированного DOC — из его свойств
        self.app_pages = self._app_pages() or source_pages

        self.items: list[RawParagraph] = []
        self.page = 1
        self.use_rendered = self.body.find(f".//{W.lastRenderedPageBreak}") is not None
        self.chars_since_break = 0
        self.text_chars = 0
        self.row: _RowPages | None = None
        self.table_counter = 0
        self.footnote_pages: dict[str, int] = {}
        self.para_index = {
            "document": {p: i for i, p in enumerate(self.document.iter(W.p))},
            "footnotes": (
                {p: i for i, p in enumerate(self.footnotes.iter(W.p))} if self.footnotes is not None else {}
            ),
        }
        self.part = "document"
        self.fixed_page: int | None = None

    # --- пакет --------------------------------------------------------------------------

    def _xml(self, name: str | None) -> etree._Element | None:
        if not name:
            return None
        try:
            if self.zip.getinfo(name).file_size > MAX_PART_BYTES:
                raise CorruptedFileError("Файл DOCX слишком большой для разбора", detail=f"{name} > {MAX_PART_BYTES}")
            data = self.zip.read(name)
        except KeyError:
            return None
        except (zipfile.BadZipFile, OSError) as exc:
            raise CorruptedFileError(detail=f"docx: {name}: {exc}") from exc
        try:
            return etree.fromstring(data, _PARSER)
        except etree.XMLSyntaxError as exc:
            raise CorruptedFileError(detail=f"docx: {name}: {exc}") from exc

    def _main_part_name(self) -> str:
        rels = self._xml("_rels/.rels")
        if rels is not None:
            for rel in rels.iter(f"{{{_REL_NS}}}Relationship"):
                if rel.get("Type") == _OFFICE_DOC:
                    return rel.get("Target", "word/document.xml").lstrip("/")
        return "word/document.xml"

    def _rels(self, part: str) -> dict[str, str]:
        folder, name = posixpath.split(part)
        rels = self._xml(posixpath.join(folder, "_rels", f"{name}.rels"))
        out: dict[str, str] = {}
        if rels is None:
            return out
        for rel in rels.iter(f"{{{_REL_NS}}}Relationship"):
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External":
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            out.setdefault(rel.get("Type", ""), path)
        return out

    def _app_pages(self) -> int | None:
        app = self._xml("docProps/app.xml")
        if app is None:
            return None
        for el in app:
            if local(el.tag) == "Pages" and el.text and el.text.strip().isdigit():
                return int(el.text.strip()) or None
        return None

    # --- страницы -----------------------------------------------------------------------

    @property
    def current_page(self) -> int:
        if self.fixed_page is not None:
            return self.fixed_page
        if self.use_rendered:
            return self.page
        return self.page + self.chars_since_break // CHARS_PER_PAGE

    def _explicit_break(self) -> None:
        if self.use_rendered or self.fixed_page is not None:
            return
        self.page = self.current_page + 1
        self.chars_since_break = 0

    def _rendered_break(self) -> None:
        if not self.use_rendered or self.fixed_page is not None:
            return
        row = self.row
        if row is not None:
            if row.after_break and self.text_chars == row.start_chars:
                return
            if row.break_at is not None and self.text_chars - row.break_at < CELL_PAGE_CHARS:
                return
            row.break_at = self.text_chars
        self.page += 1

    # --- обход --------------------------------------------------------------------------

    def read(self) -> ExtractionResult:
        self.walk_blocks(self.body, "body")
        body_pages = self.current_page
        if self.footnotes is not None:
            self._read_footnotes()
        pages_estimated = not self.use_rendered
        if pages_estimated or self._marks_incomplete(body_pages):
            pages = self._calibrate_pages(body_pages)
        else:
            pages = max(body_pages, 1)
        for i, item in enumerate(self.items):
            item.order = i
        width, height = self._page_size()
        page_info = [PageInfo(page=n, width=width, height=height) for n in range(1, pages + 1)]
        return ExtractionResult(
            kind=self.kind,
            format="docx",
            pages=pages,
            pages_estimated=pages_estimated,
            paragraphs=self.items,
            page_info=page_info,
            pipeline_version=PIPELINE_VERSION,
        )

    def _page_size(self) -> tuple[float, float]:
        """Размер страницы в пунктах из последнего `w:sectPr`; по умолчанию A4."""
        sect = self.body.find(W.sectPr)
        size = sect.find(W.pgSz) if sect is not None else None
        width = int_attr(size, "w") if size is not None else None
        height = int_attr(size, "h") if size is not None else None
        return (width / 20 if width else 595.3, height / 20 if height else 841.9)

    def _marks_incomplete(self, counted: int) -> bool:
        """Меток Word заметно меньше или больше, чем страниц в `app.xml`: Word пишет их не везде."""
        target = self.app_pages
        return bool(target) and abs(target - counted) > max(1, target // 10)

    def _calibrate_pages(self, estimated_total: int) -> int:
        """Подгоняет оценку страниц под `docProps/app.xml`, если Word записал число страниц."""
        target = self.app_pages
        if not target or estimated_total <= 1 or target == estimated_total:
            return max(target or estimated_total, 1)
        scale = (target - 1) / (estimated_total - 1)
        for item in self.items:
            item.page = min(target, 1 + round((item.page - 1) * scale))
            if item.page_end is not None:
                item.page_end = min(target, 1 + round((item.page_end - 1) * scale))
                if item.page_end <= item.page:
                    item.page_end = None
        return target

    def walk_blocks(self, container: etree._Element, stream: str, parent_table: int | None = None) -> None:
        for child in container:
            tag = child.tag
            if tag == W.p:
                para = self.read_paragraph(child)
                if para is not None:
                    self._emit(para, stream)
                    self._emit_children(para)
            elif tag == W.tbl:
                self.walk_table(child, stream, parent_table)
            elif tag == W.sdt:
                content = child.find(W.sdtContent)
                if content is not None:
                    self.walk_blocks(content, stream, parent_table)
            elif tag in (W.customXml, W.ins, W.moveTo):
                self.walk_blocks(child, stream, parent_table)
            elif tag == MC.AlternateContent:
                branch = _choose_branch(child)
                if branch is not None:
                    self.walk_blocks(branch, stream, parent_table)

    def _emit_children(self, para: _Para) -> None:
        for box in para.textboxes:
            self.walk_blocks(box, "textbox")
        for formula in para.formulas:
            text = clean_text(formula).strip()
            if text:
                self.items.append(
                    RawParagraph(
                        order=len(self.items),
                        text=text,
                        stream="formula",
                        page=para.page,
                        page_estimated=not self.use_rendered,
                        source_map=SourceMap(format="docx", part=self.part),
                    )
                )

    def _emit(self, para: _Para, stream: str, table: TableCellRef | None = None) -> RawParagraph:
        heading_level, heading_source = para.heading_level, para.heading_source
        if heading_level is None and table is None and stream == "body":
            heading_level, heading_source = _heading_heuristic(para)
        text, spans, number = para.text, para.spans, para.number
        number_source = "numbering" if number else None
        if number is None and table is None and stream == "body":
            # Номер набран вручную («6.4. За каждый факт…»): выделяется по тому же шаблону, что в PDF
            found = split_clause_number(text)
            if found is not None:
                number, number_source = found[0], "pattern"
                text, spans = drop_prefix(text, spans, found[1])
        item = RawParagraph(
            order=len(self.items),
            text=text,
            number=number,
            number_source=number_source,
            list_level=para.list_level,
            is_bullet=para.is_bullet,
            heading_level=heading_level,
            heading_source=heading_source,
            style_id=para.style_id,
            style_name=para.style_name,
            bold=para.bold,
            all_caps=para.all_caps,
            align=para.align,
            indent=para.indent,
            font_size=para.font_size,
            stream=stream,
            table=table,
            page=para.page,
            page_end=para.page_end if para.page_end > para.page else None,
            page_estimated=not self.use_rendered,
            source_map=SourceMap(format="docx", part=self.part, runs=spans),
        )
        self.items.append(item)
        return item

    # --- таблицы ------------------------------------------------------------------------

    def walk_table(self, tbl: etree._Element, stream: str, parent_table: int | None) -> None:
        table_id = self.table_counter
        self.table_counter += 1
        is_layout = self._is_layout_table(tbl)
        open_merges: dict[int, RawParagraph] = {}
        outer = self.row
        after_break = False
        rendered = self.use_rendered and self.fixed_page is None
        for row_idx, tr in enumerate(_iter_rows(tbl)):
            trpr = tr.find(W.trPr)
            col = (int_attr(trpr.find(W.gridBefore)) or 0) if trpr is not None else 0
            row_page = row_end = self.page
            row = _RowPages(start_chars=self.text_chars, after_break=after_break)
            cells: list[_Cell] = []
            for tc in _iter_cells(tr):
                self.row = row
                row.break_at = None
                if rendered:
                    self.page = row_page
                tcpr = tc.find(W.tcPr)
                span = (int_attr(tcpr.find(W.gridSpan)) if tcpr is not None else None) or 1
                vmerge = tcpr.find(W.vMerge) if tcpr is not None else None
                paras, nested = self._read_cell(tc)
                cells.append(_Cell(col, span, vmerge, [p for p in paras if p.text], nested, self.page))
                col += span
            pair = _label_value(cells)
            for cell in cells:
                self.page = cell.page if rendered else self.page
                ref = TableCellRef(
                    table_id=table_id,
                    row=row_idx,
                    col=cell.col,
                    col_span=cell.span,
                    is_layout=is_layout,
                    parent_table_id=parent_table,
                )
                continues = cell.vmerge is not None and wattr(cell.vmerge) in (None, "continue")
                if pair is not None and cell is pair[1]:
                    pass
                elif pair is not None and cell is pair[0]:
                    # Подпись и значение в соседних ячейках — один блок: цитата и поиск видят значение с подписью
                    ref.col_span = pair[1].col + pair[1].span - cell.col
                    self._emit(_merge_label_value(cell.paras[0], _merge_cell(pair[1].paras)), stream, ref)
                    open_merges.pop(cell.col, None)
                elif continues and cell.col in open_merges and not cell.paras:
                    open_merges[cell.col].table.row_span += 1
                elif cell.paras:
                    item = self._emit(_merge_cell(cell.paras), stream, ref)
                    if cell.vmerge is not None:
                        open_merges[cell.col] = item
                    else:
                        open_merges.pop(cell.col, None)
                else:
                    open_merges.pop(cell.col, None)
                for para in cell.paras:
                    self._emit_children(para)
                for inner in cell.nested:
                    self.walk_table(inner, stream, table_id)
                row_end = max(row_end, self.page)
            if rendered:
                self.page = row_end
            after_break = row_end > row_page
        self.row = outer

    def _read_cell(self, tc: etree._Element) -> tuple[list[_Para], list[etree._Element]]:
        paras: list[_Para] = []
        nested: list[etree._Element] = []

        def walk(container: etree._Element) -> None:
            for child in container:
                if child.tag == W.p:
                    para = self.read_paragraph(child)
                    if para is not None:
                        paras.append(para)
                elif child.tag == W.tbl:
                    nested.append(child)
                elif child.tag == W.sdt:
                    content = child.find(W.sdtContent)
                    if content is not None:
                        walk(content)
                elif child.tag in (W.customXml, W.ins, W.moveTo):
                    walk(child)

        walk(tc)
        return paras, nested

    def _is_layout_table(self, tbl: etree._Element) -> bool:
        """Таблица без видимых границ: раскладка реквизитов и подписей."""
        tblpr = tbl.find(W.tblPr)
        borders = tblpr.find(W.tblBorders) if tblpr is not None else None
        if borders is None:
            style_id = wattr(tblpr.find(W.tblStyle)) if tblpr is not None else None
            for style in self.styles.chain(style_id or self.styles.default_table):
                if style.tblpr is not None and style.tblpr.find(W.tblBorders) is not None:
                    borders = style.tblpr.find(W.tblBorders)
                    break
        if borders is not None and _has_visible_border(borders):
            return False
        for tcb in tbl.iter(W.tcBorders):
            if _has_visible_border(tcb):
                return False
        return True

    # --- сноски -------------------------------------------------------------------------

    def _read_footnotes(self) -> None:
        self.part = "footnotes"
        numbering, self.numbering = self.numbering, Numbering(None, self.styles)
        try:
            for fn in self.footnotes.findall(W.footnote):
                if fn.get(W.type) in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                self.fixed_page = self.footnote_pages.get(fn.get(W.id, ""), self.current_page)
                self.walk_blocks(fn, "footnote")
        finally:
            self.fixed_page = None
            self.numbering = numbering
            self.part = "document"

    # --- абзац --------------------------------------------------------------------------

    def read_paragraph(self, p: etree._Element) -> _Para | None:
        ppr = p.find(W.pPr)
        style_ref = wattr(ppr.find(W.pStyle)) if ppr is not None else None
        style_id = style_ref or self.styles.default_paragraph

        break_before = on_off(ppr.find(W.pageBreakBefore)) if ppr is not None else None
        if break_before is None:
            break_before = on_off(self.styles.ppr_find(style_id, W.pageBreakBefore))
        if break_before and self.items:
            self._explicit_break()

        number = None
        list_level = None
        is_bullet = False
        level_ppr = None
        num = self.numbering.paragraph_num(ppr, style_id)
        if num is not None:
            result = self.numbering.next_number(*num)
            if result is not None:
                number, list_level, is_bullet = result.number, result.ilvl, result.is_bullet
                level_ppr = result.level.ppr if result.level else None

        heading_level, heading_source = None, None
        outline = int_attr(ppr.find(W.outlineLvl)) if ppr is not None else None
        if outline is not None:
            if 0 <= outline <= 8:
                heading_level, heading_source = outline + 1, "outline"
        else:
            heading_level, heading_source = self.styles.heading_level(style_id)

        ctx = _Ctx(
            paragraph_index=self.para_index[self.part].get(p, -1),
            run_index={r: i for i, r in enumerate(p.iter(W.r))},
            para_style=style_id,
        )
        self._walk_inline(p, ctx)

        raw = "".join(ctx.pieces)
        text, spans = strip_spans(clean_text(raw), ctx.spans)
        page = ctx.start_page or self.current_page
        page_end = self.current_page

        if not self.use_rendered and self.fixed_page is None:
            self.chars_since_break += len(text) + PARAGRAPH_OVERHEAD
            sect = ppr.find(W.sectPr) if ppr is not None else None
            if sect is not None and wattr(sect.find(W.type)) not in ("continuous",):
                self._explicit_break()

        if self.styles.is_toc(style_id):
            if text:
                log.debug("docx: пропущена строка оглавления: %.60s", text)
            return None
        if not text and not ctx.textboxes and not ctx.formulas:
            return None

        letters = [c for c in text if c.isalpha()]
        all_caps = (len(letters) >= 3 and all(c.isupper() for c in letters)) or (
            ctx.chars > 0 and ctx.caps_chars == ctx.chars
        )
        font_size = max(ctx.size_weight, key=ctx.size_weight.get) if ctx.size_weight else None
        return _Para(
            text=text,
            spans=spans,
            number=number,
            list_level=list_level,
            is_bullet=is_bullet,
            heading_level=heading_level,
            heading_source=heading_source,
            style_id=style_ref,
            style_name=self.styles.name(style_id),
            bold=ctx.chars > 0 and ctx.bold_chars == ctx.chars,
            all_caps=all_caps,
            align=self._align(ppr, style_id),
            indent=self._indent(ppr, level_ppr, style_id),
            font_size=font_size,
            page=page,
            page_end=page_end,
            textboxes=ctx.textboxes,
            formulas=ctx.formulas,
        )

    def _align(self, ppr: etree._Element | None, style_id: str | None) -> str | None:
        jc = ppr.find(W.jc) if ppr is not None else None
        if jc is None:
            jc = self.styles.ppr_find(style_id, W.jc)
        return _ALIGN.get(wattr(jc) or "") if jc is not None else None

    def _indent(
        self, ppr: etree._Element | None, level_ppr: etree._Element | None, style_id: str | None
    ) -> float | None:
        for source in (ppr, level_ppr):
            ind = source.find(W.ind) if source is not None else None
            if ind is not None:
                value = int_attr(ind, "left") if ind.get(W.left) is not None else int_attr(ind, "start")
                if value is not None:
                    return value / 20
        ind = self.styles.ppr_find(style_id, W.ind)
        if ind is not None:
            value = int_attr(ind, "left") if ind.get(W.left) is not None else int_attr(ind, "start")
            if value is not None:
                return value / 20
        return None

    def _walk_inline(self, el: etree._Element, ctx: _Ctx) -> None:
        for child in el:
            tag = child.tag
            if tag == W.r:
                self._read_run(child, ctx)
            elif tag in _INLINE_CONTAINERS:
                self._walk_inline(child, ctx)
            elif tag == W.sdt:
                content = child.find(W.sdtContent)
                if content is not None:
                    self._walk_inline(content, ctx)
            elif tag in _SKIPPED_INLINE:
                continue
            elif tag == MC.AlternateContent:
                branch = _choose_branch(child)
                if branch is not None:
                    self._walk_inline(branch, ctx)
            elif local(tag) in ("oMath", "oMathPara"):
                formula = "".join(t.text or "" for t in child.iter() if local(t.tag) == "t")
                if formula.strip():
                    ctx.formulas.append(formula)

    def _read_run(self, r: etree._Element, ctx: _Ctx) -> None:
        rpr = r.find(W.rPr)
        hidden = rpr is not None and bool(on_off(rpr.find(W.vanish)))
        rstyle = wattr(rpr.find(W.rStyle)) if rpr is not None else None
        styles = [rstyle, ctx.para_style]
        bold = on_off(rpr.find(W.b)) if rpr is not None else None
        if bold is None:
            bold = bool(self.styles.rpr_flag(styles, W.b))
        caps = on_off(rpr.find(W.caps)) if rpr is not None else None
        if caps is None:
            caps = bool(self.styles.rpr_flag(styles, W.caps))
        size = None
        if rpr is not None:
            sz = int_attr(rpr.find(W.sz))
            size = sz / 2 if sz else None
        if size is None:
            size = self.styles.rpr_size(styles)
        run_idx = ctx.run_index.get(r, -1)
        run_offset = 0

        def emit(s: str, with_span: bool = True) -> None:
            if hidden or "instr" in ctx.fields:
                return
            if ctx.start_page is None and s.strip():
                ctx.start_page = self.current_page
            start = ctx.pos
            self.text_chars += len(s)
            ctx.pieces.append(s)
            ctx.pos += len(s)
            visible = len(s) - sum(1 for c in s if c.isspace())
            ctx.chars += visible
            if bold:
                ctx.bold_chars += visible
            if caps:
                ctx.caps_chars += visible
            if size and visible:
                ctx.size_weight[size] = ctx.size_weight.get(size, 0) + visible
            if not with_span or run_idx < 0:
                return
            last = ctx.spans[-1] if ctx.spans else None
            if (
                last is not None
                and last.run_index == run_idx
                and last.paragraph_index == ctx.paragraph_index
                and last.end == start
                and last.run_offset + (last.end - last.start) == run_offset
            ):
                last.end = ctx.pos
            else:
                ctx.spans.append(
                    DocxRunSpan(
                        start=start,
                        end=ctx.pos,
                        paragraph_index=ctx.paragraph_index,
                        run_index=run_idx,
                        run_offset=run_offset,
                    )
                )

        for child in r:
            tag = child.tag
            if tag == W.t:
                s = child.text or ""
                emit(s)
                run_offset += len(s)
            elif tag in (W.tab, W.ptab):
                emit("\t")
                run_offset += 1
            elif tag == W.br:
                br_type = child.get(W.type)
                if br_type in (None, "textWrapping"):
                    emit("\n")
                    run_offset += 1
                elif br_type == "page":
                    self._explicit_break()
            elif tag == W.cr:
                emit("\n")
                run_offset += 1
            elif tag == W.noBreakHyphen:
                emit("-")
                run_offset += 1
            elif tag == W.sym:
                ch = _sym_char(child)
                if ch:
                    emit(ch, with_span=False)
            elif tag == W.lastRenderedPageBreak:
                self._rendered_break()
            elif tag == W.fldChar:
                kind = child.get(W.fldCharType)
                if kind == "begin":
                    ctx.fields.append("instr")
                elif kind == "separate" and ctx.fields:
                    ctx.fields[-1] = "result"
                elif kind == "end" and ctx.fields:
                    ctx.fields.pop()
            elif tag == W.footnoteReference:
                fid = child.get(W.id)
                if fid is not None:
                    self.footnote_pages.setdefault(fid, self.current_page)
            elif tag in (W.drawing, W.pict, W.object):
                ctx.textboxes.extend(_top_textboxes(child))
            elif tag == MC.AlternateContent:
                branch = _choose_branch(child)
                if branch is not None:
                    ctx.textboxes.extend(_top_textboxes(branch))


def _choose_branch(alt: etree._Element) -> etree._Element | None:
    """Ветка `mc:AlternateContent`: первая `mc:Choice`, иначе `mc:Fallback`. Обе сразу не читаются."""
    choice = alt.find(MC.Choice)
    if choice is not None:
        return choice
    return alt.find(MC.Fallback)


def _top_textboxes(el: etree._Element) -> list[etree._Element]:
    """Надписи `w:txbxContent` внутри элемента, без вложенных в другие надписи."""
    out = []
    for box in el.iter(W.txbxContent):
        parent = box.getparent()
        nested = False
        while parent is not None and parent is not el:
            if parent.tag == W.txbxContent:
                nested = True
                break
            parent = parent.getparent()
        if not nested:
            out.append(box)
    return out


def _sym_char(el: etree._Element) -> str:
    code = el.get(W.char)
    if not code:
        return ""
    try:
        value = int(code, 16)
    except ValueError:
        return ""
    if 0xF000 <= value <= 0xF0FF:
        return _SYMBOL_FONT.get(value - 0xF000, "")
    return chr(value) if value < 0x10000 else "�"


def _iter_rows(tbl: etree._Element):
    for child in tbl:
        if child.tag == W.tr:
            yield child
        elif child.tag in (W.sdt, W.customXml):
            content = child.find(W.sdtContent) if child.tag == W.sdt else child
            if content is not None:
                yield from (tr for tr in content if tr.tag == W.tr)


def _iter_cells(tr: etree._Element):
    for child in tr:
        if child.tag == W.tc:
            yield child
        elif child.tag in (W.sdt, W.customXml):
            content = child.find(W.sdtContent) if child.tag == W.sdt else child
            if content is not None:
                yield from (tc for tc in content if tc.tag == W.tc)


def _has_visible_border(borders: etree._Element) -> bool:
    for side in borders:
        val = side.get(W.val)
        if val and val not in ("nil", "none"):
            return True
    return False


def _merge_cell(paras: list[_Para]) -> _Para:
    """Абзацы одной ячейки — в один блок `table_cell`. Номера внутренних абзацев идут в текст без отрезков runs."""
    first = paras[0]
    if len(paras) == 1:
        return first
    texts: list[str] = []
    spans: list[DocxRunSpan] = []
    pos = 0
    for i, para in enumerate(paras):
        if i:
            texts.append("\n")
            pos += 1
            if para.number:
                prefix = f"{para.number} "
                texts.append(prefix)
                pos += len(prefix)
        for span in para.spans:
            spans.append(span.model_copy(update={"start": span.start + pos, "end": span.end + pos}))
        texts.append(para.text)
        pos += len(para.text)
    return _Para(
        text="".join(texts),
        spans=spans,
        number=first.number,
        list_level=first.list_level,
        is_bullet=first.is_bullet,
        heading_level=None,
        heading_source=None,
        style_id=first.style_id,
        style_name=first.style_name,
        bold=all(p.bold for p in paras),
        all_caps=all(p.all_caps for p in paras),
        align=first.align,
        indent=first.indent,
        font_size=first.font_size,
        page=first.page,
        page_end=max(p.page_end for p in paras),
        textboxes=[],
        formulas=[],
    )


def _label_value(cells: list[_Cell]) -> tuple[_Cell, _Cell] | None:
    """Строка «подпись — значение»: «Размер обеспечения заявки | 7789.41 РОССИЙСКИЙ РУБЛЬ», «Количество | 200».

    Две ячейки с текстом, подпись — один короткий абзац. Не пара: обе ячейки жирные (шапка
    таблицы, «Заказчик | Поставщик»), обе кончаются двоеточием («От имени Заказчика:») или
    в них бланк подписи «____».
    """
    filled = [c for c in cells if c.paras]
    if len(filled) != 2 or any(c.vmerge is not None or c.nested for c in cells):
        # Объединённая по вертикали ячейка рядом — это строка многоколоночной таблицы, а не пара
        return None
    label, value = filled
    head = label.paras[0]
    if len(label.paras) != 1 or len(head.text) > LABEL_MAX_CHARS or "\n" in head.text:
        return None
    if not any(ch.isalpha() for ch in head.text):
        return None
    texts = [head.text] + [p.text for p in value.paras]
    if any("___" in text for text in texts):
        return None
    if head.bold and all(p.bold for p in value.paras):
        return None
    if head.text.rstrip().endswith(":") and value.paras[-1].text.rstrip().endswith(":"):
        return None
    return label, value


def _merge_label_value(label: _Para, value: _Para) -> _Para:
    """Подпись и значение — в один абзац «подпись: значение»; отрезки runs значения сдвигаются."""
    sep = " " if label.text.rstrip().endswith((":", "-", "—")) else ": "
    shift = len(label.text) + len(sep)
    spans = list(label.spans) + [s.model_copy(update={"start": s.start + shift, "end": s.end + shift}) for s in value.spans]
    return _Para(
        text=label.text + sep + value.text,
        spans=spans,
        number=label.number,
        list_level=label.list_level,
        is_bullet=label.is_bullet,
        heading_level=None,
        heading_source=None,
        style_id=label.style_id,
        style_name=label.style_name,
        bold=label.bold and value.bold,
        all_caps=label.all_caps and value.all_caps,
        align=label.align,
        indent=label.indent,
        font_size=label.font_size,
        page=label.page,
        page_end=max(label.page_end, value.page_end),
        textboxes=[],
        formulas=[],
    )


def _heading_heuristic(para: _Para) -> tuple[int | None, str | None]:
    """Запасное правило: короткий абзац до 120 знаков, целиком жирный или прописными."""
    text = para.text
    if not text or len(text) > 120 or "\n" in text or _TERMINAL.search(text):
        return None, None
    letters = sum(c.isalpha() for c in text)
    if not letters or sum(c.isdigit() for c in text) > letters:
        # Реквизиты и коды («ИНН 7726429427 КПП 772601001», «ОКПД 2: 26.20.15.160») — не заголовки
        return None, None
    if para.bold or para.all_caps:
        level = (para.list_level + 1) if para.list_level is not None else 1
        return level, "heuristic"
    return None, None


def extract_docx(path: str | Path, kind: DocumentKind = "docx", source_pages: int | None = None) -> ExtractionResult:
    """Сырые абзацы DOCX. Для DOC и RTF сюда передаётся сконвертированный файл и исходный `kind`.

    `source_pages` — число страниц исходного файла (`doc_page_count` для DOC): LibreOffice не
    пишет в DOCX ни меток страниц, ни их числа, и без него страницы оцениваются по объёму текста.
    """
    reader = DocxReader(path, kind, source_pages)
    try:
        return reader.read()
    finally:
        reader.zip.close()
