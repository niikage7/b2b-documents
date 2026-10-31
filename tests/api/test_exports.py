"""Тесты API выгрузки (FR-7.1, FR-7.2, FR-8.1): постановка задачи, статус, скачивание через API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from db import batches, exports, pipeline
from db.models import Export
from tests.api.conftest import login
from tests.api.test_documents import world  # noqa: F401 — фикстура готового документа


@pytest.fixture
def queued(monkeypatch):
    """Постановка выгрузки без Redis: вызовы записываются."""
    from api import hooks

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hooks, "enqueue_export", lambda scope, export_id: calls.append((scope, export_id)))
    return calls


def finish(s, storage, export_id: str, data: bytes, fmt: str) -> None:
    key = f"exports/{export_id}/result.{fmt}"
    storage.put_bytes(key, data)
    exports.finish_export(s, export_id, object_key=key)


def test_requires_login(client):
    assert client.post(f"/api/documents/{uuid.uuid4()}/exports", json={}).status_code == 401
    assert client.get(f"/api/exports/{uuid.uuid4()}").status_code == 401
    assert client.get(f"/api/exports/{uuid.uuid4()}/download").status_code == 401


def test_document_export_flow(world, client, s, storage, queued):  # noqa: F811
    r = client.post(f"/api/documents/{world['doc_id']}/exports", json={"include_rejected": False})
    assert r.status_code == 202
    job = r.json()
    assert job["format"] == "docx" and queued == [("document", job["export_id"])]

    status = client.get(f"/api/exports/{job['export_id']}").json()
    assert status == {"export_id": job["export_id"], "status": "processing", "download_url": None,
                      "filename": "Контракт_разметка.docx", "error_message": None, "expires_at": None}
    assert client.get(f"/api/exports/{job['export_id']}/download").status_code == 409

    finish(s, storage, job["export_id"], b"PK docx bytes" * 10000, "docx")
    status = client.get(f"/api/exports/{job['export_id']}").json()
    assert status["status"] == "done" and status["download_url"] == f"/api/exports/{job['export_id']}/download"

    r = client.get(status["download_url"])
    assert r.status_code == 200
    assert r.content == b"PK docx bytes" * 10000
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert f"filename*=UTF-8''{quote('Контракт_разметка.docx')}" in r.headers["content-disposition"]


def test_body_is_optional(world, client, queued):  # noqa: F811
    assert client.post(f"/api/documents/{world['doc_id']}/exports").status_code == 202


def test_batch_export(world, client, queued):  # noqa: F811
    r = client.post(f"/api/batches/{world['batch_id']}/exports", json={"include_rejected": False})
    assert r.status_code == 202
    assert r.json()["format"] == "zip" and queued == [("batch", r.json()["export_id"])]
    assert client.get(f"/api/exports/{r.json()['export_id']}").json()["filename"] == "Закупка_разметка.zip"


def test_not_ready_targets_are_409(world, client, s, users, queued):  # noqa: F811
    draft = batches.create_draft(s, author_id=users["lawyer"].id)
    r = client.post(f"/api/batches/{draft['id']}/exports", json={})
    assert r.status_code == 409 and "проверяется" in r.json()["error"]["message"]

    pipeline.fail_document(s, world["doc_id"], "corrupted", "Файл повреждён")
    r = client.post(f"/api/documents/{world['doc_id']}/exports", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    assert client.post(f"/api/documents/{uuid.uuid4()}/exports", json={}).status_code == 404
    assert client.post(f"/api/batches/{uuid.uuid4()}/exports", json={}).status_code == 404
    assert client.get(f"/api/exports/{uuid.uuid4()}").status_code == 404
    assert queued == []


def test_failed_and_expired_exports(world, client, s, storage, queued):  # noqa: F811
    job = client.post(f"/api/documents/{world['doc_id']}/exports", json={}).json()
    exports.finish_export(s, job["export_id"], error="Не удалось подготовить файл — попробуйте скачать ещё раз")
    status = client.get(f"/api/exports/{job['export_id']}").json()
    assert status["status"] == "error" and status["download_url"] is None
    r = client.get(f"/api/exports/{job['export_id']}/download")
    assert r.status_code == 409 and r.json()["error"]["message"].startswith("Не удалось подготовить файл")

    job = client.post(f"/api/documents/{world['doc_id']}/exports", json={}).json()
    finish(s, storage, job["export_id"], b"x", "docx")
    s.get(Export, uuid.UUID(job["export_id"])).expires_at = datetime.now(UTC) - timedelta(minutes=1)
    s.flush()
    assert client.get(f"/api/exports/{job['export_id']}/download").status_code == 404

    job = client.post(f"/api/documents/{world['doc_id']}/exports", json={}).json()
    exports.finish_export(s, job["export_id"], object_key="exports/missing.docx")
    assert client.get(f"/api/exports/{job['export_id']}/download").status_code == 404


def test_other_user_can_download(world, client, s, storage, queued):  # noqa: F811
    """Пакеты видны всем сотрудникам (фильтр «Мои» — только в списке), выгрузки — тоже."""
    job = client.post(f"/api/documents/{world['doc_id']}/exports", json={}).json()
    finish(s, storage, job["export_id"], b"data", "docx")
    client.post("/api/auth/logout")
    login(client, "specialist")
    assert client.get(f"/api/exports/{job['export_id']}/download").content == b"data"
