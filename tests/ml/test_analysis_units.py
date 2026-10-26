from __future__ import annotations

import json

import httpx
import pytest

from contracts.analysis import LLMGroupResponse, RuleSpec, RuleVariable
from ml.analysis.anchor import anchor_quote, prepare_quote
from ml.analysis.blocks import SimpleBlock, blocks_from_text, chunk_blocks
from ml.analysis.cache import DirCache
from ml.analysis.search import PgVectorSearch
from ml.analysis.templates import render_template, rule_query_text, validate_rule
from ml.checklist import load_default_checklist
from ml.errors import LLMError, LLMResponseError, RuleTemplateError
from tests.fakes import _ok, make_client


def rule(**kw) -> RuleSpec:
    base = dict(code="R-04", title="Срок поставки короче разумного", description="Срок меньше {порог} дней",
                level="critical", kind="semantic_llm",
                prompt_template="Оцени {фрагмент} для {предмет_закупки}, порог {порог} дней",
                variables=[RuleVariable(name="порог", type="number", value="10")])
    base.update(kw)
    return RuleSpec(**base)


# --- шаблоны ----------------------------------------------------------------------------


def test_validate_rule_ok_and_warnings():
    errors, warnings = validate_rule(rule())
    assert errors == [] and warnings == []
    r = rule(variables=[RuleVariable(name="порог", type="number", value="10"),
                        RuleVariable(name="лишняя", type="text", value="x")])
    assert validate_rule(r)[1] == ["Переменная {лишняя} описана, но не используется в инструкции"]


@pytest.mark.parametrize(
    ("changes", "field", "text"),
    [
        ({"prompt_template": "Оцени {фрагмент}, порог {срок}"}, "prompt_template", "Переменная {срок} не описана"),
        ({"prompt_template": "Оцени срок {порог}"}, "prompt_template", "{фрагмент}"),
        ({"variables": [RuleVariable(name="порог", type="number", value="десять")]}, "variables[0].value", "число"),
        ({"variables": [RuleVariable(name="фрагмент", type="text", value="x")]}, "variables[0].name", "системной"),
        ({"variables": [RuleVariable(name="порог", type="list", value=" ; ")]}, "variables[0].value", "точку с запятой"),
    ],
)
def test_validate_rule_errors(changes, field, text):
    errors, _ = validate_rule(rule(**changes))
    assert any(e.field == field and text in e.message for e in errors), errors


def test_render_template_and_query():
    r = rule(prompt_template="{фрагмент}; {предмет_закупки}; {порог}; {список}",
             variables=[RuleVariable(name="порог", type="number", value="10"),
                        RuleVariable(name="список", type="list", value="НДС; аванс ;гарантия")])
    assert render_template(r.prompt_template, r, fragment="[1]", subject=None) == "[1]; не указан; 10; НДС, аванс, гарантия"
    assert rule_query_text(r) == "Срок поставки короче разумного. Срок меньше 10 дней"
    with pytest.raises(RuleTemplateError):
        render_template("{неизвестно}", r, fragment="", subject=None)


def test_default_checklist_is_valid():
    profile, rules = load_default_checklist()
    assert profile["is_default"] and [r.code for r in rules] == [f"R-0{i}" for i in range(1, 9)]
    for r in rules:
        errors, warnings = validate_rule(r)
        assert errors == [] and warnings == [], (r.code, errors, warnings)
    assert sum(r.kind == "llm" for r in rules) <= 5


# --- привязка цитат -----------------------------------------------------------------------

BLOCK = SimpleBlock("b1", 0, "Поставка Товара осуществляется в течение 3 (трёх) календарных дней с даты "
                             "заключения Контракта. Досрочная поставка допускается.")


@pytest.mark.parametrize(
    "quote",
    [
        "Поставка Товара осуществляется в течение 3 (трёх) календарных дней",
        "поставка товара осуществляется в течение 3 (трех) календарных дней",  # регистр и «ё»
        "3.1. Поставка Товара осуществляется в течение 3 (трёх) календарных дней",  # номер пункта
        "«Поставка Товара осуществляется в течение 3 (трёх)  календарных дней»",  # кавычки и пробелы
        "Пoставка Тoвара осуществляется в течение 3 (трёх) календарных дней",  # латинская «o»
        "Поставка Товара осуществляеться в течении 3 (трёх) календарных дней",  # опечатки модели
    ],
)
def test_anchor_variants(quote):
    anchor, score = anchor_quote(quote, [BLOCK])
    assert anchor is not None, score
    assert BLOCK.text[anchor.start : anchor.end].startswith("Поставка Товара осуществляется")
    assert BLOCK.text[anchor.start : anchor.end].endswith("календарных дней")


def test_anchor_rejects_invented_and_short_quotes():
    assert anchor_quote("Поставщик уплачивает штраф 50 % цены контракта", [BLOCK])[0] is None
    assert anchor_quote("дня", [BLOCK])[0] is None


def test_anchor_ellipsis_and_snapping():
    assert prepare_quote("Поставка Товара … календарных дней с даты") == "календарных дней с даты"
    anchor, _ = anchor_quote("ставка Товара осуществляется в теч", [BLOCK])
    assert BLOCK.text[anchor.start : anchor.end] == "Поставка Товара осуществляется в течение"


def test_anchor_hyphen_from_pdf():
    block = SimpleBlock("b2", 0, "Порядок сдачи-\nприемки товара определён в разделе 4.")
    anchor, _ = anchor_quote("Порядок сдачи-приемки товара", [block])
    assert anchor is not None and block.text[anchor.start : anchor.end] == "Порядок сдачи-\nприемки товара"


def test_anchor_prefers_fragment_and_uses_exact_fallback():
    other = SimpleBlock("b9", 9, "Оплата производится в течение 30 рабочих дней.")
    anchor, _ = anchor_quote("Оплата производится в течение 30 рабочих дней", [BLOCK], fallback=[other])
    assert anchor.block.id == "b9"


# --- блоки и поиск --------------------------------------------------------------------


def test_blocks_from_text_and_chunks():
    blocks = blocks_from_text("1. Общие положения\n1.1. Текст пункта.\n\nАбзац без номера.\nа) подпункт")
    assert [(b.number, b.text) for b in blocks] == [
        ("1.", "Общие положения"), ("1.1.", "Текст пункта."), (None, "Абзац без номера."), ("а)", "подпункт")]
    long = [SimpleBlock(f"b{i}", i, "слово " * 200) for i in range(5)]
    chunks = chunk_blocks(long, max_tokens=400)
    assert all(len(c.block_ids) <= 2 for c in chunks)
    assert chunks[1].block_ids[0] == chunks[0].block_ids[-1]  # перекрытие в один блок


def test_pgvector_search_sql():
    class Cursor:
        def __init__(self):
            self.executed = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            self.executed = (sql, params)

        def fetchall(self):
            return [("c1", ["b1", "b2"], "текст", 0.91)]

    cursor = Cursor()

    class Conn:
        def cursor(self):
            return cursor

    hits = PgVectorSearch(Conn()).search("doc-1", [0.1, 0.2], 8)
    sql, params = cursor.executed
    assert "WHERE document_id = %(document_id)s" in sql and "LIMIT %(k)s" in sql
    assert params == {"document_id": "doc-1", "vector": "[0.1,0.2]", "k": 8}
    assert hits[0].block_ids == ["b1", "b2"] and hits[0].score == 0.91
    with pytest.raises(ValueError):
        PgVectorSearch(Conn(), table="chunk; drop table x")


# --- адаптер LLM ------------------------------------------------------------------------

ANSWER = json.dumps({"rules": [{"rule_code": "R-04", "findings": []}]})
MESSAGES = [{"role": "user", "content": "x"}]


def test_llm_json_schema_request_and_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ok(ANSWER)

    result = make_client(handler).chat(MESSAGES, LLMGroupResponse, schema_name="risk_findings")
    assert result.data.rules[0].rule_code == "R-04"
    fmt = seen[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert seen[0]["provider"] == {"require_parameters": True}
    assert (result.usage.calls, result.usage.prompt_tokens) == (1, 100)


def test_llm_downgrades_format_when_rejected():
    formats = []

    def handler(request):
        body = json.loads(request.content)
        formats.append(body.get("response_format", {}).get("type"))
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, text='{"error": "response_format json_schema is not supported"}')
        return _ok("```json\n" + ANSWER + "\n```")

    client = make_client(handler, llm_model="test/no-schema")
    assert client.chat(MESSAGES, LLMGroupResponse).data.rules
    assert formats == ["json_schema", "json_object"]
    client.chat(MESSAGES, LLMGroupResponse)
    assert formats[-1] == "json_object"  # запомнили для модели


def test_llm_retries_on_429_and_5xx(monkeypatch):
    monkeypatch.setattr("ml.analysis.llm.time.sleep", lambda s: None)
    codes = iter([429, 503])

    def handler(request):
        code = next(codes, 200)
        return _ok(ANSWER) if code == 200 else httpx.Response(code, text="busy")

    assert make_client(handler).chat(MESSAGES, LLMGroupResponse).usage.calls == 1


def test_llm_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr("ml.analysis.llm.time.sleep", lambda s: None)
    with pytest.raises(LLMError):
        make_client(lambda r: httpx.Response(502, text="bad gateway")).chat(MESSAGES, LLMGroupResponse)


def test_llm_repairs_invalid_json_once():
    answers = iter(['{"rules": [{"rule_code": "R-04", "findings": [{"quote": 1}]}]}', ANSWER])
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return _ok(next(answers))

    result = make_client(handler).chat(MESSAGES, LLMGroupResponse)
    assert result.data.rules[0].findings == [] and result.usage.calls == 2
    assert "не соответствует схеме" in calls[1]["messages"][-1]["content"]
    with pytest.raises(LLMResponseError):
        make_client(lambda r: _ok("не JSON")).chat(MESSAGES, LLMGroupResponse)


def test_llm_auth_error_is_readable():
    with pytest.raises(LLMError) as exc:
        make_client(lambda r: httpx.Response(401, text="no key")).chat(MESSAGES, LLMGroupResponse)
    assert "ключ API" in exc.value.message


def test_llm_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return _ok(ANSWER)

    client = make_client(handler)
    client.cache = DirCache(tmp_path / "llm")
    first = client.chat(MESSAGES, LLMGroupResponse)
    second = client.chat(MESSAGES, LLMGroupResponse)
    assert len(calls) == 1 and second.cached and second.usage.cached_calls == 1
    assert second.data == first.data
