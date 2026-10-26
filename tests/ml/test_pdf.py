from __future__ import annotations

import pymupdf
import pytest

from bench.fonts import font_path
from bench.samples import CONTRACT, expected_numbers, make_contract_pdf
from ml.errors import TooManyPagesError
from ml.extract.layout import LWord, build_paragraphs, group_lines, para_to_raw, reading_order
from ml.extract.pdf_extract import build_pdf_result, extract_pdf, plan_pdf
from ml.extract.pdf_text import Char, layer_quality

pytestmark = pytest.mark.skipif(font_path("serif") is None, reason="нет шрифта с кириллицей")


@pytest.fixture(scope="module")
def contract_pdf(tmp_path_factory):
    return make_contract_pdf(tmp_path_factory.mktemp("pdf") / "contract.pdf")


@pytest.fixture(scope="module")
def contract(contract_pdf):
    return extract_pdf(contract_pdf, run_ocr=False)


def test_numbers_match_source(contract):
    numbers = [i.number for i in contract.paragraphs if i.number]
    assert numbers == [n for n in expected_numbers(CONTRACT) if n] + ["10."]
    assert all(i.number_source == "pattern" for i in contract.paragraphs if i.number)


def test_headers_footers_and_footnote_mark(contract):
    texts = [i.text for i in contract.paragraphs]
    assert not any(t.startswith("Проект контракта на поставку") for t in texts)
    assert not any(t.startswith("Стр.") for t in texts)
    price = next(i for i in contract.paragraphs if i.number == "2.1.")
    assert price.text.endswith("НДС 20 %.")  # знак сноски «1» убран: внизу есть сноска 1
    area = next(i for i in contract.paragraphs if i.number == "3.2.")
    assert area.text.endswith("120 м².")


def test_hyphen_kept_and_page_info(contract):
    item = next(i for i in contract.paragraphs if i.number == "3.3.")
    assert "погрузочно-разгрузочные" in item.text
    assert contract.kind == "pdf" and contract.pages == 2
    assert [p.kind for p in contract.page_info] == ["text", "text"]


def test_requisites_read_column_by_column(contract):
    tail = [i.text for i in contract.paragraphs[-8:]]
    customer = next(n for n, t in enumerate(tail) if t.startswith("Заказчик"))
    supplier = next(n for n, t in enumerate(tail) if t.startswith("Поставщик"))
    assert any("Иванов" in t for t in tail[customer:supplier])
    assert any("Смирнов" in t for t in tail[supplier:])


def test_word_boxes_point_at_text(contract_pdf, contract):
    with pymupdf.open(contract_pdf) as doc:
        for item in contract.paragraphs[:12]:
            for wb in item.source_map.words:
                clip = pymupdf.Rect(wb.bbox.x0, wb.bbox.y0, wb.bbox.x1, wb.bbox.y1)
                found = doc[wb.page - 1].get_text("text", clip=clip).replace("\n", "").replace(" ", "")
                assert item.text[wb.start : wb.end].replace("-", "") in found.replace("\xad", "").replace("-", "")


def test_rotated_page_highlight_lands_on_word(tmp_path):
    font = pymupdf.Font(fontfile=str(font_path("serif")))
    doc = pymupdf.open()
    page = doc.new_page()
    tw = pymupdf.TextWriter(page.rect)
    tw.append((72, 100), "Поставка в течение трёх дней с даты заключения", font=font, fontsize=12)
    tw.write_text(page)
    page.set_rotation(90)
    path = tmp_path / "rotated.pdf"
    doc.save(path)
    result = extract_pdf(path, run_ocr=False)
    (item,) = result.paragraphs
    assert item.text == "Поставка в течение трёх дней с даты заключения"
    word = next(w for w in item.source_map.words if item.text[w.start : w.end] == "трёх")
    with pymupdf.open(path) as doc2:
        page2 = doc2[0]
        annot = page2.add_highlight_annot(pymupdf.Rect(word.bbox.x0, word.bbox.y0, word.bbox.x1, word.bbox.y1))
        assert annot is not None
        under = page2.get_text("text", clip=pymupdf.Rect(word.bbox.x0, word.bbox.y0, word.bbox.x1, word.bbox.y1))
        assert "трёх" in under


def test_broken_layer_detection():
    def chars(text):
        return [Char(c, 0, 0, 1, 1, 10, False, 1) for c in text if not c.isspace()]

    good = "Поставщик обязуется поставить товар в срок, указанный в контракте"
    words = [LWord(w, 0, 0, 1, 1, 10, 1) for w in good.split()]
    assert not layer_quality(chars(good), words).broken
    garbage = "�" * 10 + "abc" * 30
    assert layer_quality(chars(garbage), words).broken
    consonants = "бвгджзклмнпрстфхцчшщ" * 5
    assert layer_quality(chars(consonants), [LWord(consonants, 0, 0, 1, 1, 10, 1)]).broken


def test_number_pattern_does_not_eat_sums():
    def para(text):
        words = [LWord(t, 10 + 30 * i, 0, 35 + 30 * i, 10, 10, 1) for i, t in enumerate(text.split())]
        return para_to_raw(build_paragraphs(reading_order(group_lines(words)))[0], 0)

    assert para("10 000 рублей за единицу").number is None
    assert para("3.2 Срок поставки").number == "3.2"
    assert para("а) поставка товара").number == "а)"
    assert para("Статья 5. Ответственность").number == "Статья 5."
    assert para("01.10.2026 г. подписан акт").number is None


def test_scan_and_empty_pages_are_classified(tmp_path, contract_pdf):
    src = pymupdf.open(contract_pdf)
    pix = src[0].get_pixmap(dpi=150)
    doc = pymupdf.open()
    scan = doc.new_page()
    scan.insert_image(scan.rect, pixmap=pix)
    doc.new_page()  # пустая
    mixed = doc.new_page()
    font = pymupdf.Font(fontfile=str(font_path("serif")))
    tw = pymupdf.TextWriter(mixed.rect)
    for i in range(6):
        tw.append((72, 60 + 14 * i), "Текстовый слой страницы с несколькими строками текста", font=font, fontsize=11)
    tw.write_text(mixed)
    mixed.insert_image(pymupdf.Rect(0, 300, 595, 842), pixmap=src[1].get_pixmap(dpi=150, clip=pymupdf.Rect(0, 0, 595, 542)))
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    plan = plan_pdf(path)
    assert [(p.kind, p.needs_ocr) for p in plan] == [("scan", True), ("empty", False), ("mixed", True)]
    result = build_pdf_result(path, plan, [])
    assert result.paragraphs and all(i.page == 3 for i in result.paragraphs)


def test_too_many_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_MAX_PAGES", "3")
    from ml.config import get_settings

    get_settings.cache_clear()
    doc = pymupdf.open()
    for _ in range(4):
        doc.new_page()
    doc.save(tmp_path / "big.pdf")
    with pytest.raises(TooManyPagesError):
        plan_pdf(tmp_path / "big.pdf")
