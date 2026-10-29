"""Прогресс в реальном времени и повтор документа (FR-2.2, FR-2.3): SSE и `POST /documents/{id}/retry`.

Шина событий — в памяти (`bus` из conftest). Поток SSE в тестах закрывается сам через `SSE_MAX_SECONDS`,
поэтому `TestClient` получает ответ целиком.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import asynccontextmanager

import pytest

from db import batches, pipeline
from db.models import Batch, Document
from db.seed import seed
from tests.api.conftest import login


@pytest.fixture
def short_sse(monkeypatch):
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0.1")
    monkeypatch.setenv("SSE_MAX_SECONDS", "0.5")


@pytest.fixture
def world(s, users, short_sse, client, app):
    """Пакет из двух документов: готовый и упавший на `build_structure`; вход под специалистом."""
    seed(s)
    login(client, "specialist")
    draft = batches.create_draft(s, author_id=users["specialist"].id)
    files = batches.add_files(s, draft["id"], [
        {"filename": name, "size_bytes": 100, "mime": "application/pdf", "object_key": f"k/{name}", "check": "ok"}
        for name in ("Готов.pdf", "Упал.pdf", "Архив.zip")])["files"]
    batches.start_batch(s, draft["id"], name="Закупка")
    batch_id = draft["id"]
    made = {}
    for f in files:
        made[f["filename"]] = pipeline.create_document(
            s, batch_id=batch_id, uploaded_file_id=f["id"], filename=f["filename"], original_path=f["filename"],
            kind=f["filename"].rsplit(".", 1)[-1], size_bytes=100, object_key=f"k/{f['filename']}")
    pipeline.merge_findings(s, made["Готов.pdf"].id, [])
    run = pipeline.start_task_run(s, batch_id=batch_id, stage="build_structure", document_id=made["Упал.pdf"].id)
    pipeline.finish_task_run(s, run.id, error="internal_error: boom")
    pipeline.fail_document(s, made["Упал.pdf"].id, "internal_error", "Не удалось обработать документ")
    pipeline.fail_document(s, made["Архив.zip"].id, "archive_limit", "Архив больше 500 МБ")
    pipeline.finish_batch(s, batch_id)
    return {"batch_id": batch_id, "done": str(made["Готов.pdf"].id), "failed": str(made["Упал.pdf"].id),
            "zip": str(made["Архив.zip"].id)}


def parse(body: str) -> tuple[list[tuple[str | None, str, dict]], int, int | None]:
    """Тело SSE → события (id, тип, данные), число heartbeat-комментариев, `retry`."""
    out, pings, retry = [], 0, None
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        fields: dict[str, str] = {}
        for line in chunk.split("\n"):
            if line.startswith(":"):
                pings += 1
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if "retry" in fields:
            retry = int(fields["retry"])
        if "event" in fields:
            out.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
    return out, pings, retry


def events(client, batch_id: str, **headers) -> tuple[list, int, int | None]:
    r = client.get(f"/api/batches/{batch_id}/events", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    return parse(r.text)


def test_requires_login_and_existing_batch(world, client):
    assert client.get(f"/api/batches/{uuid.uuid4()}/events").status_code == 404
    client.post("/api/auth/logout")
    assert client.get(f"/api/batches/{world['batch_id']}/events").status_code == 401


def test_first_event_is_snapshot_then_heartbeats(world, client, bus):
    old = bus.publish(world["batch_id"], "batch.progress", {"status": "processing"})
    got, pings, retry = events(client, world["batch_id"])
    assert retry == 3000 and pings >= 2
    [(event_id, type_, data)] = got  # уже опубликованные события учтены в снимке и не повторяются
    assert type_ == "batch.snapshot" and event_id == old
    assert data["id"] == world["batch_id"] and data["status"] == "partial_error" and len(data["documents"]) == 3


def test_live_events_arrive_with_ids(world, client, bus, monkeypatch):
    batch_id = world["batch_id"]
    data = {"document_id": world["done"], "status": "processing", "stage": "analysis", "progress": 90}
    opened = bus.reader

    @asynccontextmanager
    async def reader(batch):
        async with opened(batch) as r:
            # Событие приходит, когда подписчик уже получил снимок и ждёт поток
            threading.Timer(0.05, lambda: bus.publish(batch_id, "document.progress", data)).start()
            yield r

    monkeypatch.setattr(bus, "reader", reader)
    got, _, _ = events(client, batch_id)
    assert [t for _, t, _ in got] == ["batch.snapshot", "document.progress"]
    assert got[1][0] == bus.last_id(batch_id) and got[1][2] == data


def test_last_event_id_replays_missed_events_without_snapshot(world, client, bus):
    batch_id = world["batch_id"]
    seen = bus.publish(batch_id, "document.progress", {"document_id": world["done"], "progress": 10})
    missed = [bus.publish(batch_id, "document.progress", {"document_id": world["done"], "progress": p})
              for p in (20, 30)]
    bus.publish(str(uuid.uuid4()), "batch.done", {"status": "done"})  # чужой пакет не приходит
    got, _, _ = events(client, batch_id, **{"Last-Event-ID": seen})
    assert [(i, t, d["progress"]) for i, t, d in got] == [(missed[0], "document.progress", 20),
                                                        (missed[1], "document.progress", 30)]


@pytest.mark.parametrize("last", ["1-0", "not-an-id", ""])
def test_unknown_last_event_id_gets_snapshot(world, client, bus, last):
    bus.publish(world["batch_id"], "document.progress", {"progress": 10})
    got, _, _ = events(client, world["batch_id"], **{"Last-Event-ID": last})
    assert [t for _, t, _ in got] == ["batch.snapshot"]


def test_trimmed_stream_falls_back_to_snapshot(world, client, bus):
    bus.maxlen = 2
    first = bus.publish(world["batch_id"], "document.progress", {"progress": 1})
    for p in (2, 3):
        bus.publish(world["batch_id"], "document.progress", {"progress": p})
    got, _, _ = events(client, world["batch_id"], **{"Last-Event-ID": first})
    assert [t for _, t, _ in got] == ["batch.snapshot"]  # пропущенное уже обрезано — досылать нечего


def test_redis_down_gives_snapshot_only(world, client, monkeypatch):
    from worker import runtime

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise ConnectionError("redis down")
            return fail

    monkeypatch.setattr(runtime, "get_events", lambda: Down())
    got, pings, retry = events(client, world["batch_id"])
    assert [t for _, t, _ in got] == ["batch.snapshot"] and got[0][0] is None and pings == 0 and retry == 3000


def test_retry_failed_document(world, client, s, bus, no_broker):
    r = client.post(f"/api/documents/{world['failed']}/retry")
    assert r.status_code == 202
    assert no_broker == [("retry", world["batch_id"], world["failed"], "build_structure")]
    s.expire_all()
    doc = s.get(Document, uuid.UUID(world["failed"]))
    assert (doc.status, doc.stage, doc.progress, doc.error_code) == ("queued", "queued", 0, None)
    batch = s.get(Batch, uuid.UUID(world["batch_id"]))
    assert batch.status == "processing" and batch.finished_at is None
    progress = [d for _, t, d in bus.events(world["batch_id"]) if t == "document.progress"]
    assert progress == [{"document_id": world["failed"], "status": "queued", "stage": "queued",
                         "stage_label": "В очереди", "stage_detail": None,
                         "next_stage_label": "Далее: извлечение текста", "progress": 0}]
    assert "batch.progress" in bus.types(world["batch_id"])
    view = client.get(f"/api/batches/{world['batch_id']}").json()
    assert view["status"] == "processing"


def test_retry_only_failed_documents(world, client, no_broker):
    r = client.post(f"/api/documents/{world['done']}/retry")
    assert r.status_code == 409 and r.json()["error"]["message"] == "Повторить можно только документ с ошибкой"
    r = client.post(f"/api/documents/{world['zip']}/retry")
    assert r.status_code == 409 and "Архив" in r.json()["error"]["message"]
    assert client.post(f"/api/documents/{uuid.uuid4()}/retry").status_code == 404
    assert no_broker == []


def test_retry_enqueue_failure_does_not_break_response(world, client, monkeypatch, bus):
    from worker import tasks

    def down(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks, "retry_document", down)
    assert client.post(f"/api/documents/{world['failed']}/retry").status_code == 202
    assert bus.events(world["batch_id"]) == []  # в очередь не встало — «В очереди» не показываем
