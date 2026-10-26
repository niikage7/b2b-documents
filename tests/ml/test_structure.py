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
    assert [c.block_ids for c in chunks] == [[first[0].id, first[1].id], [first[2].id]]  # сноска не в поиске
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
