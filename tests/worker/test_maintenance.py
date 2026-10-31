"""Периодическая очистка (`worker.maintenance`) и журнал в JSON (`worker.jsonlog`) на настоящем PostgreSQL."""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from db import batches, exports
from db.models import Batch, Export, UploadedFile
from worker import jsonlog, maintenance
from worker.app import celery_app, task_ids


def _draft(s, storage, author, *, age_hours: float, key_suffix: str = "a.pdf") -> tuple[str, str]:
    batch_id = batches.create_draft(s, author_id=author.id)["id"]
    key = f"batches/{batch_id}/files/{key_suffix}"
    storage.put_bytes(key, b"%PDF-1.4")
    batches.add_files(s, batch_id, [{"filename": key_suffix, "size_bytes": 8, "mime": "application/pdf",
                                     "object_key": key, "check": "ok", "message": None, "archive_entries": None}])
    s.get(Batch, uuid.UUID(batch_id)).created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    s.flush()
    return batch_id, key


def _export(s, storage, author, batch_id: str, *, expired: bool) -> tuple[str, str]:
    export_id = exports.create_export(s, scope="batch", target_id=batch_id, actor_id=author.id)["export_id"]
    key = f"exports/{export_id}/result.zip"
    storage.put_bytes(key, b"PK")
    exports.finish_export(s, export_id, object_key=key)
    if expired:
        s.get(Export, uuid.UUID(export_id)).expires_at = datetime.now(UTC) - timedelta(minutes=1)
    s.flush()
    return export_id, key


def test_cleanup_removes_old_drafts_with_files(s, env, author):
    old_id, old_key = _draft(s, env, author, age_hours=25)
    fresh_id, fresh_key = _draft(s, env, author, age_hours=1, key_suffix="b.pdf")
    started_id, started_key = _draft(s, env, author, age_hours=30, key_suffix="c.pdf")
    batches.start_batch(s, started_id, name="Запущенный")

    result = maintenance.cleanup_expired.delay().get()

    assert result["drafts"] == 1
    assert s.get(Batch, uuid.UUID(old_id)) is None
    assert s.query(UploadedFile).filter_by(batch_id=uuid.UUID(old_id)).count() == 0
    assert old_key not in env.objects
    assert s.get(Batch, uuid.UUID(fresh_id)) is not None and fresh_key in env.objects
    assert s.get(Batch, uuid.UUID(started_id)) is not None and started_key in env.objects


def test_cleanup_removes_expired_export_files_once(s, env, author):
    batch_id, _ = _draft(s, env, author, age_hours=1)
    batches.start_batch(s, batch_id, name="С выгрузкой")
    old_id, old_key = _export(s, env, author, batch_id, expired=True)
    new_id, new_key = _export(s, env, author, batch_id, expired=False)

    first = maintenance.cleanup()
    second = maintenance.cleanup()

    assert first["exports"] == 1 and second["exports"] == 0
    assert old_key not in env.objects and new_key in env.objects
    assert s.get(Export, uuid.UUID(old_id)).object_key is None
    assert s.get(Export, uuid.UUID(new_id)).object_key == new_key


def test_cleanup_is_scheduled_by_beat():
    entry = celery_app.conf.beat_schedule["cleanup-expired"]
    assert entry["task"] == maintenance.cleanup_expired.name
    assert celery_app.conf.task_routes[entry["task"]]["queue"] == "ingest"


# --- Журнал ------------------------------------------------------------------------------------


def _json_lines(logger_name: str, emit) -> list[dict]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(jsonlog.JsonFormatter("worker"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_json_log_carries_context_ids_and_extra():
    def emit(log):
        with jsonlog.bound(batch_id="b-1", document_id="d-1"):
            log.info("Этап %s готов", "extract_text", extra={"pages": 20})
        log.warning("без контекста")

    first, second = _json_lines("test.jsonlog", emit)
    assert first["message"] == "Этап extract_text готов"
    assert first["batch_id"] == "b-1" and first["document_id"] == "d-1" and first["pages"] == 20
    assert first["service"] == "worker" and first["level"] == "INFO" and first["ts"].endswith("Z")
    assert "batch_id" not in second


def test_json_log_includes_traceback():
    def emit(log):
        try:
            raise ValueError("сбой")
        except ValueError:
            log.exception("Ошибка")

    (entry,) = _json_lines("test.jsonlog.exc", emit)
    assert "ValueError: сбой" in entry["exc"]


def test_task_ids_from_task_arguments():
    from worker import exports as export_tasks
    from worker import tasks

    assert task_ids(tasks.extract_text, ("doc-1",), {}) == {"document_id": "doc-1"}
    assert task_ids(tasks.extract_batch_meta, ("b-1", ["doc-1"]), {}) == {"batch_id": "b-1"}
    assert task_ids(export_tasks.build_batch_zip, ([], "e-1"), {}) == {"export_id": "e-1"}
    assert task_ids(tasks.rule_test, ({"text": "x"},), {}) == {}
