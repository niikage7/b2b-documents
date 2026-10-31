"""Контракт 2: сырые абзацы парсера и карта исходных позиций.

Владелец — Артём. Используют: Андрей (`build_structure` строит из абзацев блоки),
Влад (задачи `extract_text` и `ocr_pages`, экспорт DOCX и PDF).

Статус: черновик, ждёт согласования с Андреем и Владом (вопросы — в docs/ml-plan.md,
раздел «Что согласовать»).

Соглашения:

- Смещения `start` / `end` считаются в символах (кодовых точках) `RawParagraph.text`,
  с 0, `end` не включается.
- Символы вне BMP парсер заменяет на U+FFFD, поэтому смещения в Python и в JavaScript
  (UTF-16) совпадают.
- Страницы нумеруются с 1.
- Координаты `BBox` — в пунктах PDF, начало в левом верхнем углу страницы, в той
  системе, которую возвращает `pymupdf.Page.get_text` и принимает
  `Page.add_highlight_annot`. Для повёрнутых страниц (`/Rotate`) это система уже
  повёрнутой страницы, как её видит пользователь.
- Номер пункта (`number`) в `text` не входит. Парсер отдаёт номер как есть («3.2.», «а)»),
  к виду раздела его приводит `build_structure`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DocumentKind = Literal["docx", "doc", "rtf", "pdf", "scan"]
"""Значение `document.kind`: исходный формат; `scan` — PDF, где больше половины страниц прошли OCR."""

FileKind = Literal["pdf", "docx", "doc", "rtf", "zip"]
FileCheckStatus = Literal["ok", "unsupported", "encrypted", "corrupted"]
PageKind = Literal["text", "scan", "mixed", "curves", "empty"]
Stream = Literal["body", "footnote", "textbox", "formula"]
Align = Literal["left", "center", "right", "justify"]


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class FileCheck(BaseModel):
    """Результат `check_file`: проверка файла при загрузке, без извлечения текста."""

    check: FileCheckStatus
    kind: FileKind | None = None
    message: str | None = Field(None, description="Причина на русском, показывается пользователю")
    pages: int | None = Field(None, description="Число страниц, если его можно узнать дёшево (PDF)")


class DocxRunSpan(BaseModel):
    """Отрезок текста абзаца, который целиком лежит в одном `w:r`.

    Внутри отрезка символы соответствуют один к одному: символ `text[start + i]` —
    это символ `Run.text[run_offset + i]` (python-docx ≥ 1.2).
    """

    start: int
    end: int
    paragraph_index: int = Field(description="Номер `w:p` среди `part.element.iter(w:p)`")
    run_index: int = Field(description="Номер `w:r` среди `paragraph.iter(w:r)`")
    run_offset: int = Field(description="Смещение начала отрезка в `Run.text`")


class WordBox(BaseModel):
    """Слово PDF или OCR: отрезок текста абзаца и его рамка на странице."""

    start: int
    end: int
    page: int
    bbox: BBox
    confidence: float | None = Field(None, description="Уверенность OCR 0–1; для текстового слоя None")


class SourceMap(BaseModel):
    """Карта исходных позиций абзаца для экспорта (FR-7.1, FR-7.2).

    Для DOCX заполнен `runs`, для PDF и сканов — `words`. Отрезки идут по возрастанию
    `start` и не пересекаются. Символы вне отрезков (например, пробелы между словами PDF
    или текст из `w:sym`) подсветить в исходнике нельзя, но подсветка соседних символов
    от этого не ломается.
    """

    format: Literal["docx", "pdf"]
    part: Literal["document", "footnotes"] = Field(
        "document", description="Часть DOCX, в которой считается `paragraph_index`"
    )
    runs: list[DocxRunSpan] = Field(default_factory=list)
    words: list[WordBox] = Field(default_factory=list)


class TableCellRef(BaseModel):
    """Положение абзаца-ячейки в таблице. Объединённая ячейка отдаётся один раз с размерами."""

    table_id: int = Field(description="Порядковый номер таблицы в документе, с 0")
    row: int = Field(description="Строка, с 0")
    col: int = Field(description="Столбец сетки таблицы, с 0")
    row_span: int = 1
    col_span: int = 1
    is_layout: bool = Field(
        False, description="Таблица без видимых границ (реквизиты, подписи): по ней не строят разделы"
    )
    parent_table_id: int | None = Field(None, description="Для вложенной таблицы — внешняя таблица")


class RawParagraph(BaseModel):
    """Сырой абзац: один абзац DOCX, одна ячейка таблицы или один абзац PDF и скана."""

    order: int = Field(description="Порядок в документе, с 0")
    text: str = Field(description="Текст без номера пункта")
    number: str | None = Field(None, description="Номер пункта как в документе: «3.2.», «а)», «IV.»")
    number_source: Literal["numbering", "pattern"] | None = Field(
        None,
        description="numbering — автонумерация DOCX; pattern — номер выделен из начала текста по шаблону",
    )
    list_level: int | None = Field(None, description="Уровень списка `w:ilvl`, с 0")
    is_bullet: bool = Field(False, description="Элемент маркированного списка: номера нет")
    heading_level: int | None = Field(None, description="Уровень заголовка, с 1")
    heading_source: Literal["outline", "style", "heuristic"] | None = None
    style_id: str | None = None
    style_name: str | None = None
    bold: bool = Field(False, description="Весь текст абзаца жирный")
    all_caps: bool = Field(False, description="Весь текст прописными буквами или с форматом «все прописные»")
    align: Align | None = None
    indent: float | None = Field(None, description="Отступ слева в пунктах")
    font_size: float | None = Field(None, description="Преобладающий кегль в пунктах")
    stream: Stream = "body"
    table: TableCellRef | None = None
    page: int = Field(description="Страница начала абзаца")
    page_end: int | None = Field(None, description="Страница конца, если абзац переходит на следующую")
    page_estimated: bool = Field(False, description="Страница оценена приблизительно (DOCX без меток страниц)")
    bbox: BBox | None = Field(None, description="Рамка абзаца на странице `page` (PDF и сканы)")
    low_ocr_quality: bool = False
    ocr_confidence: float | None = Field(None, description="Средняя уверенность OCR по словам абзаца")
    source_map: SourceMap


class PageInfo(BaseModel):
    page: int
    width: float
    height: float
    rotation: int = 0
    kind: PageKind = "text"
    needs_ocr: bool = False
    text_layer_broken: bool = Field(False, description="Текстовый слой есть, но не годится: страница ушла в OCR")
    ocr_confidence: float | None = None
    low_quality: bool = Field(False, description="Уверенность OCR ниже порога из настроек (FR-3.5)")


class OcrWord(BaseModel):
    text: str
    bbox: BBox = Field(description="Рамка на странице: по ней подсвечивают при экспорте")
    layout: BBox | None = Field(
        None, description="Рамка на выпрямленном изображении в пунктах: по ней собираются строки"
    )
    confidence: float = Field(description="0–1")
    block: int
    par: int
    line: int


class OcrPage(BaseModel):
    """Результат OCR одной страницы: только слова. Абзацы собираются после всех групп страниц."""

    page: int
    width: float
    height: float
    rotation: int = Field(0, description="Поворот, найденный определением ориентации, градусы")
    skew: float = Field(0.0, description="Найденный наклон, градусы")
    words: list[OcrWord]
    confidence: float | None = Field(None, description="Средняя по странице с весом по числу символов")
    low_quality: bool = False
    image_key: str | None = Field(None, description="Путь к изображению страницы для FR-5.6*")


class ExtractionResult(BaseModel):
    """Результат извлечения текста документа: вход `build_structure`."""

    kind: DocumentKind
    format: Literal["docx", "pdf"] = Field(description="Что разбирал парсер: DOC и RTF сначала конвертируются в DOCX")
    pages: int
    pages_estimated: bool = False
    paragraphs: list[RawParagraph]
    page_info: list[PageInfo] = Field(default_factory=list)
    ocr_confidence: float | None = Field(None, description="Средняя уверенность OCR по документу (FR-3.5)")
    low_quality_pages: list[int] = Field(default_factory=list)
    pipeline_version: str
