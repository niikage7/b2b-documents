"""Контракт 1 (часть): блоки и фрагменты документа после `build_structure` и `index_chunks`.

Используют: слой данных (таблицы `block` и `chunk`), движок правил, API документа,
экспорт. Поля — из таблиц `block` и `chunk` документа «Требования к бэкенду».

Соглашения:

- `number` — номер пункта без текста и без точки в конце: «3.2», «IV», «а)». Как номер
  выглядел в документе, видно в `RawParagraph.number`.
- `section_number` — номер пункта, в котором стоит блок: свой номер, а у блока без номера —
  ближайший номер выше («3.2» для второго абзаца пункта 3.2, «3.2 а)» для подпункта).
- `section_title` и `section_block_id` — ближайший заголовок выше; у заголовка — он сам.
- Нет ни номера, ни заголовка — раздел пуст, ссылкой на место служит `page`.
- Идентификаторы детерминированы: повтор `build_structure` для того же документа даёт те
  же `id`, и ручные пометки на блоках не теряются.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from contracts.extraction import Align, BBox, SourceMap, Stream

BlockKind = Literal["heading", "paragraph", "list_item", "table_cell"]


class Block(BaseModel):
    """Строка таблицы `block`: единое представление документа для анализа, экрана и экспорта."""

    id: str
    document_id: str | None = None
    order: int = Field(description="Порядок в документе, с 0")
    kind: BlockKind
    number: str | None = Field(None, description="Номер пункта: «3.2», «IV», «а)»")
    text: str = Field(description="Текст без номера; смещения находок считаются в нём")
    page: int
    page_end: int | None = None
    level: int | None = Field(None, description="Уровень заголовка или глубина номера пункта, с 1")
    align: Align | None = None
    bold: bool = False
    stream: Stream = "body"
    section_block_id: str | None = None
    section_number: str | None = None
    section_title: str | None = None
    table_id: int | None = None
    row: int | None = None
    col: int | None = None
    bbox: BBox | None = None
    low_ocr_quality: bool = False
    source_map: SourceMap | None = None


class ChunkRecord(BaseModel):
    """Строка таблицы `chunk`: фрагмент из нескольких блоков подряд с эмбеддингом."""

    id: str
    document_id: str | None = None
    block_ids: list[str]
    text: str = Field(description="Текст для поиска: название раздела, номера пунктов и текст блоков")
    page_from: int
    page_to: int
    embedding: list[float] | None = None
