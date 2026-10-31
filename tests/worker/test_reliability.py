"""События пакета и надёжность конвейера (FR-2.2, FR-2.3): Celery eager, шина событий в памяти, без сети и LLM.

Повторы в eager выполняются сразу, без паузы; пауза и число попыток проверяются отдельно.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import ml
from bench import samples
from db import documents
from db.models import Batch, Document, TaskRun
from ml.errors import ConversionError, CorruptedFileError
from tests.worker.conftest import make_batch, sample_bytes
from tests.worker.test_pipeline import docs_of, run_batch
from worker import tasks

DONE_KEYS = {"document_id", "pages", "worst_level", "findings", "review", "ocr_low_quality_pages"}
PROGRESS_KEYS = {"document_id", "status", "stage", "stage_label", "stage_detail", "next_stage_label", "progress"}


@pytest.fixture
def contract():
    return sample_bytes(samples.make_contract_docx, ".docx")


@pytest.fixture
def notice():
    return sample_bytes(samples.make_notice_docx, ".docx")


def runs(s, document_id, stage: str) -> list[TaskRun]:
    s.expire_all()
    return list(s.scalars(select(TaskRun).where(TaskRun.document_id == document_id, TaskRun.stage == stage)
                          .order_by(TaskRun.attempt)))


def flaky(real, failures: list[BaseException]):
    """Первые вызовы падают с ошибками из списка, дальше — настоящая функция."""

    def call(*args, **kwargs):
        if failures:
            raise failures.pop(0)
        return real(*args, **kwargs)

    return call


def test_batch_events_follow_pipeline(s, env, bus, author, contract, notice):
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)

    events = bus.events(batch_id)
    types = [t for _, t, _ in events]
    assert types[0] == "batch.snapshot" and types[-1] == "batch.done"
    assert {"document.progress", "batch.progress", "batch.meta", "document.done"} <= set(types)
    ids = [e[0] for e in events]
    assert ids == sorted(ids, key=lambda i: tuple(map(int, i.split("-")))) and len(set(ids)) == len(ids)

    snapshot = events[0][2]
    assert snapshot["id"] == batch_id and len(snapshot["documents"]) == 2  # документы уже созданы распаковкой
    progress = [d for _, t, d in events if t == "document.progress"]
    assert all(set(p) == PROGRESS_KEYS for p in progress)
    assert any(p["stage"] == "analysis" and p["stage_label"].startswith("Анализ по правилам,") for p in progress)
    done = [d for _, t, d in events if t == "document.done"]
    docs = docs_of(s, batch_id)
    assert {d["document_id"] for d in done} == {str(d.id) for d in docs.values()}
    assert all(set(d) == DONE_KEYS for d in done)
    assert [d for _, t, d in events if t == "batch.meta"][0] == {"subject": samples.SUBJECT}
    final = events[-1][2]
    assert final["status"] == "done" and set(final) == {"status", "worst_level", "counters", "review"}
    assert final["counters"]["total"] == 2
    batch_progress = [d for _, t, d in events if t == "batch.progress"]
    assert batch_progress and set(batch_progress[-1]) == {"status", "progress", "eta_seconds"}
    assert bus.dead_letters() == []


def test_transient_error_is_retried(s, env, bus, author, contract, monkeypatch):
    monkeypatch.setattr(ml, "build_structure", flaky(ml.build_structure, [ConnectionError("minio reset")]))
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)

    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert doc.status == "done"
    attempts = runs(s, doc.id, "build_structure")
    assert [(r.attempt, r.status) for r in attempts] == [(1, "error"), (2, "done")]
    assert "internal_error" in attempts[0].error
    assert bus.dead_letters() == [] and "document.error" not in bus.types(batch_id)


def test_retryable_ml_error_exhausts_attempts(s, env, bus, author, contract, monkeypatch):
    def broken(*a, **k):
        raise ConversionError()

    monkeypatch.setattr(ml, "build_structure", broken)
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)

    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert (doc.status, doc.error_code) == ("error", ConversionError.code)
    assert [r.status for r in runs(s, doc.id, "build_structure")] == ["error"] * tasks.MAX_ATTEMPTS
    assert s.get(Batch, doc.batch_id).status == "partial_error"  # chord пакета дошёл до finalize_batch
    [dead] = bus.dead_letters()
    assert dead["task"] == "worker.tasks.build_structure" and dead["attempts"] == tasks.MAX_ATTEMPTS
    assert dead["document_id"] == str(doc.id) and dead["batch_id"] == batch_id
    [error] = [d for _, t, d in bus.events(batch_id) if t == "document.error"]
    assert error == {"document_id": str(doc.id), "retryable": True,
                     "error": {"code": ConversionError.code, "message": ConversionError.default_message}}
    assert bus.types(batch_id)[-1] == "batch.done"


def test_user_error_is_not_retried_or_dead_lettered(s, env, bus, author, contract, monkeypatch):
    def corrupted(*a, **k):
        raise CorruptedFileError()

    monkeypatch.setattr(ml, "extract_docx", corrupted)
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert (doc.status, doc.error_code) == ("error", "corrupted")
    assert len(runs(s, doc.id, "extract_text")) == 1
    assert bus.dead_letters() == []
    [error] = [d for _, t, d in bus.events(batch_id) if t == "document.error"]
    assert error["retryable"] is False


def test_internal_error_goes_to_dead_letter_once(s, env, bus, author, contract, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("неожиданный сбой")

    monkeypatch.setattr(ml, "index_chunks", boom)
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert (doc.status, doc.error_code) == ("error", "internal_error")
    assert len(runs(s, doc.id, "index_chunks")) == 1  # внутренняя ошибка не проходит от повтора
    [dead] = bus.dead_letters()
    assert dead["stage"] == "index_chunks" and "неожиданный сбой" in dead["error"]


def test_retry_document_from_failed_text_stage(s, env, bus, author, contract, notice, monkeypatch):
    real = ml.build_structure
    monkeypatch.setattr(ml, "build_structure", flaky(real, [RuntimeError("сбой")]))
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract), ("Извещение.docx", notice)])
    run_batch(batch_id)
    docs = docs_of(s, batch_id)
    failed = next(d for d in docs.values() if d.status == "error")
    assert s.get(Batch, failed.batch_id).status == "partial_error"

    stage = tasks.restart_stage(s, failed.id)
    assert stage == "build_structure"
    documents.retry_document(s, failed.id)
    tasks.retry_document(batch_id, str(failed.id), stage)

    doc = docs_of(s, batch_id)[failed.original_path]
    assert (doc.status, doc.error_code) == ("done", None)
    assert len(runs(s, doc.id, "extract_text")) == 1  # текст не извлекался заново
    assert [r.status for r in runs(s, doc.id, "build_structure")] == ["error", "done"]
    assert s.get(Batch, doc.batch_id).status == "done"
    assert bus.types(batch_id)[-1] == "batch.done"


def test_retry_document_from_failed_analysis(s, env, bus, author, contract, monkeypatch):
    real = ml.analyze_rule_group
    monkeypatch.setattr(ml, "analyze_rule_group", flaky(real, [RuntimeError("сбой")]))
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert doc.status == "error"

    stage = tasks.restart_stage(s, doc.id)
    assert stage == "analyze_rule_group"
    documents.retry_document(s, doc.id)
    tasks.retry_document(batch_id, str(doc.id), stage)
    doc = docs_of(s, batch_id)["Контракт.docx"]
    assert doc.status == "done" and len(runs(s, doc.id, "index_chunks")) == 1
    assert s.get(Batch, doc.batch_id).status == "done"


def test_retry_signature_starts_from_failed_stage():
    def names(sig) -> list[str]:
        return [t.task.rsplit(".", 1)[-1] for t in getattr(sig, "tasks", [sig])]

    assert names(tasks.retry_signature("b", "d", "ocr_pages")) == [
        "extract_text", "build_structure", "index_chunks", "extract_batch_meta"]
    assert names(tasks.retry_signature("b", "d", "extracting")) == [
        "extract_text", "build_structure", "index_chunks", "extract_batch_meta"]
    assert names(tasks.retry_signature("b", "d", "index_chunks")) == ["index_chunks", "extract_batch_meta"]
    assert names(tasks.retry_signature("b", "d", "merge_findings")) == ["extract_batch_meta"]


def test_retry_delay_grows_exponentially():
    delays = [tasks.retry_delay(n) for n in range(4)]
    base = tasks.RETRY_BASE_SECONDS
    for n, delay in enumerate(delays):
        assert base * 2 ** n * 0.8 <= delay <= base * 2 ** n * 1.2
    assert tasks.retry_delay(30) <= tasks.RETRY_MAX_SECONDS * 1.2


def test_events_survive_broken_bus(s, env, author, contract, monkeypatch):
    from worker import runtime

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise ConnectionError("redis down")
            return fail

    monkeypatch.setattr(runtime, "get_events", lambda: Down())
    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    assert docs_of(s, batch_id)["Контракт.docx"].status == "done"  # без Redis событий нет, конвейер идёт


def test_ocr_progress_events(s, env, bus, author, contract):
    from worker.progress import ocr_page_done, report_progress

    batch_id = make_batch(s, env, author, [("Контракт.docx", contract)])
    run_batch(batch_id)
    doc = s.scalars(select(Document).where(Document.batch_id == batch_id)).one()
    before = len(bus.events(batch_id))
    report_progress(str(doc.id), "ocr", 10, {"page": 0, "pages": 3})
    ocr_page_done(str(doc.id), 3, 1, start=10, span=50)
    new = [(t, d) for _, t, d in bus.events(batch_id)[before:]]
    labels = [d["stage_label"] for t, d in new if t == "document.progress"]
    assert labels == ["Распознавание текста, стр. 0 из 3", "Распознавание текста, стр. 1 из 3"]


def test_retry_of_broken_file_keeps_its_reason(s, env, author, contract):
    from tests.worker.conftest import zip_bytes

    batch_id = make_batch(s, env, author, [("Пакет.zip", zip_bytes({"Битый.pdf": b"not a pdf",
                                                                     "Контракт.docx": contract}))])
    run_batch(batch_id)
    broken = docs_of(s, batch_id)["Битый.pdf"]
    reason = (broken.error_code, broken.error_message)
    assert reason[0] == "corrupted"

    stage = tasks.restart_stage(s, broken.id)
    assert stage == "extracting"  # упал на распаковке: журнала этапов нет, повтор с начала
    documents.retry_document(s, broken.id)
    tasks.retry_document(batch_id, str(broken.id), stage)
    broken = docs_of(s, batch_id)["Битый.pdf"]
    assert (broken.status, broken.error_code, broken.error_message) == ("error", *reason)
    assert s.get(Batch, broken.batch_id).status == "partial_error"
