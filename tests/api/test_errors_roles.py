"""Формат ошибок и проверка ролей."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel

from api.deps import require_role
from db.errors import Conflict, ValidationFailed
from db.models import User
from ml.errors import LLMError, RuleTemplateError, UnsupportedFormatError
from tests.api.conftest import login


def shape(r):
    err = r.json()["error"]
    assert set(err) == {"code", "message", "details"}
    return err


class Item(BaseModel):
    name: str
    count: int
    tags: list[int]


def _boom(exc):
    def endpoint():
        raise exc

    return endpoint


MethodologistOnly = Annotated[User, Depends(require_role("methodologist", message="Правила доступны только методологу"))]
LawyerOrSpecialist = Annotated[User, Depends(require_role("lawyer", "specialist"))]


def _only_methodologist(user: MethodologistOnly):
    return {"ok": True}


def _lawyer_or_specialist(user: LawyerOrSpecialist):
    return {"ok": True}


def _typed(item: Item):
    return item


def add_test_routes(app):
    app.add_api_route("/api/_t/rules", _only_methodologist, methods=["GET"])
    app.add_api_route("/api/_t/two", _lawyer_or_specialist, methods=["GET"])
    app.add_api_route("/api/_t/typed", _typed, methods=["POST"])
    app.add_api_route("/api/_t/data", _boom(Conflict()), methods=["GET"])
    app.add_api_route("/api/_t/fields", _boom(ValidationFailed({"code": "Код занят"})), methods=["GET"])
    app.add_api_route("/api/_t/ml-format", _boom(UnsupportedFormatError()), methods=["GET"])
    app.add_api_route("/api/_t/ml-rule", _boom(RuleTemplateError("Ошибка в инструкции")), methods=["GET"])
    app.add_api_route("/api/_t/ml-llm", _boom(LLMError()), methods=["GET"])
    app.add_api_route("/api/_t/crash", _boom(RuntimeError("секретная подробность")), methods=["GET"])


def test_role_allowed_and_forbidden(app, client, users):
    add_test_routes(app)
    login(client, "methodologist")
    assert client.get("/api/_t/rules").status_code == 200
    assert client.get("/api/_t/two").status_code == 403
    client.post("/api/auth/logout")
    login(client, "specialist")
    r = client.get("/api/_t/rules")
    assert r.status_code == 403
    assert shape(r)["code"] == "forbidden" and shape(r)["message"] == "Правила доступны только методологу"
    assert client.get("/api/_t/two").status_code == 200


def test_role_requires_session(app, client, users):
    add_test_routes(app)
    assert client.get("/api/_t/rules").status_code == 401


def test_data_error_maps_status_and_body(app, client):
    add_test_routes(app)
    r = client.get("/api/_t/data")
    assert r.status_code == 409 and shape(r)["code"] == "conflict"
    r = client.get("/api/_t/fields")
    assert r.status_code == 422 and shape(r)["details"] == {"fields": {"code": "Код занят"}}


def test_ml_errors_map_to_codes(app, client):
    add_test_routes(app)
    r = client.get("/api/_t/ml-format")
    assert r.status_code == 422 and shape(r)["code"] == "unsupported" and "не поддерживается" in shape(r)["message"]
    r = client.get("/api/_t/ml-rule")
    assert r.status_code == 422 and shape(r)["code"] == "rule_template" and shape(r)["message"] == "Ошибка в инструкции"
    r = client.get("/api/_t/ml-llm")
    assert r.status_code == 503 and shape(r)["details"] == {"retryable": True}


def test_request_validation_is_russian_422_with_fields(app, client):
    add_test_routes(app)
    r = client.post("/api/_t/typed", json={"count": "abc", "tags": [1, "x"]})
    assert r.status_code == 422
    err = shape(r)
    assert err["code"] == "validation_error" and err["message"] == "Проверьте заполнение полей"
    assert err["details"]["fields"] == {
        "name": "Обязательное поле",
        "count": "Нужно целое число",
        "tags[1]": "Нужно целое число",
    }


def test_invalid_json_body(app, client):
    add_test_routes(app)
    r = client.post("/api/_t/typed", content="{bad", headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert shape(r)["details"]["fields"] == {"body": "Тело запроса не является корректным JSON"}


def test_login_validation_error_uses_same_format(client):
    r = client.post("/api/auth/login", json={"login": ""})
    assert r.status_code == 422
    assert shape(r)["details"]["fields"] == {"login": "Слишком короткое значение", "password": "Обязательное поле"}


def test_unknown_path_and_method(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and shape(r)["code"] == "not_found"
    r = client.get("/api/auth/login")
    assert r.status_code == 405 and shape(r)["code"] == "method_not_allowed"


def test_unexpected_error_hides_internals(app, client):
    add_test_routes(app)
    r = client.get("/api/_t/crash")
    assert r.status_code == 500
    assert shape(r)["code"] == "internal_error"
    assert "секретная" not in r.text
