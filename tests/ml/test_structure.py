from __future__ import annotations

from bench.samples import make_contract_docx, make_contract_pdf
from contracts.extraction import ExtractionResult, RawParagraph, SourceMap, TableCellRef
from ml.extract import extract_document
from ml.structure import build_chunks, build_structure, index_chunks, normalize_number
from tests.fakes import FakeEmbedder


def para(order: int, text: str, *, number: str | None = None, heading: int | None = None, page: int = 1,
         table: TableCellRef | None = None, stream: str = "body") -> RawParagraph:
    return RawParagraph(order=order, text=text, number=number, heading_level=heading, page=page, table=table,
                        stream=stream, source_map=SourceMap(format="docx"))


def result(*paragraphs: RawParagraph) -> ExtractionResult:
    return ExtractionResult(kind="docx", format="docx", pages=3, paragraphs=list(paragraphs), pipeline_version="t")


def test_normalize_number():
    assert normalize_number("3.2.") == ("3.2", "decimal", 2)
    assert normalize_number("10.") == ("10", "decimal", 1)
    assert normalize_number("IV.") == ("IV", "roman", 1)
    assert normalize_number("Раздел III") == ("Раздел III", "roman", 1)
    assert normalize_number("а)") == ("а)", "sub", None)
    assert normalize_number("2)") == ("2)", "sub", None)
    assert normalize_number(None) == (None, None, None)


def test_sections_inherit_number_and_heading():
    blocks = build_structure(result(
        para(0, "КОНТРАКТ № 1 на поставку", heading=1),
        para(1, "г. Москва"),
        para(2, "Сроки поставки", number="3.", heading=1),
        para(3, "Поставка в течение 10 дней.", number="3.1."),
        para(4, "Второй абзац того же пункта."),
        para(5, "по заявке Заказчика;", number="а)"),
        para(6, "Порядок приёмки", number="3.2.", heading=2),
        para(7, "Приёмка в течение 5 дней.", number="3.2.1."),
        para(8, "1", table=TableCellRef(table_id=0, row=1, col=0)),
        para(9, "Сервер", table=TableCellRef(table_id=0, row=1, col=1)),
        para(10, "Приложение № 1", heading=1, page=2),
        para(11, "Спецификация без номеров пунктов.", page=2),
        para(12, "Цена с учётом НДС.", stream="footnote", page=3),
    ))
    got = [(b.kind, b.number, b.section_number, b.section_title) for b in blocks]
    assert got == [
        ("heading", None, None, "КОНТРАКТ № 1 на поставку"),
        ("paragraph", None, None, "КОНТРАКТ № 1 на поставку"),  # преамбула — в разделе заголовка документа
        ("heading", "3", "3", "Сроки поставки"),
        ("list_item", "3.1", "3.1", "Сроки поставки"),
        ("paragraph", None, "3.1", "Сроки поставки"),
        ("list_item", "а)", "3.1 а)", "Сроки поставки"),
        ("heading", "3.2", "3.2", "Порядок приёмки"),
        ("list_item", "3.2.1", "3.2.1", "Порядок приёмки"),
        ("table_cell", None, "3.2.1", "Порядок приёмки"),  # номер строки таблицы не раздел
        ("table_cell", None, "3.2.1", "Порядок приёмки"),
        ("heading", None, None, "Приложение № 1"),  # заголовок без номера сбрасывает номер
        ("paragraph", None, None, "Приложение № 1"),
        ("paragraph", None, None, None),  # сноска: только страница
    ]
    assert blocks[3].section_block_id == blocks[2].id and blocks[2].section_block_id == blocks[2].id
    assert [b.level for b in blocks[2:8]] == [1, 2, None, None, 2, 3]
    assert blocks[8].table_id == 0 and (blocks[8].row, blocks[8].col) == (1, 0)


def test_heading_split_into_two_paragraphs_is_one_section():
    blocks = build_structure(result(
        para(0, "Порядок сдачи", number="8.", heading=1),
        para(1, "и приемки Товара", heading=1),
        para(2, "Приёмка в течение 10 рабочих дней.", number="8.1."),
        para(3, "Описание объекта закупки", heading=1),
        para(4, "(техническое задание)", heading=1),
        para(5, "Поставляется мебель."),
        para(6, "Приложение № 2", heading=1),
    ))
    rows = [(b.section_number, b.section_title) for b in blocks]
    assert rows == [
        ("8", "Порядок сдачи и приемки Товара"),
        ("8", "Порядок сдачи и приемки Товара"),
        ("8.1", "Порядок сдачи и приемки Товара"),
        (None, "Описание объекта закупки (техническое задание)"),
        (None, "Описание объекта закупки (техническое задание)"),
        (None, "Описание объекта закупки (техническое задание)"),
        (None, "Приложение № 2"),  # с заглавной буквы — новый раздел
    ]


def test_print_form_table_rows_give_section_titles():
    """Печатная форма ЕИС — одна таблица без заголовков документа: раздел ячейки — жирная строка
    таблицы над ней. Не жирная строка без двоеточия («Требуется обеспечение заявки») — не заголовок."""
    def cell(order, text, row, *, bold=False, table=0, parent=None, col=0):
        p = para(order, text, table=TableCellRef(table_id=table, row=row, col=col, is_layout=True,
                                                 parent_table_id=parent))
        p.bold = bold
        return p

    blocks = build_structure(result(
        para(0, "Извещение о проведении электронного аукциона"),
        cell(1, "Обеспечение заявки", 0, bold=True),
        cell(2, "Требуется обеспечение заявки", 1),
        cell(3, "Размер обеспечения заявки: 7789.41 РОССИЙСКИЙ РУБЛЬ", 2),
        cell(4, "Характеристики товара ( Ветошь )", 0, bold=True, table=1, parent=0),
        cell(5, "Материал", 1, table=1, parent=0),
        cell(6, "Хлопок", 1, table=1, parent=0, col=1),
        cell(7, "Объём", 0, table=2, parent=0),
        cell(8, "Подпись: Иванов", 3, bold=True),
    ))
    titles = [b.section_title for b in blocks]
    assert titles[:4] == [None, "Обеспечение заявки", "Обеспечение заявки", "Обеспечение заявки"]
    assert titles[4:7] == ["Характеристики товара ( Ветошь )"] * 3
    # Вложенная таблица без своей строки-заголовка берёт её у внешней; жирная ячейка с двоеточием — не заголовок
    assert titles[7:] == ["Обеспечение заявки", "Обеспечение заявки"]
    assert blocks[3].section_block_id == blocks[1].id


def test_table_rows_do_not_override_document_headings():
    p = para(2, "Характеристики", table=TableCellRef(table_id=0, row=0, col=0))
    p.bold = True
    blocks = build_structure(result(
        para(0, "Спецификация", heading=1),
        para(1, "Таблица характеристик товара"),
        p,
        para(3, "Цвет", table=TableCellRef(table_id=0, row=1, col=0)),
    ))
    assert {b.section_title for b in blocks} == {"Спецификация"}


def test_ids_are_stable_and_chunks_have_pages():
    res = result(para(0, "Цена", number="2.", heading=1), para(1, "Цена составляет 100 рублей.", number="2.1."),
                 para(2, "слово " * 300, number="2.2.", page=2),
                 para(3, "Сноска", stream="footnote", page=3))
    first = build_structure(res, document_id="doc-1")
    again = build_structure(res, document_id="doc-1")
    other = build_structure(res, document_id="doc-2")
    assert [b.id for b in first] == [b.id for b in again]
    assert first[0].id != other[0].id and len(first[0].id) == 36
    chunks = build_chunks(first, document_id="doc-1", max_tokens=60)
    # Пункт 2.2 длиннее LONG_BLOCK_TOKENS — окна одного блока; сноска не в поиске
    assert chunks[0].block_ids == [first[0].id, first[1].id] and chunks[0].char_start is None
    windows = chunks[1:]
    assert len(windows) > 1 and all(c.block_ids == [first[2].id] for c in windows)
    assert windows[0].char_start == 0 and windows[-1].char_end == len(first[2].text.rstrip())
    assert all(a.char_end <= b.char_start for a, b in zip(windows, windows[1:], strict=False))
    assert (chunks[0].page_from, chunks[0].page_to, chunks[1].page_from) == (1, 1, 2)
    assert chunks[1].text.startswith("Цена\n2.2 ")
    indexed = index_chunks(first, document_id="doc-1", embedder=FakeEmbedder())
    assert [c.id for c in indexed] == [c.id for c in build_chunks(first, document_id="doc-1")]
    assert all(len(c.embedding) == FakeEmbedder.dim for c in indexed)


def test_every_block_of_sample_contract_has_section(tmp_path):
    for path in (make_contract_docx(tmp_path / "c.docx"), make_contract_pdf(tmp_path / "c.pdf")):
        blocks = build_structure(extract_document(path))
        body = blocks[3:]  # после заголовка документа, города и преамбулы
        assert all(b.section_number and b.section_title for b in body), path.suffix
        by_text = {b.text[:20]: b for b in blocks}
        penalty = next(b for b in blocks if b.text.startswith("В случае просрочки исполнения Поставщиком"))
        assert penalty.section_number.count(".") == 1 and penalty.section_title == "Ответственность сторон"
        assert by_text["Реквизиты и подписи "].kind == "heading"
