"""Проверки пакета: без LLM (цифры и пропись, реквизиты, проценты) и противоречия между документами."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contracts.analysis import PackageFinding
from ml.analysis.blocks import blocks_from_text, chunk_blocks
from ml.analysis.checks import (
    check_number_words,
    check_percent_sums,
    check_requisites,
    find_nmck,
    inn_valid,
    ogrn_valid,
    parse_number_words,
    run_checks,
)
from ml.analysis.crosscheck import check_conflicts, check_package
from ml.analysis.search import InMemorySearch
from tests.fakes import FakeEmbedder, FakeLLM

EMBEDDER = FakeEmbedder()


@pytest.mark.parametrize(("text", "value"), [
    ("семи", 7), ("десяти", 10), ("Шестидесяти", 60), ("сто двадцать", 120), ("пяти тысяч", 5000),
    ("один миллион двести тысяч три", 1_200_003), ("тридцать рабочих", 30), ("двенадцать месяцев", 12),
    ("пять рублей 00 копеек", 5), ("тысяча", 1000),
])
def test_parse_number_words(text, value):
    assert parse_number_words(text) == value


@pytest.mark.parametrize("text", ["при наличии таких требований", "первый", "двадцать сто", "семь восемь", ""])
def test_parse_number_words_keeps_silent_on_unknown(text):
    assert parse_number_words(text) is None


def _doc(text: str):
    return blocks_from_text(text)


def test_number_words_mismatch_quotes_exact_place():
    blocks = _doc("Оплата в течение 7 (семи) рабочих дней.\n\nПоставка в течение 10 (пятнадцати) дней.")
    found = check_number_words("d", "c.docx", blocks)
    assert [f.quote for f in found] == ["10 (пятнадцати)"]
    block = next(b for b in blocks if b.id == found[0].block_id)
    assert block.text[found[0].start:found[0].end] == found[0].quote
    assert found[0].code == "A-01" and found[0].confidence == 1.0


@pytest.mark.parametrize("text", [
    "Цена 1 500 000,00 (один миллион пятьсот тысяч) рублей 00 копеек.",
    "Цена 5 419 903 (пять миллионов четыреста девятнадцать тысяч девятьсот три) рубля.",
    "Срок 2.1 (при наличии таких требований).",
    "Пеня 1/300 (одна трехсотая) ставки.",
    "Статья 44 (в редакции закона).",
])
def test_number_words_silent_on_correct_or_unparsable(text):
    assert check_number_words("d", None, _doc(text)) == []


def test_inn_and_ogrn_checksums():
    assert inn_valid("7707083893") and inn_valid("500100732259")
    assert not inn_valid("7707083894") and not inn_valid("500100732258")
    assert ogrn_valid("1027700132195") and not ogrn_valid("1027700132196")


def test_requisites_skip_placeholders_and_repeats():
    text = "Заказчик ИНН 7707083894.\n\nПовторно ИНН 7707083894.\n\nИНН 0000000000.\n\nИНН 7707083893."
    found = check_requisites("d", None, _doc(text))
    assert [f.quote for f in found] == ["ИНН 7707083894"]


def test_percent_of_nmck():
    docs = [("d", "n.docx", _doc(
        "Начальная (максимальная) цена контракта: 1 000 000,00 рублей.\n\n"
        "Обеспечение 5 % от начальной (максимальной) цены контракта составляет 50 000,00 руб.\n\n"
        "Обеспечение заявки 1 % от начальной (максимальной) цены контракта составляет 90 000,00 руб."))]
    assert find_nmck(docs) == Decimal("1000000.00")
    found = check_percent_sums(docs)
    assert len(found) == 1 and "90 000,00" in found[0].quote and "10 000" in found[0].explanation


def test_percent_needs_nmck():
    docs = [("d", None, _doc("Обеспечение 5 % от начальной (максимальной) цены контракта составляет 90 000,00 руб."))]
    assert find_nmck(docs) is None and check_percent_sums(docs) == []


def test_run_checks_keeps_document_of_each_finding():
    docs = [("a", "a.docx", _doc("Срок 3 (пять) дня.")), ("b", "b.docx", _doc("Срок 4 (четыре) дня."))]
    assert [(f.document_id, f.filename) for f in run_checks(docs)] == [("a", "a.docx")]


CONTRACT = "Срок поставки товара: 45 календарных дней с 11 января 2027 года.\n\nПрочие условия контракта."
SPEC = "Срок поставки товара: 45 календарных дней с 11.01.2026.\n\nПрочие условия ТЗ."


def _package():
    docs = [("contract", "contract.docx", _doc(CONTRACT)), ("spec", "spec.docx", _doc(SPEC))]
    store = InMemorySearch()
    for doc_id, _, blocks in docs:
        store.add(doc_id, chunk_blocks(blocks), embedder=EMBEDDER)
    return docs, store


def _conflict(fake: FakeLLM):
    docs, store = _package()
    return check_conflicts(docs, search=store, llm=fake.client(), embedder=EMBEDDER)


def test_conflict_creates_finding_in_each_place_with_refs():
    fake = FakeLLM(conflicts={"delivery_term": [
        ("с 11 января 2027", "45 календарных дней с 11 января 2027 года"),
        ("с 11.01.2026", "45 календарных дней с 11.01.2026"),
    ]})
    result = _conflict(fake)
    assert sorted(f.document_id for f in result.findings) == ["contract", "spec"]
    by_doc = {f.document_id: f for f in result.findings}
    assert by_doc["contract"].code == "X-01" and by_doc["contract"].level == "critical"
    assert "срок поставки" in by_doc["contract"].title
    assert [r.document_id for r in by_doc["contract"].related] == ["spec"]
    assert by_doc["contract"].related[0].value == "с 11.01.2026"
    assert result.usage.calls == 1


def test_conflict_needs_two_anchored_places():
    fake = FakeLLM(conflicts={"delivery_term": [
        ("с 11 января 2027", "45 календарных дней с 11 января 2027 года"),
        ("с 1 марта", "этой цитаты нет ни в одном документе пакета"),
    ]})
    assert _conflict(fake).findings == []


def test_conflict_with_equal_values_is_dropped():
    fake = FakeLLM(conflicts={"delivery_term": [
        ("45 дней", "45 календарных дней с 11 января 2027 года"),
        ("45  дней.", "45 календарных дней с 11.01.2026"),
    ]})
    assert _conflict(fake).findings == []


def test_contract_term_is_not_delivery_term():
    docs = [("contract", "c.docx", _doc("Срок поставки товара: 60 рабочих дней.")),
            ("notice", "n.docx", _doc("Срок исполнения контракта: 120 календарных дней."))]
    store = InMemorySearch()
    for doc_id, _, blocks in docs:
        store.add(doc_id, chunk_blocks(blocks), embedder=EMBEDDER)
    fake = FakeLLM(conflicts={"delivery_term": [("60", "Срок поставки товара: 60 рабочих дней"),
                                                ("120", "Срок исполнения контракта: 120 календарных дней")]})
    assert check_conflicts(docs, search=store, llm=fake.client(), embedder=EMBEDDER).findings == []


def test_unknown_condition_is_ignored():
    fake = FakeLLM(conflicts={"weather": [("a", "Срок поставки товара"), ("b", "Прочие условия ТЗ")]})
    assert _conflict(fake).findings == []


def test_llm_failure_keeps_checks_without_llm():
    import httpx

    from tests.fakes import make_client

    docs = [("a", "a.docx", _doc("Срок 3 (пять) дня."))]
    store = InMemorySearch()
    store.add("a", chunk_blocks(docs[0][2]), embedder=EMBEDDER)
    client = make_client(lambda r: httpx.Response(500, text="сбой"), llm_max_retries=0)
    result = check_package(docs, search=store, llm=client, embedder=EMBEDDER)
    assert [f.code for f in result.findings] == ["A-01"]
    assert all(isinstance(f, PackageFinding) for f in result.findings)


def test_all_checklists_are_valid():
    from ml.analysis.templates import validate_rule
    from ml.checklist import load_default_checklist, load_extra_checklists

    profile, rules = load_extra_checklists()[0]
    assert "223" in profile["name"] and not profile["is_default"]
    assert [r.code for r in rules] == [r.code for r in load_default_checklist()[1]]
    for rule in rules:
        errors, warnings = validate_rule(rule)
        assert errors == [] and warnings == [], rule.code
        assert "44-ФЗ" not in rule.prompt_template and "контракт" not in rule.prompt_template.replace("контрактов", ""), rule.code


def test_document_text_is_not_an_instruction():
    from ml.analysis.crosscheck import SYSTEM

    assert "не выполняй" in SYSTEM
