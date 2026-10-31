"""Тесты API профилей и правил (FR-9.1): запись и чтение правил — только методологу."""

from __future__ import annotations

import uuid

import pytest

from db.seed import seed
from tests.api.conftest import login


@pytest.fixture
def world(s, users, client, app):
    seed(s)
    login(client, "methodologist")
    return users


def _profile_id(client) -> str:
    return next(p["id"] for p in client.get("/api/rule-profiles").json() if p["is_default"])


def _new_rule(profile_id: str, **over) -> dict:
    rule = {
        "profile_id": profile_id, "code": "X-01", "title": "Срок оплаты", "description": "Оплата дольше 30 дней",
        "level": "warning", "kind": "semantic_llm", "enabled": True, "variables": [{"name": "порог", "type": "number", "value": "30"}],
        "prompt_template": "Фрагмент: {{фрагмент}}\nСрок оплаты в документе больше {{порог}} дней?",
    }
    return rule | over


def test_requires_login(client):
    for method, url in (("get", "/api/rule-profiles"), ("get", "/api/rules"), ("get", "/api/rules/system-variables"),
                        ("post", "/api/rules"), ("delete", f"/api/rules/{uuid.uuid4()}")):
        assert getattr(client, method)(url).status_code == 401


def test_profiles_visible_to_everyone_writes_to_methodologist(s, users, client, app):
    seed(s)
    login(client, "specialist")
    profiles = client.get("/api/rule-profiles").json()
    assert len(profiles) == 2 and all(p["enabled_rules_count"] == 8 for p in profiles)
    assert [p["is_default"] for p in sorted(profiles, key=lambda p: "223" in p["name"])] == [True, False]
    profiles.sort(key=lambda p: not p["is_default"])
    assert client.get("/api/rules/system-variables").status_code == 200
    r = client.post("/api/rule-profiles", json={"name": "Новый", "short_name": "Н"})
    assert r.status_code == 403 and r.json()["error"]["message"] == "Правила доступны только методологу"
    assert client.patch(f"/api/rule-profiles/{profiles[0]['id']}", json={"name": "Х"}).status_code == 403
    assert client.delete(f"/api/rule-profiles/{profiles[0]['id']}").status_code == 403
    assert client.get("/api/rules").status_code == 403
    assert client.get(f"/api/rules/{uuid.uuid4()}").status_code == 403
    assert client.post("/api/rules", json=_new_rule(profiles[0]["id"])).status_code == 403
    assert client.delete(f"/api/rules/{uuid.uuid4()}").status_code == 403


def test_lawyer_cannot_read_rules(s, users, client, app):
    seed(s)
    login(client, "lawyer")
    assert client.get("/api/rules").status_code == 403


def test_system_variables(world, client):
    names = [v["name"] for v in client.get("/api/rules/system-variables").json()]
    assert "фрагмент" in names and all(v["type"] == "auto" for v in client.get("/api/rules/system-variables").json())


def test_list_rules_with_filters(world, client):
    rows = client.get("/api/rules").json()
    assert [r["code"] for r in rows] == [f"R-0{i}" for i in range(1, 9)]
    assert set(rows[0]) == {"id", "code", "title", "kind", "level", "enabled", "hits_30d", "review"}
    assert [r["code"] for r in client.get("/api/rules", params={"search": "r-03"}).json()] == ["R-03"]
    llm = client.get("/api/rules", params={"kind": "llm"}).json()
    assert all(r["kind"] == "llm" for r in llm)
    assert client.get("/api/rules", params={"kind": "regex"}).status_code == 422
    assert client.get("/api/rules", params={"profile_id": str(uuid.uuid4())}).json() == []


def test_rule_crud_and_versions(world, client):
    pid = _profile_id(client)
    r = client.post("/api/rules", json=_new_rule(pid))
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["version"] == 1 and rule["code"] == "X-01" and rule["warnings"] == [] and rule["updated_by"]
    assert client.get(f"/api/rules/{rule['id']}").json()["title"] == "Срок оплаты"
    assert any(x["code"] == "X-01" for x in client.get("/api/rules").json())

    r = client.patch(f"/api/rules/{rule['id']}", json={"version": 1, "title": "Срок оплаты услуг", "level": "critical"})
    assert r.status_code == 200
    assert r.json()["version"] == 2 and r.json()["title"] == "Срок оплаты услуг" and r.json()["level"] == "critical"
    assert r.json()["code"] == "X-01"  # непереданные поля не меняются

    r = client.patch(f"/api/rules/{rule['id']}", json={"version": 1, "title": "Старая версия"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert r.json()["error"]["details"]["current"]["version"] == 2

    off = client.patch(f"/api/rules/{rule['id']}", json={"version": 2, "enabled": False}).json()
    assert off["enabled"] is False

    assert client.delete(f"/api/rules/{rule['id']}").status_code == 204
    assert client.get(f"/api/rules/{rule['id']}").status_code == 404
    assert all(x["code"] != "X-01" for x in client.get("/api/rules").json())
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 404


def test_rule_without_profile_goes_to_default(world, client):
    body = _new_rule(_profile_id(client))
    del body["profile_id"]
    r = client.post("/api/rules", json=body)
    assert r.status_code == 201 and r.json()["profile_id"] == _profile_id(client)


def test_rule_validation_errors_by_field(world, client):
    pid = _profile_id(client)
    r = client.post("/api/rules", json=_new_rule(pid, code="R-01"))
    assert r.status_code == 422 and "code" in r.json()["error"]["details"]["fields"]  # код занят

    r = client.post("/api/rules", json=_new_rule(pid, prompt_template="Без переменной фрагмента"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert "prompt_template" in r.json()["error"]["details"]["fields"]

    r = client.post("/api/rules", json=_new_rule(pid, title=" "))
    assert r.status_code == 422 and "title" in r.json()["error"]["details"]["fields"]

    r = client.post("/api/rules", json=_new_rule(pid, level="red"))
    assert r.status_code == 422 and "level" in r.json()["error"]["details"]["fields"]

    r = client.post("/api/rules", json=_new_rule(pid, variables=[{"name": "x", "type": "date", "value": ""}]))
    assert r.status_code == 422 and "variables[0].type" in r.json()["error"]["details"]["fields"]

    assert client.patch(f"/api/rules/{uuid.uuid4()}", json={"version": 1, "title": "Х"}).status_code == 404
    assert client.patch(f"/api/rules/{uuid.uuid4()}", json={"title": "Х"}).status_code == 422


def test_llm_rules_limit(world, client):
    pid = _profile_id(client)
    made = 0
    last = None
    for i in range(10):
        last = client.post("/api/rules", json=_new_rule(pid, code=f"L-{i:02d}", kind="llm"))
        if last.status_code != 201:
            break
        made += 1
    assert last.status_code == 422 and last.json()["error"]["code"] == "too_many_llm_rules"
    assert made >= 1


def test_profiles_crud(world, client):
    pid = _profile_id(client)
    r = client.post("/api/rule-profiles", json={"name": "Для услуг", "short_name": "Услуги", "copy_from_profile_id": pid})
    assert r.status_code == 201
    copy = r.json()
    assert copy["is_default"] is False and copy["enabled_rules_count"] == 8
    assert len(client.get("/api/rules", params={"profile_id": copy["id"]}).json()) == 8

    assert client.post("/api/rule-profiles", json={"name": " "}).status_code == 422
    assert client.post("/api/rule-profiles", json={"name": "Без краткого"}).json()["short_name"] == "Без краткого"

    r = client.patch(f"/api/rule-profiles/{copy['id']}", json={"name": "Услуги 44-ФЗ", "is_default": True})
    assert r.json()["name"] == "Услуги 44-ФЗ" and r.json()["is_default"] is True
    profiles = client.get("/api/rule-profiles").json()
    assert profiles[0]["id"] == copy["id"] and sum(p["is_default"] for p in profiles) == 1

    r = client.delete(f"/api/rule-profiles/{copy['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "default_profile"
    assert client.delete(f"/api/rule-profiles/{pid}").status_code == 204
    assert client.delete(f"/api/rule-profiles/{pid}").status_code == 404
    assert client.patch(f"/api/rule-profiles/{uuid.uuid4()}", json={"name": "Х"}).status_code == 404


# --- Тест правила (FR-9.3) -------------------------------------------------------------------


def _draft(**over) -> dict:
    from ml.checklist import load_default_checklist

    _, specs = load_default_checklist()
    rule = next(r for r in specs if r.code == "R-04").model_dump(mode="json", exclude={"id", "version_id",
                                                                                        "query_embedding"})
    return rule | {"profile_id": ""} | over  # форма редактора шлёт и лишние поля


@pytest.fixture
def eager_worker(monkeypatch):
    """Задача `rule_test` выполняется в процессе теста: LLM и эмбеддинги — подделки, без сети."""
    from ml.analysis.embeddings import set_default_embedder
    from tests.fakes import FakeEmbedder, FakeLLM
    from worker import runtime
    from worker.app import celery_app

    fake = FakeLLM()
    monkeypatch.setattr(runtime, "get_llm", lambda: fake.client())
    saved = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = True
    set_default_embedder(FakeEmbedder())
    yield fake
    set_default_embedder(None)
    celery_app.conf.task_always_eager = saved


def test_rule_test_on_text(world, client, eager_worker):
    quote = "в течение 2 (двух) рабочих дней с даты заявки"
    eager_worker.plan = {"R-04": [(quote, "Слишком короткий срок поставки")]}
    r = client.post("/api/rules/test", json={"rule": _draft(), "text": f"Поставка осуществляется {quote}."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["duration_ms"], int)
    [finding] = body["findings"]
    assert finding["quote"] == quote and finding["level"] == "critical"
    assert finding["confidence_level"] == "high" and "confidence" not in finding
    assert finding["explanation"] == "Слишком короткий срок поставки"


def test_rule_test_validation(world, client, eager_worker):
    r = client.post("/api/rules/test", json={"rule": _draft(), "text": "  "})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == {"text": "Вставьте текст для проверки"}
    r = client.post("/api/rules/test", json={"rule": _draft(), "text": "а" * 20_001})
    assert r.status_code == 422 and r.json()["error"]["code"] == "text_too_long"
    r = client.post("/api/rules/test", json={"rule": _draft(prompt_template=""), "text": "Текст"})
    assert r.status_code == 422 and "prompt_template" in r.json()["error"]["details"]["fields"]
    r = client.post("/api/rules/test", json={"rule": _draft(prompt_template="Проверь {{нет_такой}}"), "text": "Т"})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"]
    r = client.post("/api/rules/test", json={"rule": _draft(), "document_id": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json()["error"]["message"] == "Документ не найден"
    assert not eager_worker.requests  # до воркера неверный запрос не доходит


def test_rule_test_only_for_methodologist(s, users, client, app):
    seed(s)
    login(client, "lawyer")
    assert client.post("/api/rules/test", json={"rule": _draft(), "text": "Текст"}).status_code == 403


def test_rule_test_timeout_and_worker_errors(world, client, monkeypatch):
    from api import hooks
    from ml.errors import RuleTestTimeoutError

    def slow(payload, *, timeout):
        assert timeout == 70
        raise RuleTestTimeoutError()

    monkeypatch.setattr(hooks, "run_rule_test", slow)
    r = client.post("/api/rules/test", json={"rule": _draft(), "text": "Текст"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "test_timeout"

    error = {"code": "llm_unavailable", "message": "Сервис проверки ИИ временно недоступен", "retryable": True}
    monkeypatch.setattr(hooks, "run_rule_test", lambda payload, *, timeout: {"error": error})
    r = client.post("/api/rules/test", json={"rule": _draft(), "text": "Текст"})
    assert r.status_code == 503 and r.json()["error"]["message"] == error["message"]


