"""Тесты API документов и ревью находок (FR-5.1–5.5, FR-6.1–6.4). Данные создаются функциями `db.pipeline`."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from contracts.analysis import FindingCandidate, RuleGroupResult
from contracts.document import Block
from db import batches, pipeline
from db.seed import seed
from tests.api.conftest import login

PRICE = "Цена контракта составляет 4 850 000 рублей, неустойка 0,5% за каждый день просрочки."
TERM = "Оплата производится в течение 60 календарных дней после подписания акта."


def _block(doc_id: str, order: int, kind: str, text_: str, *, number: str | None = None, page: int = 1,
           section_title: str | None = None, section_number: str | None = None) -> Block:
    return Block(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}/{order}")), document_id=doc_id, order=order, kind=kind,
                 number=number, text=text_, page=page, section_number=section_number, section_title=section_title)


@pytest.fixture
def published(bus, world):
    """События `document.done`, которые API опубликовал в шину пакета после правки находки."""
    return lambda: [data for _, type_, data in bus.events(world["batch_id"]) if type_ == "document.done"]


@pytest.fixture
def world(s, users, client, app):
    """Профиль, три пользователя, вход под юристом; готовый документ с двумя находками ИИ."""
    seed(s)
    login(client, "lawyer")
    owner = users["lawyer"]
    draft = batches.create_draft(s, author_id=owner.id)
    files = batches.add_files(s, draft["id"], [
        {"filename": "Контракт.docx", "size_bytes": 900, "mime": "application/msword", "object_key": "k/contract",
         "check": "ok"}])
    batches.start_batch(s, draft["id"], name="Закупка")
    doc = pipeline.create_document(s, batch_id=draft["id"], uploaded_file_id=files["files"][0]["id"],
                                   filename="Контракт.docx", original_path="Контракт.docx", kind="docx",
                                   size_bytes=900, object_key="k/contract")
    batches.mark_processing(s, draft["id"])
    doc_id = str(doc.id)
    blocks = [
        _block(doc_id, 0, "heading", "Цена и порядок расчётов", number="3", section_title="Цена и порядок расчётов",
               section_number="3"),
        _block(doc_id, 1, "paragraph", PRICE, number="3.1", section_title="Цена и порядок расчётов",
               section_number="3.1"),
        _block(doc_id, 2, "paragraph", TERM, number="3.2", page=2, section_title="Цена и порядок расчётов",
               section_number="3.2"),
    ]
    pipeline.save_blocks(s, doc.id, blocks)
    rules = {r.code: r for r in pipeline.batch_rules(s, draft["id"])}
    candidates = []
    for code, level, block, quote in (("R-01", "critical", blocks[1], "неустойка 0,5% за каждый день"),
                                      ("R-02", "warning", blocks[2], "60 календарных дней")):
        start = block.text.index(quote)
        candidates.append(FindingCandidate(
            rule_code=code, rule_id=rules[code].id, rule_version_id=rules[code].version_id, level=level,
            block_id=block.id, start=start, end=start + len(quote), quote=quote, explanation=f"Риск по {code}",
            confidence=0.9, match_score=100))
    pipeline.merge_findings(s, doc.id, [RuleGroupResult(candidates=candidates)])
    pipeline.finish_batch(s, draft["id"])
    return {"batch_id": draft["id"], "doc_id": doc_id, "blocks": blocks, "user": owner}


def _findings(client, doc_id: str) -> list[dict]:
    return client.get(f"/api/documents/{doc_id}").json()["findings"]


def test_requires_login(client):
    assert client.get(f"/api/documents/{uuid.uuid4()}").status_code == 401
    assert client.patch(f"/api/findings/{uuid.uuid4()}", json={"version": 1}).status_code == 401


def test_document_view(world, client):
    r = client.get(f"/api/documents/{world['doc_id']}")
    assert r.status_code == 200
    view = r.json()
    assert view["status"] == "done" and view["worst_level"] == "critical" and view["blocks_complete"] is True
    assert [b["text"] for b in view["blocks"]] == [b.text for b in world["blocks"]]
    assert view["findings_summary"] == {"critical": 1, "warning": 1} and view["review"] == {"checked": 0, "total": 2}
    first = view["findings"][0]
    block = next(b for b in view["blocks"] if b["id"] == first["block_id"])
    assert block["text"][first["start"]:first["end"]] == first["quote"]
    assert first["section_number"] == "3.1" and first["page"] == 1 and first["rule"]["code"] == "R-01"


def test_unknown_document_is_404(world, client):
    r = client.get(f"/api/documents/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/api/documents/nope").status_code == 422


def test_blocks_by_pages(world, client):
    r = client.get(f"/api/documents/{world['doc_id']}/blocks", params={"from_page": 2, "to_page": 2})
    assert [b["text"] for b in r.json()["blocks"]] == [TERM]
    assert client.get(f"/api/documents/{world['doc_id']}/blocks", params={"from_page": 1, "to_page": 30}).status_code == 422
    assert client.get(f"/api/documents/{world['doc_id']}/blocks", params={"from_page": 3, "to_page": 2}).status_code == 422
    assert client.get(f"/api/documents/{world['doc_id']}/blocks").status_code == 422


def test_search(world, client):
    r = client.get(f"/api/documents/{world['doc_id']}/search", params={"q": "НЕУСТОЙКА"})
    matches = r.json()["matches"]
    assert len(matches) == 1 and matches[0]["page"] == 1
    block = next(b for b in world["blocks"] if b.id == matches[0]["block_id"])
    assert block.text[matches[0]["start"]:matches[0]["end"]].lower() == "неустойка"
    assert client.get(f"/api/documents/{world['doc_id']}/search", params={"q": "н"}).status_code == 422


def test_recent_documents(world, client):
    r = client.get("/api/documents", params={"status": "done", "limit": 20})
    assert r.status_code == 200
    assert r.json() == [{"id": world["doc_id"], "filename": "Контракт.docx",
                         "batch": {"id": world["batch_id"], "name": "Закупка"}}]
    assert client.get("/api/documents", params={"status": "error"}).status_code == 422
    assert client.get("/api/documents", params={"limit": 0}).status_code == 422


def test_review_accept_reject_and_recalc(world, client, published):
    critical, warning = _findings(client, world["doc_id"])
    r = client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "rejected"})
    assert r.status_code == 200
    body = r.json()
    assert body["finding"]["status"] == "rejected" and body["finding"]["decided_by"]["full_name"]
    assert body["document"]["worst_level"] == "warning"  # отклонённая находка в расчёт не идёт
    assert body["document"]["review"] == {"checked": 1, "total": 2}
    assert body["batch"]["counters"]["warning"] == 1
    [event] = published()
    assert event["document_id"] == world["doc_id"] and event["worst_level"] == "warning"
    assert event["findings"] == {"critical": 0, "warning": 1} and event["review"] == {"checked": 1, "total": 2}
    assert event["pages"] is None and event["ocr_low_quality_pages"] == []

    r = client.patch(f"/api/findings/{warning['id']}", json={"version": warning["version"], "status": "accepted"})
    assert r.json()["document"]["review"] == {"checked": 2, "total": 2}


def test_edit_level_and_comment(world, client):
    critical, _ = _findings(client, world["doc_id"])
    r = client.patch(f"/api/findings/{critical['id']}",
                     json={"version": critical["version"], "level": "warning", "comment": "Согласовано"})
    f = r.json()["finding"]
    assert f["status"] == "edited" and f["level"] == "warning" and f["original_level"] == "critical"
    assert f["comment"] == "Согласовано"
    assert r.json()["document"]["worst_level"] == "warning"


def test_version_conflict_returns_current(world, client):
    critical, _ = _findings(client, world["doc_id"])
    client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "accepted"})
    r = client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "rejected"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert r.json()["error"]["details"]["current"]["version"] == critical["version"] + 1


def test_invalid_transition_is_409(world, client):
    critical, _ = _findings(client, world["doc_id"])
    client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "rejected"})
    r = client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"] + 1, "status": "accepted"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


def test_patch_validation(world, client):
    critical, _ = _findings(client, world["doc_id"])
    url = f"/api/findings/{critical['id']}"
    assert client.patch(url, json={"status": "accepted"}).status_code == 422  # нет version
    r = client.patch(url, json={"version": 1, "level": "red"})
    assert r.status_code == 422 and "level" in r.json()["error"]["details"]["fields"]
    assert client.patch(url, json={"version": 1, "status": "done"}).status_code == 422
    assert client.patch(f"/api/findings/{uuid.uuid4()}", json={"version": 1}).status_code == 404


def test_manual_finding_lifecycle(world, client, published):
    block = world["blocks"][1]
    body = {"block_id": block.id, "start": 0, "end": 5, "level": "warning", "comment": "Уточнить цену"}
    r = client.post(f"/api/documents/{world['doc_id']}/findings", json=body)
    assert r.status_code == 201
    made = r.json()
    f = made["finding"]
    assert f["source"] == "user" and f["status"] == "accepted" and f["quote"] == block.text[:5] and f["can_delete"]
    assert made["document"]["review"]["total"] == 2  # прогресс ревью считается по находкам ИИ

    hist = client.get(f"/api/findings/{f['id']}/history").json()["events"]
    assert [e["action"] for e in hist] == ["created"] and hist[0]["actor"]["full_name"] == world["user"].full_name

    r = client.delete(f"/api/findings/{f['id']}")
    assert r.status_code == 200 and r.json()["document"]["id"] == world["doc_id"]
    assert len(_findings(client, world["doc_id"])) == 2
    assert len(published()) == 2
    assert client.delete(f"/api/findings/{f['id']}").status_code == 404


def test_manual_finding_validation(world, client):
    url = f"/api/documents/{world['doc_id']}/findings"
    block = world["blocks"][1]
    r = client.post(url, json={"block_id": block.id, "start": 0, "end": len(block.text) + 1, "level": "warning"})
    assert r.status_code == 422 and "start" in r.json()["error"]["details"]["fields"]
    r = client.post(url, json={"block_id": str(uuid.uuid4()), "start": 0, "end": 3, "level": "warning"})
    assert r.status_code == 422 and "block_id" in r.json()["error"]["details"]["fields"]
    assert client.post(url, json={"block_id": block.id, "start": 0, "end": 3, "level": "red"}).status_code == 422
    assert client.post(f"/api/documents/{uuid.uuid4()}/findings",
                       json={"block_id": block.id, "start": 0, "end": 3, "level": "warning"}).status_code == 404


def test_delete_only_own_manual_finding(world, client):
    ai = _findings(client, world["doc_id"])[0]
    r = client.delete(f"/api/findings/{ai['id']}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"

    block = world["blocks"][2]
    created = client.post(f"/api/documents/{world['doc_id']}/findings",
                          json={"block_id": block.id, "start": 0, "end": 6, "level": "critical"}).json()["finding"]
    client.post("/api/auth/logout")
    login(client, "specialist")
    assert client.delete(f"/api/findings/{created['id']}").status_code == 403
    login(client, "lawyer")
    assert client.delete(f"/api/findings/{created['id']}").status_code == 200


def test_history_of_ai_finding(world, client):
    critical, _ = _findings(client, world["doc_id"])
    client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "accepted"})
    events = client.get(f"/api/findings/{critical['id']}/history").json()["events"]
    assert [e["action"] for e in events] == ["created", "accepted"]
    assert events[0]["actor"]["full_name"] == "ИИ" and events[1]["actor"]["full_name"] == world["user"].full_name
    assert events[1]["old"]["status"] == "new" and events[1]["new"]["status"] == "accepted"


def test_batch_findings_reflect_review(world, client):
    critical, _ = _findings(client, world["doc_id"])
    client.patch(f"/api/findings/{critical['id']}", json={"version": critical["version"], "status": "rejected"})
    url = f"/api/batches/{world['batch_id']}/findings"
    assert client.get(url).json()["total"] == 2
    only = client.get(url, params={"status": "rejected"}).json()
    assert only["total"] == 1 and only["items"][0]["document"]["filename"] == "Контракт.docx"


def test_document_in_progress_has_no_blocks(world, client, s):
    s.execute(text("UPDATE document SET status = 'processing', stage = 'analyzing' WHERE id = :d"), {"d": world["doc_id"]})
    view = client.get(f"/api/documents/{world['doc_id']}").json()
    assert view["status"] == "processing" and view["blocks"] == [] and view["findings"] == []
