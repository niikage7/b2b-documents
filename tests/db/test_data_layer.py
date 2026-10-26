"""Слой данных на настоящем PostgreSQL: пакет от черновика до ревью находок."""

from __future__ import annotations

import pytest

from bench.samples import EXPECTED, SUBJECT, make_contract_docx
from contracts.analysis import FindingCandidate, RuleGroupResult
from db import batches, documents, exports, findings, pipeline, rules, users
from db.errors import Conflict, Forbidden, InvalidTransition, ValidationFailed, VersionConflict
from db.seed import seed
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.search import PgVectorSearch
from ml.extract.docx_reader import extract_docx
from ml.structure import build_structure, index_chunks
from tests.fakes import FakeEmbedder, FakeLLM


class Embedder768(FakeEmbedder):
    dim = 768


EMBEDDER = Embedder768()


def _plan() -> dict:
    plan: dict = {}
    for item in EXPECTED:
        if item["doc"] == "contract":
            plan.setdefault(item["rule"], []).append((item["quote"], f"Риск по {item['rule']}"))
    return plan


@pytest.fixture
def world(s, tmp_path):
    """Сид, пакет с одним договором, прошедший весь конвейер с подделкой LLM."""
    seed(s, demo_password="demo-pass")
    user = s.scalar(__import__("sqlalchemy").select(users.User).where(users.User.login == "lawyer"))
    draft = batches.create_draft(s, author_id=user.id)
    files = batches.add_files(s, draft["id"], [
        {"filename": "Закупка_0045.zip", "size_bytes": 1000, "mime": "application/zip", "object_key": "k/zip",
         "check": "ok", "archive_entries": [{"path": "Контракт.docx", "size_bytes": 900, "check": "ok"}]},
    ])
    assert files["total_bytes"] == 1000
    started = batches.start_batch(s, draft["id"], name="Закупка_0045")
    assert started["status"] == "queued"
    upload_id = files["files"][0]["id"]
    doc = pipeline.create_document(s, batch_id=draft["id"], uploaded_file_id=upload_id, filename="Контракт.docx",
                                   original_path="Закупка_0045/Контракт.docx", kind="docx", size_bytes=900,
                                   object_key="k/contract")
    batches.mark_processing(s, draft["id"])
    event = pipeline.set_stage(s, doc.id, "extracting", progress=10)
    assert event["stage_label"] == "Извлечение текста" and event["status"] == "processing"

    result = extract_docx(make_contract_docx(tmp_path / "contract.docx"))
    pipeline.save_extraction_info(s, doc.id, result)
    blocks = build_structure(result, document_id=str(doc.id))
    assert pipeline.save_blocks(s, doc.id, blocks) == len(blocks)
    chunks = index_chunks(blocks, document_id=str(doc.id), embedder=EMBEDDER)
    assert pipeline.save_chunks(s, doc.id, chunks) == len(chunks)
    pipeline.set_batch_meta(s, draft["id"], subject=SUBJECT, meta={"nmck": "4 850 000 руб."})

    specs = pipeline.batch_rules(s, draft["id"])
    assert [r.code for r in specs] == [f"R-0{i}" for i in range(1, 9)] and all(r.version_id for r in specs)
    stored = pipeline.load_blocks(s, doc.id)
    assert [b.id for b in stored] == [b.id for b in blocks]
    search = PgVectorSearch(s.connection().connection.driver_connection)
    llm = FakeLLM(_plan()).client()
    results = [analyze_rule_group(stored, g, subject=SUBJECT, document_id=str(doc.id), search=search, llm=llm,
                                  embedder=EMBEDDER) for g in group_rules(specs)]
    done = pipeline.merge_findings(s, doc.id, results)
    return {"user": user, "batch_id": draft["id"], "doc": doc, "blocks": blocks, "results": results, "done": done}


def test_pipeline_to_findings(s, world):
    done = world["done"]
    expected = sum(1 for e in EXPECTED if e["doc"] == "contract")
    assert done["document"]["worst_level"] == "critical"
    assert done["document"]["findings_summary"]["critical"] + done["document"]["findings_summary"]["warning"] == expected
    assert done["document"]["review"] == {"checked": 0, "total": expected}
    finished = pipeline.finish_batch(s, world["batch_id"])
    assert finished["status"] == "done" and finished["worst_level"] == "critical"
    assert finished["counters"] == {"total": 1, "ok": 0, "warning": 0, "critical": 1, "errors": 0}

    view = documents.document_view(s, world["doc"].id, viewer_id=world["user"].id)
    assert view["status"] == "done" and len(view["blocks"]) == len(world["blocks"])
    assert [f["order"] for f in view["findings"]] == list(range(1, expected + 1))
    for f in view["findings"]:
        block = next(b for b in view["blocks"] if b["id"] == f["block_id"])
        assert block["text"][f["start"]:f["end"]] == f["quote"]  # цитата подсвечивается ровно
        assert f["section_number"] and f["section_title"] and f["page"]  # у каждой находки есть раздел
        assert f["rule"]["code"].startswith("R-") and f["confidence_level"] == "high"
        assert f["recommendation"].startswith("Запросить разъяснение")

    bview = batches.batch_view(s, world["batch_id"])
    assert bview["status"] == "done" and bview["meta"] == {"nmck": "4 850 000 руб."}
    assert bview["documents"][0]["findings"]["critical"] >= 1 and bview["rules_triggered"]
    listing = batches.list_batches(s, user_id=world["user"].id, search="контракт", owner="me")
    assert listing["total"] == 1 and listing["items"][0]["documents_count"] == 1
    assert batches.list_batches(s, level="ok")["total"] == 0

    # Повтор merge_findings не создаёт дублей
    again = pipeline.merge_findings(s, world["doc"].id, world["results"])
    assert again["document"]["review"]["total"] == expected


def test_review_flow_and_conflicts(s, world):
    user = world["user"]
    doc_id = world["doc"].id
    view = documents.document_view(s, doc_id)
    critical = next(f for f in view["findings"] if f["level"] == "critical")

    rejected = findings.update_finding(s, critical["id"], actor_id=user.id, version=1, status="rejected")
    assert rejected["finding"]["status"] == "rejected" and rejected["finding"]["decided_by"]["full_name"]
    assert rejected["document"]["review"]["checked"] == 1
    with pytest.raises(VersionConflict) as exc:
        findings.update_finding(s, critical["id"], actor_id=user.id, version=1, status="accepted")
    assert exc.value.details["current"]["version"] == 2
    with pytest.raises(InvalidTransition):
        findings.update_finding(s, critical["id"], actor_id=user.id, version=2, status="accepted")
    reopened = findings.update_finding(s, critical["id"], actor_id=user.id, version=2, status="new")
    assert reopened["finding"]["decided_by"] is None

    edited = findings.update_finding(s, critical["id"], actor_id=user.id, version=3, level="warning",
                                     comment="Срок согласован с заказчиком")
    assert edited["finding"]["status"] == "edited" and edited["finding"]["level"] == "warning"
    assert edited["finding"]["original_level"] == "critical"
    back = findings.update_finding(s, critical["id"], actor_id=user.id, version=4, status="new")
    assert back["finding"]["level"] == "critical"  # возврат решения возвращает исходный уровень
    history = findings.finding_history(s, critical["id"])["events"]
    assert [e["action"] for e in history] == ["created", "rejected", "reopened", "edited", "reopened"]
    assert history[0]["actor"]["full_name"] == "ИИ"

    # Все критичные отклонены — документ становится жёлтым
    for f in documents.document_view(s, doc_id)["findings"]:
        if f["level"] == "critical":
            out = findings.update_finding(s, f["id"], actor_id=user.id, version=f["version"], status="rejected")
    assert out["document"]["worst_level"] == "warning"
    assert out["batch"]["counters"]["warning"] == 1 and out["batch"]["counters"]["critical"] == 0
    assert out["batch"]["worst_level"] is None  # пакет ещё в работе: finish_batch не вызван


def test_user_findings(s, world):
    user = world["user"]
    other = users.create_user(s, login="other", password="x" * 8, full_name="Пётр Иванов", role="specialist")
    block = next(b for b in world["blocks"] if b.text.startswith("Цена Контракта"))
    with pytest.raises(ValidationFailed) as exc:
        findings.create_user_finding(s, world["doc"].id, actor_id=user.id, block_id=block.id, start=0,
                                     end=len(block.text) + 1, level="warning")
    assert "start" in exc.value.details["fields"]
    made = findings.create_user_finding(s, world["doc"].id, actor_id=user.id, block_id=block.id, start=0, end=14,
                                        level="warning", comment="Проверить НДС")
    f = made["finding"]
    assert f["source"] == "user" and f["status"] == "accepted" and f["quote"] == block.text[:14]
    assert f["can_delete"] and f["author"]["full_name"] == user.full_name
    with pytest.raises(Forbidden):
        findings.delete_user_finding(s, f["id"], actor_id=other.id)

    # Повторный разбор документа с теми же id блоков сохраняет ручную пометку
    pipeline.save_blocks(s, world["doc"].id, world["blocks"])
    assert any(x["id"] == f["id"] for x in documents.document_view(s, world["doc"].id)["findings"])
    findings.delete_user_finding(s, f["id"], actor_id=user.id)


def test_document_search_pages_and_exports(s, world):
    doc_id = world["doc"].id
    found = documents.search_document(s, doc_id, "ПЕНЮ")
    assert found["matches"] and all(m["end"] - m["start"] == 4 for m in found["matches"])
    assert documents.document_blocks(s, doc_id, from_page=1, to_page=1)["blocks"]
    with pytest.raises(ValidationFailed):
        documents.document_blocks(s, doc_id, from_page=1, to_page=40)

    payload = exports.export_payload(s, doc_id)
    assert payload["document"]["kind"] == "docx"
    assert all(item["source_map"]["runs"] for item in payload["findings"])
    summary = exports.batch_summary(s, world["batch_id"])
    assert summary["totals"]["name"] == "Закупка_0045" and summary["documents"][0]["worst_level"] == "Критично"
    assert summary["findings"][0]["status"] == "Новая" and summary["findings"][0]["section_number"]

    export = exports.create_export(s, scope="document", target_id=doc_id, actor_id=world["user"].id)
    assert export["format"] == "docx"
    done = exports.finish_export(s, export["export_id"], object_key="exports/x.docx")
    assert done["status"] == "done" and done["filename"] == "Контракт_разметка.docx"

    pipeline.fail_document(s, doc_id, "corrupted", "Файл повреждён")
    assert documents.retry_document(s, doc_id) == "extracting"
    with pytest.raises(Conflict):
        documents.retry_document(s, doc_id)


def test_rules_crud_and_versions(s, world):
    profile_id = rules.default_profile_id(s)
    base = {"code": "R-09", "title": "Аванс не предусмотрен", "description": "Нет аванса при длинной поставке",
            "level": "warning", "kind": "semantic_llm",
            "prompt_template": "Проверь {фрагмент}: есть ли аванс больше {порог} %.",
            "variables": [{"name": "порог", "type": "number", "value": "0"}]}
    with pytest.raises(ValidationFailed) as exc:
        rules.create_rule(s, base | {"prompt_template": "Проверь {неизвестная}"})
    assert exc.value.details["fields"]["prompt_template"]
    with pytest.raises(ValidationFailed) as exc:
        rules.create_rule(s, base | {"code": "R-01"})
    assert "уже есть" in exc.value.details["fields"]["code"]

    made = rules.create_rule(s, base, actor_id=world["user"].id)
    assert made["version"] == 1 and made["warnings"] == []
    updated = rules.update_rule(s, made["id"], {"level": "critical"}, version=1)
    assert updated["version"] == 2 and updated["level"] == "critical"
    with pytest.raises(VersionConflict):
        rules.update_rule(s, made["id"], {"enabled": False}, version=1)
    assert any(r["code"] == "R-09" for r in rules.list_rules(s, profile_id=profile_id, search="аванс"))

    # Пакет, запущенный до правки, проверяется старыми версиями; новый — новыми
    old_specs = {r.code for r in pipeline.batch_rules(s, world["batch_id"])}
    assert "R-09" not in old_specs
    rules.delete_rule(s, made["id"])
    assert not any(r["code"] == "R-09" for r in rules.list_rules(s))
    hits = {r["code"]: r["hits_30d"] for r in rules.list_rules(s)}
    assert hits["R-04"] >= 1

    new = rules.create_profile(s, name="223-ФЗ, услуги", short_name="223-ФЗ", copy_from_profile_id=profile_id)
    assert new["enabled_rules_count"] == 8 and not new["is_default"]
    rules.update_profile(s, new["id"], is_default=True)
    assert rules.default_profile_id(s) == __import__("uuid").UUID(new["id"])
    with pytest.raises(Conflict):
        rules.delete_profile(s, new["id"])


def test_seed_is_idempotent_and_auth(s):
    first = seed(s, demo_password="demo-pass")
    again = seed(s, demo_password="demo-pass")
    assert first["rules"] == 8 and first["users"] == 3
    assert again == {"profile": False, "rules": 0, "users": 0}
    user = users.authenticate(s, "Lawyer", "demo-pass")
    assert users.me(user)["role_label"] == "Юрист"
    for _ in range(users.MAX_FAILED):
        with pytest.raises(users.AuthError):
            users.authenticate(s, "lawyer", "wrong")
    with pytest.raises(users.TooManyAttempts):
        users.authenticate(s, "lawyer", "demo-pass")


def test_merge_ignores_unknown_blocks(s, world):
    ghost = FindingCandidate(rule_code="R-04", level="critical", block_id="00000000-0000-0000-0000-000000000000",
                             start=0, end=5, quote="xxxxx", explanation="—", confidence=0.5, match_score=100)
    out = pipeline.merge_findings(s, world["doc"].id, [RuleGroupResult(candidates=[ghost])])
    assert out["document"]["worst_level"] == "ok"
