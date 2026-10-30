"""Вопрос к пакету, вывод по пакету, черновик правила: ответы модели проверяются кодом."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from ml.analysis.ask import NOT_FOUND, ask_package
from ml.analysis.blocks import blocks_from_text, chunk_blocks
from ml.analysis.brief import package_brief
from ml.analysis.rule_draft import draft_rule
from ml.analysis.search import InMemorySearch
from ml.errors import LLMError, LLMResponseError
from tests.fakes import FakeEmbedder, _ok, make_client

EMBEDDER = FakeEmbedder()
QUOTE = "Срок оплаты составляет 7 рабочих дней"


def _documents():
    docs = [("c", "contract.docx", blocks_from_text(f"{QUOTE}.\n\nПрочие условия контракта."))]
    store = InMemorySearch()
    store.add("c", chunk_blocks(docs[0][2]), embedder=EMBEDDER)
    return docs, store


def _reply(answer: dict):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _ok(json.dumps(answer, ensure_ascii=False))

    return make_client(handler), requests


def _ask(answer: dict):
    docs, store = _documents()
    client, requests = _reply(answer)
    return ask_package("Какой срок оплаты?", docs, search=store, llm=client, embedder=EMBEDDER), requests


def test_ask_anchors_citation_to_block():
    result, requests = _ask({"found": True, "answer": "7 рабочих дней.", "citations": [{"fragment": 1, "quote": QUOTE}]})
    assert result.found and result.answer == "7 рабочих дней."
    ref = result.citations[0]
    assert ref.document_id == "c" and ref.filename == "contract.docx" and ref.quote == QUOTE
    assert "не выполняй" in requests[0]["messages"][0]["content"]
    assert requests[0]["response_format"]["json_schema"]["name"] == "package_answer"


def test_ask_hides_answer_without_real_citation():
    result, _ = _ask({"found": True, "answer": "90 дней.", "citations": [{"fragment": 1, "quote": "оплата за 90 дней"}]})
    assert not result.found and result.answer == NOT_FOUND and result.citations == []


def test_ask_not_found():
    result, _ = _ask({"found": False, "answer": "", "citations": []})
    assert not result.found and result.answer == NOT_FOUND


def test_ask_empty_package_does_not_call_llm():
    client, requests = _reply({})
    result = ask_package("Вопрос?", [], llm=client, embedder=EMBEDDER)
    assert not result.found and requests == []


FINDINGS = [
    {"id": "w", "level": "warning", "rule": {"title": "Срок приёмки"}, "quote": "60 дней", "filename": "c.docx"},
    {"id": "k", "level": "critical", "rule": {"title": "Пеня"}, "quote": "1 % в день", "filename": "c.docx",
     "section_number": "6.4", "document_id": "d"},
]


def test_brief_decision_comes_from_findings_not_from_model():
    client, requests = _reply({"summary": "Поставка мебели, главное — пеня.", "top": [1, 99, 1], "actions": ["Запросить разъяснение."]})
    brief = package_brief(FINDINGS, subject="Мебель", passport=[{"label": "НМЦК", "value": "5 млн"}], llm=client)
    assert brief.decision == "clarify" and brief.summary.startswith("Поставка")
    assert [r.finding_id for r in brief.top_risks] == ["k"]  # сначала критичные, чужой номер отброшен
    assert brief.actions == ["Запросить разъяснение."]
    assert "НМЦК: 5 млн" in requests[0]["messages"][-1]["content"]


def test_brief_decisions_without_llm():
    assert package_brief([], use_llm=False).decision == "go"
    assert package_brief(FINDINGS[:1], use_llm=False).decision == "caution"
    brief = package_brief(FINDINGS, use_llm=False, not_checked=1)
    assert brief.decision == "clarify" and "критичных находок — 1" in brief.summary and brief.not_checked == 1


def test_brief_survives_llm_failure():
    client = make_client(lambda r: httpx.Response(500, text="сбой"), llm_max_retries=0)
    assert package_brief(FINDINGS, llm=client).decision == "clarify"


GOOD_RULE = {
    "title": "Оплата после экспертизы", "description": "Оплата откладывается.", "level": "warning",
    "prompt_template": "Проверь {фрагмент}: найди срок оплаты. Риск — только если больше {порог} дней. Не риск: меньше.",
    "variables": [{"name": "порог", "type": "number", "value": "30"}],
    "risk_example": "Оплата в течение 60 дней.", "safe_example": "Оплата в течение 7 дней.",
}


def test_draft_rule_passes_validation():
    client, _ = _reply(GOOD_RULE)
    draft = draft_rule("Заказчик платит слишком поздно", llm=client)
    assert draft.kind == "semantic_llm" and draft.variables[0].name == "порог" and draft.risk_example


def test_draft_rule_repairs_template_once():
    answers = [{**GOOD_RULE, "prompt_template": "Найди срок больше {порог}."}, GOOD_RULE]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _ok(json.dumps(answers.pop(0), ensure_ascii=False))

    draft = draft_rule("Заказчик платит слишком поздно", llm=make_client(handler))
    assert "{фрагмент}" in draft.prompt_template and len(requests) == 2
    assert re.search("ошибки", requests[1]["messages"][-1]["content"])


def test_draft_rule_gives_up_with_message():
    client, _ = _reply({**GOOD_RULE, "prompt_template": "Найди срок."})
    with pytest.raises(LLMResponseError):
        draft_rule("Заказчик платит слишком поздно", llm=client)


def test_fallback_model_answers_after_primary_fails():
    models = []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if body["model"] == "test/model":
            return httpx.Response(500, text="сбой")
        return _ok(json.dumps({"found": False, "answer": "", "citations": []}))

    from ml.analysis.ask import _Answer

    client = make_client(handler, llm_max_retries=0, llm_fallback_model="test/backup")
    assert client.chat([{"role": "user", "content": "?"}], _Answer).data.found is False
    assert models == ["test/model", "test/backup"]


def test_no_fallback_without_setting():
    from ml.analysis.ask import _Answer

    client = make_client(lambda r: httpx.Response(500, text="сбой"), llm_max_retries=0)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "?"}], _Answer)


def test_rule_test_explains_why_rule_stayed_silent():
    from contracts.analysis import RuleSpec
    from ml.analysis.rule_test import test_rule as run_rule_test

    quote = "Оплата в течение 7 рабочих дней"
    rule = RuleSpec(code="R-07", title="Оплата", description="Поздняя оплата", level="warning", kind="llm",
                    prompt_template="Проверь срок оплаты.")
    answer = {"rules": [{"rule_code": "R-07", "findings": [
        {"fragment": 1, "quote": quote, "check": "7 дней при пороге 7", "risk": False, "explanation": "",
         "recommendation": "", "confidence": 0.9}]}]}
    client, _ = _reply(answer)
    result = run_rule_test(rule, text=f"{quote}.", llm=client, embedder=EMBEDDER)
    assert result.findings == []
    assert [(c.quote, c.note) for c in result.considered] == [(quote, "7 дней при пороге 7")]
    assert "риска не нашла" in result.considered[0].reason


def test_invisible_characters_do_not_break_anchor():
    from ml.analysis.anchor import anchor_quote

    blocks = blocks_from_text("Срок​ оплаты 7­ дней﻿.")
    anchor, _ = anchor_quote("Срок оплаты 7 дней", blocks)
    assert anchor is not None and anchor.score == 100
