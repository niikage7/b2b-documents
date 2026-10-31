"""Пакеты: черновик, загрузка файлов, запуск, список, экран пакета (FR-1.1–1.3, FR-10.2)."""

from __future__ import annotations

import dataclasses
import io
import uuid
import zipfile

import pymupdf
import pytest

from api import hooks
from api.config import get_settings
from api.deps import settings_dep
from bench.samples import make_contract_docx
from db import pipeline
from db.seed import seed
from tests.api.conftest import login


@pytest.fixture
def world(s, users, client, app):
    """Профиль правил, три пользователя, вход под специалистом."""
    seed(s)
    login(client, "specialist")
    return users


@pytest.fixture
def hook_calls(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(hooks, "enqueue_batch", lambda batch_id: calls.append(("batch", batch_id)))
    monkeypatch.setattr(hooks, "enqueue_rerun", lambda batch_id, ids: calls.append(("rerun", batch_id, ids)))
    return calls


def pdf_bytes(pages: int = 2, *, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    if password:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
    return doc.tobytes()


def zip_bytes(entries: dict[str, bytes], *, utf8: bool = True) -> bytes:
    buf = io.BytesIO()
    placeholders: dict[bytes, bytes] = {}
    with zipfile.ZipFile(buf, "w") as zf:
        for i, (name, data) in enumerate(entries.items()):
            if utf8 or name.isascii():
                zf.writestr(name, data)
                continue
            # Как пишут русские архивы Windows: имя в cp866 без флага UTF-8. Пишем ASCII-метку той же
            # длины и подменяем её байтами cp866 в заголовках
            encoded = name.encode("cp866")
            marker = f"{i:0{len(encoded)}d}".encode()
            placeholders[marker] = encoded
            zf.writestr(marker.decode(), data)
    raw = buf.getvalue()
    for marker, encoded in placeholders.items():
        raw = raw.replace(marker, encoded)
    return raw


def upload(client, batch_id: str, *files: tuple[str, bytes]):
    return client.post(f"/api/batches/{batch_id}/files", files=[("files", (name, data)) for name, data in files])


def new_draft(client) -> str:
    return client.post("/api/batches").json()["id"]


def started_batch(client, name: str = "Закупка") -> str:
    draft = new_draft(client)
    assert upload(client, draft, ("a.pdf", pdf_bytes())).status_code == 201
    assert client.post(f"/api/batches/{draft}/start", json={"name": name}).status_code == 202
    return draft


# --- Черновик и загрузка ---------------------------------------------------------------------


def test_requires_login(client):
    for method, path in (("get", "/api/batches"), ("post", "/api/batches"), ("get", f"/api/batches/{uuid.uuid4()}")):
        assert getattr(client, method)(path).status_code == 401


def test_create_draft(world, client):
    r = client.post("/api/batches")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft" and body["files"] == [] and body["total_bytes"] == 0
    assert body["max_batch_bytes"] == 100 * 1024 * 1024
    assert client.get(f"/api/batches/{body['id']}").json()["status"] == "draft"


def test_upload_checks_each_file(world, client, storage, tmp_path):
    draft = new_draft(client)
    docx = make_contract_docx(tmp_path / "contract.docx").read_bytes()
    r = upload(client, draft, ("Контракт.docx", docx), ("Извещение.pdf", pdf_bytes()),
               ("Защищён.pdf", pdf_bytes(password="secret")), ("Сломан.pdf", b"not a pdf at all"),
               ("Таблица.xlsx", b"PK\x03\x04junk"), ("Пустой.pdf", b""))
    assert r.status_code == 201, r.text
    files = {f["filename"]: f for f in r.json()["files"]}
    assert files["Контракт.docx"]["check"] == "ok" and files["Контракт.docx"]["type_label"] == "DOCX"
    assert files["Извещение.pdf"]["check"] == "ok" and files["Извещение.pdf"]["message"] is None
    assert files["Защищён.pdf"]["check"] == "encrypted" and "паролем" in files["Защищён.pdf"]["message"]
    assert files["Сломан.pdf"]["check"] == "corrupted" and files["Сломан.pdf"]["message"]
    assert files["Таблица.xlsx"]["check"] == "unsupported" and "не поддерживается" in files["Таблица.xlsx"]["message"]
    assert files["Пустой.pdf"]["check"] == "corrupted"
    assert r.json()["total_bytes"] == sum(f["size_bytes"] for f in r.json()["files"]) > len(docx)
    assert len(storage.objects) == 6 and all(k.startswith(f"batches/{draft}/source/") for k in storage.objects)
    # Повторная загрузка дополняет список
    assert len(upload(client, draft, ("Ещё.pdf", pdf_bytes(1))).json()["files"]) == 7


def test_upload_strips_client_path(world, client):
    draft = new_draft(client)
    r = upload(client, draft, ("C:\\Users\\Мария\\Документы\\Договор.pdf", pdf_bytes()))
    assert [f["filename"] for f in r.json()["files"]] == ["Договор.pdf"]


def test_zip_shows_contents_without_unpacking(world, client):
    draft = new_draft(client)
    data = zip_bytes({
        "Закупка_0045/Контракт.pdf": pdf_bytes(),
        "Закупка_0045/Смета.xlsx": b"x",
        "Закупка_0045/../evil.pdf": pdf_bytes(),
        "Закупка_0045/Приложения.zip": zip_bytes({"a.pdf": pdf_bytes()}),
        "__MACOSX/._x.pdf": b"x",
        "Закупка_0045/": b"",
    })
    r = upload(client, draft, ("Закупка_0045.zip", data))
    f = r.json()["files"][0]
    assert f["check"] == "ok" and f["type_label"] == "ZIP"
    assert {e["path"]: e["check"] for e in f["archive_entries"]} == {
        "Закупка_0045/Контракт.pdf": "ok", "Закупка_0045/Смета.xlsx": "unsupported",
        "Закупка_0045/../evil.pdf": "unsupported", "Закупка_0045/Приложения.zip": "ok"}
    # Первый zip задаёт название пакета по умолчанию
    assert client.get(f"/api/batches/{draft}").json()["name"] == "Закупка_0045"


def test_zip_with_cp866_names(world, client):
    draft = new_draft(client)
    f = upload(client, draft, ("a.zip", zip_bytes({"Договор.pdf": pdf_bytes()}, utf8=False))).json()["files"][0]
    assert [e["path"] for e in f["archive_entries"]] == ["Договор.pdf"]


def test_broken_zip_is_corrupted(world, client):
    draft = new_draft(client)
    f = upload(client, draft, ("a.zip", b"PK\x03\x04" + b"\x00" * 30)).json()["files"][0]
    assert f["check"] == "corrupted"


def test_upload_without_files_is_422(world, client):
    draft = new_draft(client)
    r = client.post(f"/api/batches/{draft}/files", data={"x": "1"})
    assert r.status_code == 422 and "files" in r.json()["error"]["details"]["fields"]


def test_upload_to_unknown_batch_is_404(world, client):
    assert upload(client, str(uuid.uuid4()), ("a.pdf", pdf_bytes())).status_code == 404


def test_batch_limit_is_413_and_stores_nothing(world, client, app, storage):
    small = dataclasses.replace(get_settings(), max_batch_bytes=3000)
    app.dependency_overrides[settings_dep] = lambda: small
    draft = new_draft(client)
    big = pdf_bytes(40)
    assert len(big) > 3000
    r = upload(client, draft, ("a.pdf", big))
    assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"
    assert storage.objects == {}
    assert client.get(f"/api/batches/{draft}").json()["files"] == []


def test_declared_oversize_body_is_rejected_before_reading(world, client):
    draft = new_draft(client)
    r = client.post(f"/api/batches/{draft}/files", content=b"x", headers={
        "content-type": "multipart/form-data; boundary=x", "content-length": str(200 * 1024 * 1024)})
    assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"


def test_remove_file(world, client, storage):
    draft = new_draft(client)
    files = upload(client, draft, ("a.pdf", pdf_bytes()), ("b.pdf", pdf_bytes(1))).json()["files"]
    first, second = files
    r = client.delete(f"/api/batches/{draft}/files/{first['id']}")
    assert r.status_code == 200
    assert [f["id"] for f in r.json()["files"]] == [second["id"]]
    assert r.json()["total_bytes"] == second["size_bytes"]
    assert len(storage.objects) == 1
    assert client.delete(f"/api/batches/{draft}/files/{first['id']}").status_code == 404
    assert client.delete(f"/api/batches/{draft}/files/{uuid.uuid4()}").status_code == 404


# --- Запуск ----------------------------------------------------------------------------------


def test_start_validation(world, client):
    draft = new_draft(client)
    r = client.post(f"/api/batches/{draft}/start", json={"name": "Закупка"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "nothing_to_check"
    upload(client, draft, ("a.xlsx", b"junk"))
    assert client.post(f"/api/batches/{draft}/start", json={"name": "Закупка"}).status_code == 422
    upload(client, draft, ("a.pdf", pdf_bytes()))
    r = client.post(f"/api/batches/{draft}/start", json={"name": "  "})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == {"name": "Введите название пакета"}
    r = client.post(f"/api/batches/{draft}/start", json={"name": "Закупка", "profile_id": str(uuid.uuid4())})
    assert r.status_code == 422 and "profile_id" in r.json()["error"]["details"]["fields"]
    r = client.post(f"/api/batches/{draft}/start", json={"name": "Закупка", "profile_id": "не-uuid"})
    assert r.status_code == 422


def test_start_queues_batch_and_calls_hook(world, client, hook_calls):
    draft = new_draft(client)
    upload(client, draft, ("a.pdf", pdf_bytes()), ("b.xlsx", b"junk"))
    r = client.post(f"/api/batches/{draft}/start", json={"name": " Закупка 45 "})
    assert r.status_code == 202 and r.json() == {"id": draft, "status": "queued"}
    assert hook_calls == [("batch", draft)]
    view = client.get(f"/api/batches/{draft}").json()
    assert view["name"] == "Закупка 45" and view["status"] == "queued"
    assert view["profile"]["short_name"]
    # Повторный запуск и добавление файлов в запущенный пакет — 409
    assert client.post(f"/api/batches/{draft}/start", json={"name": "x"}).status_code == 409
    assert upload(client, draft, ("c.pdf", pdf_bytes())).status_code == 409
    assert client.delete(f"/api/batches/{draft}/files/{uuid.uuid4()}").status_code == 409
    assert hook_calls == [("batch", draft)]


def test_start_enqueues_pipeline(world, client, no_broker):
    """Без подмены хука запуск доходит до постановки `unpack_batch` (здесь — без Redis)."""
    draft = new_draft(client)
    upload(client, draft, ("a.pdf", pdf_bytes()))
    assert client.post(f"/api/batches/{draft}/start", json={"name": "Закупка"}).status_code == 202
    assert no_broker == [("batch", draft)]


def test_enqueue_failure_does_not_break_response(world, client, monkeypatch):
    from worker import tasks

    def broken(batch_id):
        raise ConnectionError("redis недоступен")

    monkeypatch.setattr(tasks, "process_batch", broken)
    draft = new_draft(client)
    upload(client, draft, ("a.pdf", pdf_bytes()))
    assert client.post(f"/api/batches/{draft}/start", json={"name": "Закупка"}).status_code == 202


def test_start_with_explicit_profile(world, client, s):
    from db import rules

    profile = rules.create_profile(s, name="Другой профиль", short_name="Другой")
    draft = new_draft(client)
    upload(client, draft, ("a.pdf", pdf_bytes()))
    assert client.post(f"/api/batches/{draft}/start", json={"name": "П", "profile_id": profile["id"]}).status_code == 202
    assert client.get(f"/api/batches/{draft}").json()["profile"]["id"] == profile["id"]


# --- Список ----------------------------------------------------------------------------------


def test_list_hides_drafts_and_filters(world, client, hook_calls):
    new_draft(client)
    mine = started_batch(client, "Поставка серверов")
    login(client, "lawyer")
    other = started_batch(client, "Ремонт кровли")

    body = client.get("/api/batches").json()
    assert body["total"] == 2 and body["processing_count"] == 2 and body["page"] == 1 and body["page_size"] == 20
    assert {i["id"] for i in body["items"]} == {mine, other}
    item = next(i for i in body["items"] if i["id"] == other)
    assert item["status"] == "queued" and item["documents_count"] == 0 and item["author"]["full_name"]
    assert item["worst_level"] is None and item["findings"] == {"critical": 0, "warning": 0}

    assert [i["id"] for i in client.get("/api/batches", params={"owner": "me"}).json()["items"]] == [other]
    assert [i["id"] for i in client.get("/api/batches", params={"search": "серверов"}).json()["items"]] == [mine]
    assert client.get("/api/batches", params={"search": "%"}).json()["total"] == 0
    assert client.get("/api/batches", params={"status": "done"}).json()["total"] == 0
    assert client.get("/api/batches", params={"status": "processing"}).json()["total"] == 2
    page = client.get("/api/batches", params={"page_size": 1, "page": 2}).json()
    assert page["total"] == 2 and len(page["items"]) == 1


@pytest.mark.parametrize("params", [{"status": "bogus"}, {"level": "red"}, {"period": "1y"}, {"owner": "x"},
                                    {"page": 0}, {"page_size": 101}])
def test_list_rejects_bad_filters(world, client, params):
    r = client.get("/api/batches", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


# --- Экран пакета ----------------------------------------------------------------------------


def test_patch_batch(world, client, hook_calls):
    batch = started_batch(client)
    r = client.patch(f"/api/batches/{batch}", json={"name": " Новое имя ", "subject": "Поставка"})
    assert r.status_code == 200 and r.json()["name"] == "Новое имя" and r.json()["subject"] == "Поставка"
    r = client.patch(f"/api/batches/{batch}", json={"name": " "})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == {"name": "Введите название пакета"}
    assert client.patch(f"/api/batches/{uuid.uuid4()}", json={"name": "x"}).status_code == 404


def test_delete_draft_removes_files_from_storage(world, client, storage):
    draft = new_draft(client)
    upload(client, draft, ("a.pdf", pdf_bytes()), ("b.pdf", pdf_bytes(1)))
    assert len(storage.objects) == 2
    r = client.delete(f"/api/batches/{draft}")
    assert r.status_code == 204 and r.content == b""
    assert storage.objects == {}
    assert client.get(f"/api/batches/{draft}").status_code == 404


def test_delete_started_batch_hides_it(world, client, hook_calls, storage):
    batch = started_batch(client)
    assert client.delete(f"/api/batches/{batch}").status_code == 204
    assert len(storage.objects) == 1  # исходники запущенного пакета нужны для экспорта и повтора
    assert client.get(f"/api/batches/{batch}").status_code == 404
    assert client.get("/api/batches").json()["total"] == 0


def test_rerun_requires_finished_batch(world, client, hook_calls):
    batch = started_batch(client)
    r = client.post(f"/api/batches/{batch}/rerun")
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_rerun_requeues_done_documents(world, client, s, hook_calls):
    batch = started_batch(client)
    file_id = s.execute(__import__("sqlalchemy").text("SELECT id FROM uploaded_file WHERE batch_id = :b"),
                        {"b": batch}).scalar_one()
    doc = pipeline.create_document(s, batch_id=batch, uploaded_file_id=file_id, filename="a.pdf", original_path="a.pdf",
                                   kind="pdf", size_bytes=10, object_key="k")
    pipeline.merge_findings(s, doc.id, [])
    pipeline.finish_batch(s, batch)
    assert client.get(f"/api/batches/{batch}").json()["status"] == "done"

    hook_calls.clear()
    r = client.post(f"/api/batches/{batch}/rerun")
    assert r.status_code == 202
    assert hook_calls == [("rerun", batch, [str(doc.id)])]
    assert client.get(f"/api/batches/{batch}").json()["status"] == "queued"


def test_batch_findings(world, client, hook_calls):
    batch = started_batch(client)
    r = client.get(f"/api/batches/{batch}/findings")
    assert r.status_code == 200 and r.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert client.get(f"/api/batches/{uuid.uuid4()}/findings").status_code == 404
    assert client.get(f"/api/batches/{batch}/findings", params={"level": "red"}).status_code == 422
    assert client.get(f"/api/batches/{batch}/findings", params={"level": "critical", "status": "new"}).status_code == 200


def test_invalid_batch_id_is_422(world, client):
    r = client.get("/api/batches/not-a-uuid")
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == {"batch_id": "Неверный идентификатор"}
