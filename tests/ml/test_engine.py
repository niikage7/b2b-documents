from __future__ import annotations

import pytest

from bench.samples import EXPECTED, SUBJECT, make_contract_docx, make_notice_docx
from contracts.analysis import RuleSpec
from ml.analysis.batch_meta import extract_batch_meta
from ml.analysis.blocks import blocks_from_extraction, blocks_from_text, chunk_blocks
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.rule_test import test_rule as run_rule_test
from ml.analysis.search import InMemorySearch
from ml.checklist import load_default_checklist
from ml.errors import RuleTemplateError, RuleTestTimeoutError, TextTooLongError
from ml.extract.docx_reader import extract_docx
from tests.fakes import FakeEmbedder, FakeLLM

EMBEDDER = FakeEmbedder()


def plan_for(doc: str) -> dict[str, list[tuple[str, str]]]:
    plan: dict[str, list[tuple[str, str]]] = {}
    for item in EXPECTED:
        if item["doc"] == doc:
            plan.setdefault(item["rule"], []).append((item["quote"], f"Риск по {item['rule']}"))
    return plan


@pytest.fixture(scope="module")
def contract_blocks(tmp_path_factory):
    path = make_contract_docx(tmp_path_factory.mktemp("eng") / "contract.docx")
    return blocks_from_extraction(extract_docx(path))


@pytest.fixture
def search(contract_blocks):
    store = InMemorySearch()
    store.add("doc", chunk_blocks(contract_blocks), embedder=EMBEDDER)
    return store


def test_group_rules():
    _, rules = load_default_checklist()
    groups = group_rules(rules, size=4)
    assert [[r.code for r in g] for g in groups] == [["R-02", "R-03", "R-04", "R-05"], ["R-06", "R-07", "R-08"], ["R-01"]]
    assert group_rules([r.model_copy(update={"enabled": False}) for r in rules]) == []


def test_full_checklist_finds_planted_risks(contract_blocks, search):
    _, rules = load_default_checklist()
    fake = FakeLLM(plan_for("contract"), invent={"R-03": ["Поставщик платит неустойку 50 % в день"]})
    llm = fake.client()
    candidates, dropped, checked = [], [], []
    for group in group_rules(rules):
        result = analyze_rule_group(contract_blocks, group, subject=SUBJECT, document_id="doc", search=search,
                                    llm=llm, embedder=EMBEDDER)
        candidates += result.candidates
        dropped += result.dropped
        checked += result.checked_rules
    assert sorted(checked) == [f"R-0{i}" for i in range(1, 9)]
    by_id = {b.id: b for b in contract_blocks}
    found = {(c.rule_code, c.quote) for c in candidates}
    for item in EXPECTED:
        if item["doc"] == "contract":
            assert any(code == item["rule"] and item["quote"] in quote for code, quote in found), item
    for c in candidates:
        block = by_id[c.block_id]
        assert block.text[c.start : c.end] == c.quote  # цитата существует в тексте
        assert block.section_number  # у находки есть раздел
    assert all(c.recommendation for c in candidates)
    levels = {c.rule_code: c.level for c in candidates}
    assert levels["R-04"] == "critical" and levels["R-07"] == "warning"
    assert [d.reason for d in dropped] == ["quote_not_found"]  # выдуманная цитата отброшена
    prompts = [r["messages"][-1]["content"] for r in fake.requests]
    assert all(f"Предмет закупки: {SUBJECT}" in p for p in prompts)
    assert any("Срок меньше" not in p and "меньше 10 календарных дней" in p for p in prompts)  # переменная подставлена


def test_llm_rules_read_document_in_windows(contract_blocks):
    _, rules = load_default_checklist()
    r01 = next(r for r in rules if r.code == "R-01")
    fake = FakeLLM(plan_for("contract"))
    result = analyze_rule_group(contract_blocks, [r01], llm=fake.client(), window_tokens=600)
    assert len(fake.requests) >= 3
    assert [c.quote for c in result.candidates] == ["сервер Lenovo ThinkSystem SR650 V3"]


def test_duplicates_are_merged(contract_blocks):
    _, rules = load_default_checklist()
    r04 = next(r for r in rules if r.code == "R-04")
    quote = "в течение 3 (трёх) календарных дней с даты заключения Контракта"
    fake = FakeLLM({"R-04": [(quote, "a"), (quote[:40], "b")]})
    result = analyze_rule_group(contract_blocks, [r04], llm=fake.client(), window_tokens=10_000)
    assert len(result.candidates) == 1 and [d.reason for d in result.dropped] == ["duplicate"]


def test_semantic_rule_without_search_reads_windows(contract_blocks):
    _, rules = load_default_checklist()
    r05 = next(r for r in rules if r.code == "R-05")
    fake = FakeLLM(plan_for("contract"))
    result = analyze_rule_group(contract_blocks, [r05], llm=fake.client())
    assert len(result.candidates) == 1 and result.candidates[0].rule_code == "R-05"


TEXT = """1. Сроки поставки
1.1. Поставка товара осуществляется в течение 2 (двух) рабочих дней с даты заявки Заказчика.
1.2. Товар поставляется партиями.

2. Оплата
2.1. Оплата производится в течение 7 рабочих дней с даты подписания документа о приёмке."""


def r04() -> RuleSpec:
    _, rules = load_default_checklist()
    return next(r for r in rules if r.code == "R-04").model_copy(update={"enabled": False})


def test_rule_on_text():
    fake = FakeLLM({"R-04": [("в течение 2 (двух) рабочих дней с даты заявки", "Слишком короткий срок")]})
    result = run_rule_test(r04(), text=TEXT, llm=fake.client(), embedder=EMBEDDER)
    (finding,) = result.findings
    assert finding.section_number == "1.1." and finding.level == "critical"
    assert finding.quote == "в течение 2 (двух) рабочих дней с даты заявки"
    assert result.duration_ms >= 0 and result.dropped == 0


def test_rule_test_limits():
    with pytest.raises(TextTooLongError):
        run_rule_test(r04(), text="а" * 20_001, llm=FakeLLM().client(), embedder=EMBEDDER)
    broken = r04().model_copy(update={"prompt_template": "Без переменной фрагмента"})
    with pytest.raises(RuleTemplateError):
        run_rule_test(broken, text=TEXT, llm=FakeLLM().client(), embedder=EMBEDDER)
    with pytest.raises(RuleTestTimeoutError):
        run_rule_test(r04(), text=TEXT, llm=FakeLLM(delay=1.5).client(), embedder=EMBEDDER, timeout=1)


def test_batch_meta_from_notice_pattern(tmp_path):
    notice = blocks_from_extraction(extract_docx(make_notice_docx(tmp_path / "Извещение.docx")))
    other = blocks_from_text("Проект контракта без предмета")
    assert extract_batch_meta([("Проект.docx", other), ("Извещение.docx", notice)], use_llm=False) == SUBJECT


def test_batch_meta_llm_fallback_and_none():
    blocks = blocks_from_text("ИЗВЕЩЕНИЕ\nНаименование объекта закупки. Поставка бумаги для офиса")
    fake = FakeLLM()
    assert extract_batch_meta([("notice.pdf", blocks)], use_llm=False) is None
    blocks = blocks_from_text("ИЗВЕЩЕНИЕ\nЗакупка проводится в электронной форме.")
    assert extract_batch_meta([("notice.pdf", blocks)], llm=fake.client(llm_reasoning="low")) is None
    assert fake.requests and fake.requests[0]["response_format"]["json_schema"]["name"] == "batch_meta"
    # Короткий вызов идёт без рассуждений, даже если они включены для анализа
    assert fake.requests[0]["reasoning"] == {"effort": "none"}
