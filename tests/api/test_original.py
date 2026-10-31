"""Сверка находки с оригиналом (FR-5.6*): страница исходного PDF и рамки цитаты."""

from __future__ import annotations

import uuid

import pymupdf
import pytest

from db.models import BlockRow, Document
from tests.api.test_documents import world  # noqa: F401 — фикстура готового документа


@pytest.fixture
def pdf_world(world, s, client, storage):  # noqa: F811
    """Тот же документ как двухстраничный PDF: у цитаты находки R-02 есть рамка слова на странице 2."""
    doc = pymupdf.open()
    doc.new_page(), doc.new_page()
    storage.put_bytes("k/contract", doc.tobytes())
    row = s.get(Document, uuid.UUID(world["doc_id"]))
    row.kind = "pdf"
    finding = next(f for f in client.get(f"/api/documents/{world['doc_id']}").json()["findings"]
                   if f["rule"]["code"] == "R-02")
    block = s.get(BlockRow, uuid.UUID(finding["block_id"]))
    block.source_map = {"format": "pdf", "words": [
        {"start": finding["start"], "end": finding["end"], "page": 2,
         "bbox": {"x0": 100, "y0": 100, "x1": 200, "y1": 120}}]}
    s.flush()
    return {**world, "finding": finding}


def test_requires_login(client):
    assert client.get(f"/api/findings/{uuid.uuid4()}/source").status_code == 401
    assert client.get(f"/api/documents/{uuid.uuid4()}/pages/1/image").status_code == 401


def test_source_and_image(pdf_world, client, storage):
    doc_id = pdf_world["doc_id"]
    view = client.get(f"/api/documents/{doc_id}").json()["findings"]
    assert all(f["source_available"] for f in view)

    r = client.get(f"/api/findings/{pdf_world['finding']['id']}/source")
    assert r.status_code == 200
    data = r.json()
    assert data["page"] == 2 and data["image_url"] == f"/api/documents/{doc_id}/pages/2/image"
    (rect,) = data["rects"]
    assert rect["x0"] == pytest.approx(100 / 595, abs=0.01) and rect["y1"] == pytest.approx(120 / 842, abs=0.01)
    assert data["aspect"] == pytest.approx(595 / 842, abs=0.01)

    image = client.get(data["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.content[:2] == b"\xff\xd8"

    # Второй показ берёт страницу из хранилища: исходник уже не нужен
    del storage.objects["k/contract"]
    assert client.get(data["image_url"]).status_code == 200


def test_missing_page_and_missing_boxes(pdf_world, client):
    assert client.get(f"/api/documents/{pdf_world['doc_id']}/pages/9/image").status_code == 404
    other = next(f for f in client.get(f"/api/documents/{pdf_world['doc_id']}").json()["findings"]
                 if f["rule"]["code"] == "R-01")
    data = client.get(f"/api/findings/{other['id']}/source").json()
    assert data["page"] == 1 and data["rects"] == []


def test_docx_has_no_page_view(world, client):  # noqa: F811
    findings = client.get(f"/api/documents/{world['doc_id']}").json()["findings"]
    assert not findings[0]["source_available"]
    assert client.get(f"/api/findings/{findings[0]['id']}/source").status_code == 409
    assert client.get(f"/api/documents/{world['doc_id']}/pages/1/image").status_code == 409
