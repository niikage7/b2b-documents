"""Анализ по правилам в конвейере: предмет закупки, chord групп правил, находки, паспорт, тест правила.

LLM — подделка `FakeLLM` (фикстура `fake_llm`): находит заложенные риски синтетического пакета
`bench.samples`, только если цитата есть в запросе. Сеть и настоящий LLM не используются.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from bench import samples
from db import batches, findings
from db.models import Batch, BlockRow, Document, Finding, TaskRun
from tests.fakes import FakeLLM, make_client
from tests.worker.conftest import make_batch, sample_bytes
from tests.worker.test_pipeline import docs_of, run_batch
from worker import runtime, tasks


def risk_plan(doc: str) -> dict[str, list[tuple[str, str]]]:
    plan: dict[str, list[tuple[str, str]]] = {}
    for item in samples.EXPECTED:
        if item["doc"] == doc:
            plan.setdefault(item["rule"], []).append((item["quote"], f"Риск по {item['rule']}"))
    return plan


@pytest.fixture
def contract():
    return sample_bytes(samples.make_contract_docx, ".docx")


@pytest.fixture
def notice():
    return sample_bytes(samples.make_notice_docx, ".docx")


def ai_findings(s, document_id) -> list[Finding]:
    s.expire_all()
    return list(s.scalars(select(Finding).where(Finding.document_id == document_id, Finding.source == "ai")))


def stages(s, batch_id) -> list[str]:
    return list(s.scalars(select(TaskRun.stage).where(TaskRun.batch_id == batch_id)))


def schema_names(fake: FakeLLM) -> list[str]:
    return [r.get("response_format", {}).get("json_schema", {}).get("name") for r in fake.requests]


def test_batch_gets_findings_colors_subject_and_passport(s, env, author, fake_llm, contract, notice):
    fake_llm.plan = risk_plan("contract") | risk_plan("notice")
    fake_llm.passport = {"nmck": ("4 850 000,00 руб.", "4 850 000,00")}
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)

    docs = docs_of(s, batch_id)
    contract_doc, notice_doc = docs["Контракт.docx"], docs["Извещение.docx"]
    for doc in docs.values():
        assert (doc.status, doc.stage, doc.progress) == ("done", "done", 100), doc.error_message

    found = ai_findings(s, contract_doc.id)
    codes = {f.quote: f for f in found}
    assert len(found) >= 8
    for item in samples.EXPECTED:
        if item["doc"] == "contract":
            assert any(item["quote"] in q for q in codes), item
    # Цитата — ровно текст блока по смещениям, раздел даёт блок
    for f in found:
        block = s.get(BlockRow, f.block_id)
        assert block.text[f.start:f.end] == f.quote
        assert f.level in ("critical", "warning") and f.explanation and f.status == "new"
    assert {f.level for f in found} == {"critical", "warning"}
    assert ai_findings(s, notice_doc.id)  # R-06 — в извещении

    # Цвет — худший уровень, у пакета — худший среди документов
    assert contract_doc.worst_level == "critical"
    view = batches.batch_view(s, batch_id)
    assert view["status"] == "done" and view["worst_level"] == "critical"
    assert view["counters"]["critical"] >= 1 and view["findings"]
    assert {r["code"] for r in view["rules_triggered"]} >= {"R-02", "R-03", "R-06"}

    # Предмет закупки — шаблоном из извещения (без LLM), паспорт — одним вызовом
    assert view["subject"] == samples.SUBJECT
    assert "batch_meta" not in schema_names(fake_llm)
    assert schema_names(fake_llm).count("procurement_passport") == 1
    assert [f["key"] for f in view["meta"]["passport"]] == ["nmck"]

    # Группы правил: 7 правил поиска по 4 и одно правило «Проверка ИИ» — у каждого документа свои вызовы
    run = stages(s, batch_id)
    assert run.count("analyze_rule_group") == 6 and run.count("merge_findings") == 2
    assert {"extract_batch_meta", "extract_passport", "analyze_document", "finalize_batch"} <= set(run)
    assert not s.scalar(select(TaskRun.id).where(TaskRun.batch_id == batch_id, TaskRun.status != "done"))
    # Кандидаты групп лежат рядом с исходниками пакета
    key = runtime.work_key(batch_id, str(contract_doc.id), "analysis/group-01.json")
    assert key in env.objects


def test_subject_goes_into_every_rule_prompt(s, env, author, fake_llm, contract, notice):
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)
    prompts = [r["messages"][-1]["content"] for r in fake_llm.requests
               if r["response_format"]["json_schema"]["name"] == "risk_findings"]
    assert prompts and all(f"Предмет закупки: {samples.SUBJECT}" in p for p in prompts)


def test_invented_quote_is_dropped(s, env, author, fake_llm, contract):
    fake_llm.invent = {"R-03": ["Поставщик платит неустойку 50 % в день"]}
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert doc.status == "done" and ai_findings(s, doc.id) == []
    assert doc.worst_level == "ok"  # находок нет — «Без рисков»


def test_rerun_replaces_ai_findings_and_keeps_manual(s, env, author, fake_llm, contract):
    fake_llm.plan = risk_plan("contract")
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert ai_findings(s, doc.id)
    block = s.scalars(select(BlockRow).where(BlockRow.document_id == doc.id, BlockRow.text != "")).first()
    findings.create_user_finding(s, doc.id, actor_id=author.id, block_id=block.id, start=0, end=5, level="warning")

    fake_llm.plan, fake_llm.requests = {}, []
    ids = batches.rerun_batch(s, batch_id)
    tasks.rerun_batch(batch_id, ids)

    s.expire_all()
    assert ai_findings(s, doc.id) == []
    manual = list(s.scalars(select(Finding).where(Finding.document_id == doc.id, Finding.source == "user")))
    assert len(manual) == 1
    doc = s.get(Document, doc.id)
    assert doc.status == "done" and doc.worst_level == "warning"
    assert s.get(Batch, doc.batch_id).status == "done"
    # Повторный анализ не извлекает текст заново
    assert stages(s, batch_id).count("extract_text") == 1 and stages(s, batch_id).count("merge_findings") == 2


def test_rerun_keeps_subject_and_passport(s, env, author, fake_llm, contract, notice):
    fake_llm.passport = {"nmck": ("4 850 000,00 руб.", "4 850 000,00")}
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)
    fake_llm.requests = []
    tasks.rerun_batch(batch_id, batches.rerun_batch(s, batch_id))
    assert set(schema_names(fake_llm)) == {"risk_findings"}  # предмет и паспорт уже есть
    assert s.get(Batch, batch_id).status == "done"


def test_llm_failure_fails_document_not_batch(s, env, author, contract, notice, monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr(runtime, "get_llm", lambda: make_client(down, llm_max_retries=0))
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)

    for doc in docs_of(s, batch_id).values():
        assert (doc.status, doc.error_code) == ("error", "llm_unavailable")
        assert doc.error_message == "Сервис проверки ИИ временно недоступен"
    batch = s.get(Batch, doc.batch_id)
    assert batch.status == "partial_error"
    assert batch.subject == samples.SUBJECT  # предмет найден шаблоном, LLM ему не нужна
    run = s.scalars(select(TaskRun).where(TaskRun.batch_id == batch_id, TaskRun.stage == "analyze_rule_group",
                                          TaskRun.status == "error")).first()
    assert run is not None and "llm_unavailable" in run.error


def test_analysis_progress_counts_rules(s, env, author, contract):
    from worker.progress import report_progress, rules_done

    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    report_progress(str(doc.id), "analysis", 80, {"rules_done": 0, "rules_total": 8})
    events = [rules_done(str(doc.id), 8, n, start=80, span=18) for n in (4, 3, 1, 4)]
    assert [e["stage_detail"]["rules_done"] for e in events] == [4, 7, 8, 8]
    assert events[0]["stage_label"] == "Анализ по правилам, 4 из 8" and events[-1]["progress"] == 98


def r04() -> dict:
    from ml.checklist import load_default_checklist

    _, rules = load_default_checklist()
    rule = next(r for r in rules if r.code == "R-04")
    return rule.model_dump(mode="json", exclude={"id", "version_id", "query_embedding"})


def test_rule_test_on_text(env, fake_llm):
    quote = "в течение 2 (двух) рабочих дней с даты заявки"
    fake_llm.plan = {"R-04": [(quote, "Слишком короткий срок")]}
    out = tasks.rule_test.apply(args=({"rule": r04(), "text": f"Поставка товара осуществляется {quote}."},)).get()
    finding = out["result"]["findings"][0]
    assert finding["quote"] == quote and finding["level"] == "critical"


def test_rule_test_on_document(s, env, author, fake_llm, contract):
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    fake_llm.plan = risk_plan("contract")
    out = tasks.rule_test.apply(args=({"rule": r04(), "document_id": str(doc.id)},)).get()
    assert [f["quote"] for f in out["result"]["findings"]] == [
        "в течение 3 (трёх) календарных дней с даты заключения Контракта"]
    block = s.get(BlockRow, out["result"]["findings"][0]["block_id"])
    assert block.document_id == doc.id


def test_rule_test_returns_ml_error(env):
    out = tasks.rule_test.apply(args=({"rule": r04(), "text": "а" * 20_001},)).get()
    assert out == {"error": {"code": "text_too_long", "message": "Текст для проверки длиннее 20 000 символов",
                             "retryable": False}}
