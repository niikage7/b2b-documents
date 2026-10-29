from __future__ import annotations

import httpx
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
from tests.fakes import FakeEmbedder, FakeLLM, make_client

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


def test_no_risk_verdict_is_dropped(contract_blocks):
    _, rules = load_default_checklist()
    r04 = next(r for r in rules if r.code == "R-04")
    quote = "в течение 3 (трёх) календарных дней с даты заключения Контракта"
    fake = FakeLLM(safe={"R-04": [quote]})
    result = analyze_rule_group(contract_blocks, [r04], llm=fake.client(), window_tokens=10_000)
    assert result.candidates == []
    assert [(d.reason, d.quote) for d in result.dropped] == [("no_risk", quote)]


def _verdicts(fake: FakeLLM) -> list[dict]:
    return [r for r in fake.requests if r.get("response_format", {}).get("json_schema", {}).get("name")
            == "finding_verdict"]


@pytest.fixture
def verify_all(monkeypatch):
    """Второй взгляд на всех кандидатов: у подделки LLM уверенность 0,9 — выше порога по умолчанию."""
    from ml.config import get_settings

    monkeypatch.setenv("LLM_VERIFY_BELOW", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_second_look_drops_unconfirmed(contract_blocks, verify_all):
    _, rules = load_default_checklist()
    r04 = next(r for r in rules if r.code == "R-04")
    keep = "в течение 3 (трёх) календарных дней с даты заключения Контракта"
    plan = plan_for("contract")
    other = next(q for q, _ in plan["R-07"])
    fake = FakeLLM({"R-04": [(keep, "a")], "R-07": [(other, "b")]}, reject=[other])
    r07 = next(r for r in rules if r.code == "R-07")
    result = analyze_rule_group(contract_blocks, [r04, r07], llm=fake.client(), window_tokens=100_000)
    assert [c.rule_code for c in result.candidates] == ["R-04"]
    assert [(d.rule_code, d.reason) for d in result.dropped] == [("R-07", "not_confirmed")]
    verdicts = _verdicts(fake)
    assert len(verdicts) == 2  # по вызову на кандидата
    prompt = verdicts[0]["messages"][-1]["content"]
    assert "Проверяемое условие: «" in prompt and "Текст документа" in prompt
    assert result.usage.calls == 3


def test_second_look_failure_keeps_finding(contract_blocks, verify_all):
    _, rules = load_default_checklist()
    r04 = next(r for r in rules if r.code == "R-04")
    quote = "в течение 3 (трёх) календарных дней с даты заключения Контракта"
    fake = FakeLLM({"R-04": [(quote, "a")]})

    def handler(request):
        if b"finding_verdict" in request.content:
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        return fake.handler(request)

    result = analyze_rule_group(contract_blocks, [r04], llm=make_client(handler), window_tokens=10_000)
    assert [c.quote for c in result.candidates] == [quote] and result.dropped == []


def test_second_look_only_for_doubtful(contract_blocks):
    _, rules = load_default_checklist()
    r04 = next(r for r in rules if r.code == "R-04")
    quote = "в течение 3 (трёх) календарных дней с даты заключения Контракта"
    fake = FakeLLM({"R-04": [(quote, "a")]}, reject=[quote])  # уверенность подделки 0,9 — выше порога 0,85
    result = analyze_rule_group(contract_blocks, [r04], llm=fake.client(), window_tokens=10_000)
    assert len(result.candidates) == 1 and _verdicts(fake) == []


def test_long_block_goes_to_llm_as_found_windows():
    filler = "".join(f"Участник представляет документ номер {i} в составе заявки. " for i in range(80))
    target = "Размер обеспечения исполнения контракта составляет 30 % начальной цены."
    blocks = blocks_from_text(f"1. Общие положения\n\n{filler}{target} {filler}")
    store = InMemorySearch()
    store.add("doc", chunk_blocks(blocks), embedder=EMBEDDER)
    _, rules = load_default_checklist()
    r02 = next(r for r in rules if r.code == "R-02")
    fake = FakeLLM({"R-02": [(target, "30 % больше порога")]})
    result = analyze_rule_group(blocks, [r02], document_id="doc", search=store, llm=fake.client(),
                                embedder=EMBEDDER, budget_tokens=400)
    prompt = fake.requests[0]["messages"][-1]["content"]
    assert target in prompt and "…" in prompt and len(prompt) < len(blocks[1].text)  # блок не целиком
    (found,) = result.candidates
    assert found.block_id == blocks[1].id and blocks[1].text[found.start : found.end] == target  # смещения в блоке


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


def test_passport_anchors_values_and_drops_invented(tmp_path, contract_blocks):
    from ml.analysis.passport import extract_passport

    notice = blocks_from_extraction(extract_docx(make_notice_docx(tmp_path / "Извещение.docx")))
    fake = FakeLLM(passport={
        "nmck": ("4 850 000,00 руб.", "Начальная (максимальная) цена контракта: 4 850 000,00 рублей"),
        "delivery_term": ("3 календарных дня с даты заключения", "в течение 3 (трёх) календарных дней с даты заключения"),
        "advance": ("30 %", "Аванс составляет 30 процентов"),  # такого текста нет — поле отбрасывается
        "unknown": ("x", "Аванс по Контракту не предусмотрен"),  # неизвестное поле
    })
    passport = extract_passport([("n", "Извещение.docx", notice), ("c", "Контракт.docx", contract_blocks)],
                                llm=fake.client(), embedder=EMBEDDER)
    assert [f.key for f in passport.fields] == ["nmck", "delivery_term"]  # порядок карточки, а не ответа
    nmck, delivery = passport.fields
    assert nmck.document_id == "n" and nmck.filename == "Извещение.docx" and nmck.value == "4 850 000,00 руб."
    block = next(b for b in contract_blocks if b.id == delivery.block_id)
    assert block.text[delivery.start:delivery.end] == delivery.quote and delivery.section_number
    assert passport.value("nmck") == "4 850 000,00 руб." and passport.value("advance") is None
    prompt = fake.requests[0]["messages"][-1]["content"]
    assert "Гарантийный срок на Товар" in prompt and "Размер обеспечения исполнения" in prompt


def test_clarification_draft(tmp_path):
    from docx import Document

    from ml.analysis.clarification import draft_clarification, render_clarification_docx

    findings = [
        {"id": "f1", "quote": "в течение 3 (трёх) календарных дней с даты заключения Контракта", "section_number": "3.1",
         "page": 2, "filename": "Контракт.docx", "rule": {"title": "Срок поставки короче разумного"},
         "explanation": "Срок 3 дня нереален для серверов под заказ.",
         "recommendation": "Направить запрос на разъяснение и предложить срок не менее 30 дней."},
        {"id": "f2", "quote": "лицензию ФСБ России", "section_number": None, "page": 1,
         "document": {"filename": "Извещение.docx"}, "explanation": "", "recommendation": ""},
        {"id": "f3", "quote": "  ", "section_number": "1.1"},  # пустая цитата — не вопрос
    ]
    offline = draft_clarification(findings, subject="поставка серверов", customer="ФКУ «ЦОД»",
                                  procurement_number="0373100000126000045", use_llm=False)
    assert [i.finding_id for i in offline.items] == ["f1", "f2"]
    assert offline.items[0].request == ("Просим разъяснить данное условие. Срок 3 дня нереален для серверов под заказ. "
                                        "Предлагаем срок не менее 30 дней.")
    assert offline.intro.startswith("В соответствии с частью 5 статьи 42") and "0373100000126000045" in offline.intro
    text = offline.as_text()
    assert "1. В пункте 3.1 документа «Контракт.docx» установлено:" in text
    assert "2. На странице 1 документа «Извещение.docx»" in text

    fake = FakeLLM()
    drafted = draft_clarification(findings, llm=fake.client(), law="223")
    assert [i.request for i in drafted.items] == ["Просим разъяснить условие 1.", "Просим разъяснить условие 2."]
    assert "223-ФЗ" in drafted.intro and "«в течение 3 (трёх)" in fake.requests[0]["messages"][-1]["content"]

    path = render_clarification_docx(offline, tmp_path / "Запрос.docx", sender="ООО «Северный терминал»")
    paragraphs = [p.text for p in Document(str(path)).paragraphs]
    assert paragraphs[0] == "Заказчику: ФКУ «ЦОД»" and any("ЗАПРОС О ДАЧЕ РАЗЪЯСНЕНИЙ" in p for p in paragraphs)
    assert any(p.startswith("1. В пункте 3.1") for p in paragraphs)
    assert any("ООО «Северный терминал»" in p for p in paragraphs)


@pytest.mark.parametrize("relative", [False, True])
def test_clarification_groups_keep_own_answers(relative):
    """Ответ второй группы не затирает вопросы первой, как бы модель ни нумеровала условия."""
    import json
    import re

    from ml.analysis.clarification import GROUP, draft_clarification
    from tests.fakes import _ok, make_client

    def handler(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, re.M)]
        items = [{"index": pos if relative else n, "request": f"Вопрос по условию {n}."}
                 for pos, n in enumerate(numbers, 1)]
        return _ok(json.dumps({"items": items}, ensure_ascii=False))

    findings = [{"id": f"f{i}", "quote": f"условие номер {i}", "explanation": "", "recommendation": ""}
                for i in range(1, GROUP + 3)]
    draft = draft_clarification(findings, llm=make_client(handler))
    assert [i.request for i in draft.items] == [f"Вопрос по условию {i}." for i in range(1, GROUP + 3)]
