"""API запросов к ИИ: очередь подменена, проверяются доступ, ошибки и форма ответа."""

from __future__ import annotations

import uuid

import pytest

from api import hooks
from db import exports
from tests.api.conftest import login
from tests.api.test_documents import world  # noqa: F401 — фикстура готового документа


@pytest.fixture
def assist(monkeypatch):
    calls: list[dict] = []
    answers = {
        "ask": {"result": {"found": True, "answer": "7 дней", "citations": [], "usage": {"cost": 0.1}}},
        "brief": {"result": {"decision": "go", "decision_label": "Рисков нет", "summary": "s", "top_risks": [],
                             "actions": [], "not_checked": 0, "usage": {"cost": 0.1}}},
        "rule_draft": {"result": {"title": "T", "description": "D", "level": "warning", "kind": "semantic_llm",
                                  "prompt_template": "{фрагмент}", "variables": [], "risk_example": "r",
                                  "safe_example": "s", "warnings": [], "usage": {}}},
    }

    def run(payload, *, timeout):
        calls.append(payload)
        return answers[payload["kind"]]

    monkeypatch.setattr(hooks, "run_assist", run)
    return calls


def test_requires_login(client):
    assert client.post(f"/api/batches/{uuid.uuid4()}/ask", json={"question": "?"}).status_code == 401
    assert client.post("/api/rules/draft", json={"description": "x" * 20}).status_code == 401


def test_ask_and_brief_hide_usage(world, client, assist):  # noqa: F811
    batch = world["batch_id"]
    r = client.post(f"/api/batches/{batch}/ask", json={"question": "Срок оплаты?"})
    assert r.status_code == 200 and r.json() == {"found": True, "answer": "7 дней", "citations": []}
    assert assist[0] == {"kind": "ask", "batch_id": str(batch), "question": "Срок оплаты?"}
    r = client.post(f"/api/batches/{batch}/brief")
    assert r.status_code == 200 and r.json()["decision"] == "go" and "usage" not in r.json()


def test_ask_validation_and_missing_batch(world, client, assist):  # noqa: F811
    assert client.post(f"/api/batches/{world['batch_id']}/ask", json={"question": "  "}).status_code == 422
    assert client.post(f"/api/batches/{uuid.uuid4()}/ask", json={"question": "?"}).status_code == 404
    assert assist == []


def test_worker_error_is_shown_as_is(world, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(hooks, "run_assist", lambda payload, *, timeout: {
        "error": {"code": "llm_unavailable", "message": "Сервис недоступен", "retryable": True}})
    r = client.post(f"/api/batches/{world['batch_id']}/ask", json={"question": "Срок?"})
    assert r.status_code == 503 and r.json()["error"]["message"] == "Сервис недоступен"


def test_clarification_goes_to_export_queue(world, client, s, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(hooks, "enqueue_clarification", calls.append)
    r = client.post(f"/api/batches/{world['batch_id']}/clarification")
    assert r.status_code == 202
    job = r.json()
    assert job["format"] == "docx" and calls == [job["export_id"]]
    assert "Запрос о разъяснениях" in exports.export_view(s, job["export_id"])["filename"]


def test_rule_draft_only_for_methodologist(world, client, assist):  # noqa: F811
    assert client.post("/api/rules/draft", json={"description": "Заказчик платит слишком поздно"}).status_code == 403
    login(client, "methodologist")
    assert client.post("/api/rules/draft", json={"description": "мало"}).status_code == 422
    r = client.post("/api/rules/draft", json={"description": "Заказчик платит слишком поздно"})
    assert r.status_code == 200 and r.json()["title"] == "T" and r.json()["code"].startswith("R-")
