from __future__ import annotations

import argparse
import json

import httpx
import pytest

from bench.pipeline import _modes, _totals
from contracts.analysis import FindingCandidate, LLMGroupResponse, LLMUsage, RuleSpec, RuleVariable
from ml.analysis.anchor import anchor_quote, prepare_quote
from ml.analysis.blocks import (
    Chunk,
    SimpleBlock,
    blocks_from_text,
    chunk_blocks,
    estimate_tokens,
    excerpt,
)
from ml.analysis.cache import DirCache
from ml.analysis.llm import reasoning_effort
from ml.analysis.search import LexicalIndex, PgVectorSearch, lexical_terms, rank_chunks, take_budget
from ml.analysis.templates import render_template, rule_query_text, rule_search_text, validate_rule
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
    long = [SimpleBlock(f"b{i}", i, "слово " * 150) for i in range(5)]
    chunks = chunk_blocks(long, max_tokens=400, overlap=1)
    assert all(len(c.block_ids) <= 2 for c in chunks)
    assert chunks[1].block_ids[0] == chunks[0].block_ids[-1]  # перекрытие в один блок
    # По умолчанию — без перекрытия: длинный блок — свой фрагмент, короткие склеиваются
    short = [SimpleBlock("h", 0, "Ответственность сторон", number="7.", section_title="Ответственность сторон"),
             SimpleBlock("p1", 1, "Пеня начисляется за каждый день просрочки.", number="7.1.",
                         section_title="Ответственность сторон"),
             SimpleBlock("p2", 2, "слово " * 60, number="7.2.", section_title="Ответственность сторон")]
    chunks = chunk_blocks(short, max_tokens=80, overlap=0)
    assert [c.block_ids for c in chunks] == [["h", "p1"], ["p2"]]
    # Название раздела в начале фрагмента, если фрагмент начинается не с заголовка
    assert chunks[0].text.startswith("7. Ответственность") and chunks[1].text.startswith("Ответственность сторон\n7.2.")


def test_long_block_is_split_into_sentence_windows():
    text = "".join(f"Требование номер {i} к содержанию заявки участника закупки. " for i in range(60))
    cell = SimpleBlock("cell", 0, text.strip())
    chunks = chunk_blocks([SimpleBlock("a", 1, "Короткий пункт."), cell, SimpleBlock("b", 2, "Ещё пункт.")],
                          max_tokens=60)
    windows = [c for c in chunks if c.block_ids == ["cell"]]
    assert [c.block_ids for c in chunks if c.start is None] == [["a"], ["b"]]  # окна не склеиваются с соседями
    assert len(windows) > 5 and all(estimate_tokens(c.text) <= 60 for c in windows)
    assert all(c.text == cell.text[c.start : c.end] and c.text.endswith(".") for c in windows)  # по предложениям
    assert excerpt(cell.text, [(windows[3].start, windows[3].end), (windows[0].start, windows[0].end)]) == (
        f"{windows[0].text} … {windows[3].text} …")
    assert excerpt(cell.text, None) == cell.text


def test_lexical_index_and_hybrid_rank():
    texts = [
        "Поставщик уплачивает пеню в размере 1 % цены контракта за каждый день просрочки.",
        "Стороны обмениваются документами в единой информационной системе.",
        "Размер обеспечения исполнения контракта составляет 30 % начальной цены.",
    ]
    assert lexical_terms("Обеспечения исполнения ёмкость и в") == ["обесп", "испол", "емкос"]
    scores = LexicalIndex(texts).scores("обеспечение исполнения контракта")
    assert max(range(3), key=lambda i: scores[i]) == 2 and scores[1] == 0
    chunks = [Chunk(f"c{i}", [f"b{i}"], t, embedding=v) for i, (t, v) in
              enumerate(zip(texts, ([1.0, 0.0], [0.9, 0.1], [0.0, 1.0]), strict=True))]
    # Вектор указывает на c0, слова — на c2: гибрид ставит их выше c1, у которого нет ни того, ни другого
    hybrid = rank_chunks(chunks, [1.0, 0.0], "обеспечение исполнения", mode="hybrid")
    assert {h.chunk_id for h in hybrid[:2]} == {"c0", "c2"} and hybrid[-1].chunk_id == "c1"
    assert rank_chunks(chunks, [1.0, 0.0], "обеспечение", mode="dense")[0].chunk_id == "c0"
    assert rank_chunks(chunks, [1.0, 0.0], "обеспечение", mode="lexical")[0].chunk_id == "c2"
    # Бюджет: первый фрагмент всегда, дальше — пока укладываются
    assert len(take_budget(hybrid, 1)) == 1 and len(take_budget(hybrid, 10_000)) == 3
    assert len(take_budget(hybrid, estimate_tokens(hybrid[0].text) + estimate_tokens(hybrid[1].text))) == 2


def test_rule_search_text_includes_instruction():
    r = rule(prompt_template="Найди в {фрагмент} пени больше {порог} % для {предмет_закупки}.",
             variables=[RuleVariable(name="порог", type="number", value="10")])
    text = rule_search_text(r)
    assert text.startswith(rule_query_text(r)) and "пени больше 10 %" in text
    assert "{" not in text and "фрагмент" not in text


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
            return [("c1", ["b1", "b2"], "текст", "[0.1,0.2]", None, None), ("c2", ["b3"], "без вектора", None, 5, 40)]

    cursor = Cursor()

    class Conn:
        def cursor(self):
            return cursor

    chunks = PgVectorSearch(Conn()).chunks("doc-1")
    sql, params = cursor.executed
    assert "WHERE document_id = %(document_id)s" in sql and params == {"document_id": "doc-1"}
    assert chunks[0].block_ids == ["b1", "b2"] and chunks[0].embedding == [0.1, 0.2]
    assert chunks[1].embedding is None and (chunks[1].start, chunks[1].end) == (5, 40)
    hits = rank_chunks(chunks, [0.1, 0.2], "текст")
    assert hits[0].chunk_id == "c1" and (hits[1].start, hits[1].end) == (5, 40)
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


# --- рассуждения модели -----------------------------------------------------------------


def _reasoning_sent(llm_reasoning: str, **overrides) -> dict | None:
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ok(ANSWER)

    make_client(handler, llm_reasoning=llm_reasoning, **overrides).chat(MESSAGES, LLMGroupResponse)
    return seen[0].get("reasoning")


def test_reasoning_setting_values():
    assert reasoning_effort("") is None and reasoning_effort(" Default ") is None
    assert reasoning_effort("NONE") == "none"
    with pytest.raises(LLMError) as exc:
        reasoning_effort("fast")
    assert "LLM_REASONING" in exc.value.detail


def test_llm_reasoning_defaults_to_none(monkeypatch):
    monkeypatch.delenv("LLM_REASONING", raising=False)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ok(ANSWER)

    make_client(handler).chat(MESSAGES, LLMGroupResponse)
    assert seen[0]["reasoning"] == {"effort": "none"}


def test_llm_reasoning_param():
    assert _reasoning_sent("") is None
    assert _reasoning_sent("none") == {"effort": "none"}
    assert _reasoning_sent("low") == {"effort": "low", "exclude": True}
    # vLLM и другие API, совместимые с OpenAI, параметр не получают
    assert _reasoning_sent("none", llm_base_url="http://localhost:8000/v1") is None


def test_llm_reasoning_per_call():
    for base_url, expected in (("https://openrouter.ai/api/v1", {"effort": "none"}), ("http://localhost:8000/v1", None)):
        seen = []

        def handler(request, seen=seen):
            seen.append(json.loads(request.content))
            return _ok(ANSWER)

        client = make_client(handler, llm_reasoning="low", llm_base_url=base_url)
        client.chat(MESSAGES, LLMGroupResponse, reasoning="none")
        client.chat(MESSAGES, LLMGroupResponse)
        assert seen[0].get("reasoning") == expected
        # Настройка клиента не меняется: следующий вызов — снова с LLM_REASONING
        assert seen[1].get("reasoning") == ({"effort": "low", "exclude": True} if expected else None)


def test_llm_counts_reasoning_tokens():
    usage = {"prompt_tokens": 100, "completion_tokens": 320, "completion_tokens_details": {"reasoning_tokens": 300}}
    client = make_client(lambda r: _ok(ANSWER, usage))
    first = client.chat(MESSAGES, LLMGroupResponse).usage
    assert (first.completion_tokens, first.reasoning_tokens) == (320, 300)
    total = LLMUsage()
    total.add(first)
    total.add(first)
    assert total.reasoning_tokens == 600


@pytest.mark.parametrize("error", [
    (400, '{"error": "reasoning effort none is not supported: this model always thinks"}'),
    (404, '{"error": "No endpoints found that can handle the requested parameters"}'),
])
def test_llm_drops_reasoning_when_rejected(error):
    model = f"test/always-thinks-{error[0]}"
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((body.get("reasoning"), body["response_format"]["type"]))
        return httpx.Response(error[0], text=error[1]) if "reasoning" in body else _ok(ANSWER)

    client = make_client(handler, llm_model=model, llm_reasoning="none")
    assert client.chat(MESSAGES, LLMGroupResponse).data.rules
    client.chat(MESSAGES, LLMGroupResponse)
    # Формат ответа не понижается: отказ из-за reasoning, а не из-за json_schema
    assert sent == [({"effort": "none"}, "json_schema"), (None, "json_schema"), (None, "json_schema")]
    assert client.reasoning_active is None


def test_llm_cache_separates_reasoning(tmp_path):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content).get("reasoning"))
        return _ok(ANSWER)

    for mode in ("", "none", "none"):
        client = make_client(handler, llm_reasoning=mode)
        client.cache = DirCache(tmp_path / "llm")
        client.chat(MESSAGES, LLMGroupResponse)
    assert calls == [None, {"effort": "none"}]


def test_bench_reasoning_modes_and_totals():
    assert _modes("default, NONE") == ["default", "none"]
    with pytest.raises(argparse.ArgumentTypeError):
        _modes("default,fast")
    found = {"contract": [FindingCandidate(rule_code="R-04", level="critical", block_id="b1", start=0, end=20,
                                           quote="срок поставки 3 дня", explanation="", confidence=0.9,
                                           match_score=100)]}
    expected = [{"rule": "R-04", "doc": "contract", "quote": "срок поставки 3 дня"},
                {"rule": "R-05", "doc": "contract", "quote": "аванс не предусмотрен"}]
    assert _totals(found, expected) == ("1 из 2", "1 из 1")
